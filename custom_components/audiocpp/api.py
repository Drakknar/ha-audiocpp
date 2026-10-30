"""Async client for the audio.cpp HTTP server (``audiocpp_server``).

Endpoints used, as documented in the server README:

* ``GET  /health`` and ``GET /v1/models`` to validate and discover models,
  with ``include_params`` for each model's declared request options.
* ``GET  /v1/audio/voices`` to list voices and voice presets for a model.
* ``POST /v1/audio/speech`` for TTS, either as one WAV response or, for a
  model in streaming mode, as SSE ``speech.audio.delta`` events carrying
  base64 16-bit PCM.
* ``POST /v1/audio/transcriptions`` (multipart WAV upload) for ASR, and
  ``POST /v1/audio/transcriptions/live`` (chunked raw PCM in, SSE out) for
  a model in streaming mode, so recognition runs while the user speaks.
"""

import base64
import binascii
from collections.abc import AsyncGenerator, AsyncIterable, AsyncIterator, Mapping
from contextlib import aclosing, asynccontextmanager, suppress
from dataclasses import dataclass
import io
import json
import logging
from typing import Any
from urllib.parse import urlsplit, urlunsplit
import wave

import aiohttp

from .const import MODE_STREAMING, SAMPLE_WIDTH

_LOGGER = logging.getLogger(__name__)

CONNECT_TIMEOUT = 10
# A model configured with ``lazy_load`` is loaded by its first request, which
# can take minutes on a large model, so reads get a generous bound.
READ_TIMEOUT = 300
# Longest error body kept for a message. The server sends short JSON errors;
# anything longer is not an audio.cpp error and only needs to be recognisable.
MAX_ERROR_TEXT = 300

# Fields the client sets itself. User supplied extra options never override
# them, so a stray key cannot switch the response to a format we do not read.
RESERVED_SPEECH_KEYS = frozenset(
    {"model", "input", "stream", "stream_format", "response_format"}
)


class AudioCppError(Exception):
    """Base error for the audio.cpp client."""


class AudioCppConnectionError(AudioCppError):
    """The server could not be reached or did not answer in time."""


class AudioCppAuthError(AudioCppError):
    """The server (or a proxy in front of it) refused the credentials."""


class AudioCppRequestError(AudioCppError):
    """The server rejected or failed a request."""


@dataclass(frozen=True, slots=True)
class ModelParam:
    """A request option a model's spec declares (``include_params``).

    Only what the option check uses is kept; the server also sends the
    option's type, description, default and whether it is required.
    """

    name: str
    min: float | None = None
    max: float | None = None
    # Allowed values of an enum option; empty for any other type.
    values: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ModelInfo:
    """A model configured on the server."""

    id: str
    family: str
    task: str
    mode: str
    # The request options the model declares, or None when the server does
    # not report them (servers before audio.cpp v0.9.1).
    params: tuple[ModelParam, ...] | None = None
    # Whether the server reads a top-level ``instruct`` (audio.cpp
    # v0.9.2-drama3-base-small and later, which also list ``configured``).
    instruct_field: bool = False

    @property
    def streaming(self) -> bool:
        """Return True if the server runs this model in streaming mode."""
        return self.mode == MODE_STREAMING

    def param(self, name: str) -> ModelParam | None:
        """Return the declared option ``name``, if there is one."""
        return next((p for p in self.params or () if p.name == name), None)

    @property
    def speed_param(self) -> ModelParam | None:
        """Return the option behind the top-level ``speed`` field, if any.

        The server sends ``speed`` to a model declaring either ``speed`` or
        ``speaking_rate``, and rejects it (other than 1) for any other model.
        """
        return self.param("speed") or self.param("speaking_rate")


@dataclass(frozen=True, slots=True)
class PcmFormat:
    """Layout of raw PCM audio."""

    sample_rate: int
    channels: int
    sample_width: int = SAMPLE_WIDTH


def normalize_url(url: str) -> str:
    """Return the server base URL, or raise ValueError if it is not usable.

    A trailing ``/`` or ``/v1`` is dropped, since people often paste the
    OpenAI style base URL; every path is added by the client.
    """
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("URL must be http:// or https:// with a host")
    if parts.username is not None or parts.password is not None:
        # Credentials in the URL would end up in the entry title and in
        # logged errors. A proxy token belongs in the API key field.
        raise ValueError("URL must not contain a username or password")
    path = parts.path.rstrip("/")
    path = path.removesuffix("/v1")
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def wav_header(fmt: PcmFormat) -> bytes:
    """Return a WAV header for a stream of unknown length.

    The frame count is 0, the same convention Home Assistant's Wyoming
    integration uses for streamed WAV; ffmpeg reads to the end of the data.
    """
    with io.BytesIO() as buffer:
        with wave.open(buffer, "wb") as writer:
            writer.setframerate(fmt.sample_rate)
            writer.setsampwidth(fmt.sample_width)
            writer.setnchannels(fmt.channels)
        return buffer.getvalue()


def build_wav(fmt: PcmFormat, pcm: bytes) -> bytes:
    """Return a complete WAV file holding ``pcm``."""
    with io.BytesIO() as buffer:
        with wave.open(buffer, "wb") as writer:
            writer.setframerate(fmt.sample_rate)
            writer.setsampwidth(fmt.sample_width)
            writer.setnchannels(fmt.channels)
            writer.writeframes(pcm)
        return buffer.getvalue()


def parse_wav(data: bytes) -> tuple[PcmFormat, bytes]:
    """Split a WAV file into its format and raw PCM frames."""
    try:
        with wave.open(io.BytesIO(data), "rb") as reader:
            fmt = PcmFormat(
                sample_rate=reader.getframerate(),
                channels=reader.getnchannels(),
                sample_width=reader.getsampwidth(),
            )
            frames = reader.readframes(reader.getnframes())
    except (wave.Error, EOFError) as err:
        raise AudioCppRequestError(f"Server returned invalid WAV audio: {err}") from err
    return fmt, frames


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _parse_params(rows: Any) -> tuple[ModelParam, ...] | None:
    """Parse the ``params`` of a ``/v1/models`` entry; None if absent."""
    if not isinstance(rows, list):
        return None
    params = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            continue
        values = row.get("values")
        params.append(
            ModelParam(
                name=row["name"],
                min=_number(row.get("min")),
                max=_number(row.get("max")),
                values=tuple(str(v) for v in values)
                if isinstance(values, list)
                else (),
            )
        )
    return tuple(params)


async def _error_from_response(response: aiohttp.ClientResponse) -> AudioCppError:
    """Build an exception from an HTTP error response."""
    text = (await response.text(errors="replace"))[:MAX_ERROR_TEXT]
    message = text
    with suppress(ValueError, KeyError, TypeError):
        message = json.loads(text)["error"]["message"]
    message = f"HTTP {response.status}: {message}".strip()
    if response.status in (401, 403):
        return AudioCppAuthError(message)
    return AudioCppRequestError(message)


async def iter_sse(response: aiohttp.ClientResponse) -> AsyncGenerator[dict[str, Any]]:
    """Yield the JSON events of a server-sent event stream until ``[DONE]``.

    Lines are split by hand rather than with ``readline``: a TTS delta holds
    a whole base64 audio chunk on one line, which can exceed aiohttp's line
    length limit.
    """
    buffer = bytearray()
    data_lines: list[bytes] = []
    async for chunk in response.content.iter_any():
        buffer.extend(chunk)
        while (newline := buffer.find(b"\n")) >= 0:
            line = bytes(buffer[:newline]).rstrip(b"\r")
            del buffer[: newline + 1]
            if line.startswith(b"data:"):
                value = line[len(b"data:") :]
                data_lines.append(value[1:] if value.startswith(b" ") else value)
                continue
            if line or not data_lines:
                # Comments and other SSE fields carry nothing we use.
                continue
            payload = b"\n".join(data_lines)
            data_lines.clear()
            if payload.strip() == b"[DONE]":
                return
            try:
                event = json.loads(payload)
            except ValueError as err:
                raise AudioCppRequestError(
                    "Server sent a malformed stream event"
                ) from err
            if not isinstance(event, dict):
                continue
            if event.get("type") == "error":
                error = event.get("error")
                message = error.get("message") if isinstance(error, dict) else error
                raise AudioCppRequestError(f"Server stream error: {message}")
            yield event
    raise AudioCppRequestError("Server stream ended before it was complete")


class AudioCppClient:
    """Client for one audio.cpp server."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        url: str,
        api_key: str | None = None,
    ) -> None:
        """Initialize the client."""
        self._session = session
        self._url = normalize_url(url)
        # audio.cpp has no authentication of its own. The key is only for a
        # reverse proxy that adds it, and is never sent when unset.
        self._headers: dict[str, str] = (
            {"Authorization": f"Bearer {api_key}"} if api_key else {}
        )

    @property
    def url(self) -> str:
        """Return the server base URL."""
        return self._url

    @asynccontextmanager
    async def _request(
        self,
        method: str,
        path: str,
        *,
        read_timeout: float = READ_TIMEOUT,
        headers: Mapping[str, str] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[aiohttp.ClientResponse]:
        """Send a request and yield the response once its status is good."""
        timeout = aiohttp.ClientTimeout(
            total=None, sock_connect=CONNECT_TIMEOUT, sock_read=read_timeout
        )
        try:
            async with self._session.request(
                method,
                f"{self._url}{path}",
                headers={**self._headers, **(headers or {})},
                timeout=timeout,
                **kwargs,
            ) as response:
                if response.status >= 400:
                    raise await _error_from_response(response)
                yield response
        except TimeoutError as err:
            raise AudioCppConnectionError(
                f"Timed out talking to audio.cpp at {self._url}"
            ) from err
        except aiohttp.ClientError as err:
            raise AudioCppConnectionError(
                f"Error talking to audio.cpp at {self._url}: {err}"
            ) from err

    async def _get_json(self, path: str, **kwargs: Any) -> Any:
        async with self._request(
            "GET", path, read_timeout=CONNECT_TIMEOUT, **kwargs
        ) as response:
            try:
                return await response.json(content_type=None)
            except ValueError as err:
                raise AudioCppRequestError(
                    f"{path} did not return JSON; is this an audio.cpp server?"
                ) from err

    async def health(self) -> dict[str, Any]:
        """Return the server health report, raising if it is not ready."""
        data = await self._get_json("/health")
        if not isinstance(data, dict) or data.get("status") != "ok":
            raise AudioCppRequestError("audio.cpp server did not report status ok")
        return data

    async def models(self) -> list[ModelInfo]:
        """Return the models configured on the server.

        ``include_params`` (audio.cpp v0.9.1) adds each model's declared
        request options; older servers ignore the flag and omit them.
        """
        data = await self._get_json("/v1/models", params={"include_params": "true"})
        entries = data.get("data") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            raise AudioCppRequestError("/v1/models returned an unexpected shape")
        return [
            ModelInfo(
                id=str(entry["id"]),
                family=str(entry.get("family", "")),
                task=str(entry.get("task", "")),
                mode=str(entry.get("mode", "")),
                params=_parse_params(entry.get("params")),
                instruct_field="configured" in entry,
            )
            for entry in entries
            if isinstance(entry, dict) and entry.get("id")
        ]

    async def voices(self, model: str) -> list[str]:
        """Return the voice ids and preset names available to a TTS model."""
        data = await self._get_json("/v1/audio/voices", params={"model": model})
        voices = data.get("voices") if isinstance(data, dict) else None
        if not isinstance(voices, list):
            return []
        return [str(voice) for voice in voices if voice]

    @staticmethod
    def _speech_body(
        model: str,
        text: str,
        voice: str | None,
        speed: float | None,
        extra: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        body = {
            key: value
            for key, value in (extra or {}).items()
            if key not in RESERVED_SPEECH_KEYS
        }
        body["model"] = model
        body["input"] = text
        if voice:
            body["voice"] = voice
        if speed is not None:
            body["speed"] = speed
        return body

    async def synthesize(
        self,
        model: str,
        text: str,
        *,
        voice: str | None = None,
        speed: float | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> tuple[PcmFormat, bytes]:
        """Synthesize ``text`` in one request; return its format and PCM."""
        body = self._speech_body(model, text, voice, speed, extra)
        body["response_format"] = "wav"
        async with self._request("POST", "/v1/audio/speech", json=body) as response:
            data = await response.read()
        return parse_wav(data)

    async def synthesize_stream(
        self,
        model: str,
        text: str,
        *,
        voice: str | None = None,
        speed: float | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> AsyncGenerator[bytes]:
        """Synthesize ``text`` on a streaming model, yielding PCM as it comes.

        The stream does not say its sample rate or channel count; the caller
        has to know them (see ``PROBE_TEXT``).
        """
        body = self._speech_body(model, text, voice, speed, extra)
        body["response_format"] = "pcm"
        body["stream_format"] = "sse"
        async with (
            self._request(
                "POST",
                "/v1/audio/speech",
                json=body,
                headers={"Accept": "text/event-stream"},
            ) as response,
            aclosing(iter_sse(response)) as events,
        ):
            async for event in events:
                if event.get("type") != "speech.audio.delta":
                    continue
                try:
                    pcm = base64.b64decode(event["audio"], validate=True)
                except (KeyError, TypeError, binascii.Error) as err:
                    raise AudioCppRequestError(
                        "Server sent an audio event without valid audio"
                    ) from err
                if pcm:
                    yield pcm

    async def transcribe(
        self,
        model: str,
        wav: bytes,
        *,
        language: str | None = None,
        prompt: str | None = None,
    ) -> str:
        """Transcribe a complete WAV recording."""
        form = aiohttp.FormData()
        form.add_field("model", model)
        if language:
            form.add_field("language", language)
        if prompt:
            form.add_field("prompt", prompt)
        # The server only accepts uploads whose filename ends in .wav.
        form.add_field("file", wav, filename="audio.wav", content_type="audio/wav")
        async with self._request(
            "POST", "/v1/audio/transcriptions", data=form
        ) as response:
            try:
                data = await response.json(content_type=None)
            except ValueError as err:
                raise AudioCppRequestError(
                    "Transcription response was not JSON"
                ) from err
        if not isinstance(data, dict):
            raise AudioCppRequestError("Transcription response had an unexpected shape")
        return str(data.get("text") or "")

    async def transcribe_live(
        self,
        model: str,
        audio: AsyncIterable[bytes],
        fmt: PcmFormat,
        *,
        language: str | None = None,
        prompt: str | None = None,
    ) -> str:
        """Stream 16-bit PCM to a streaming model while it is captured.

        The body goes out with chunked transfer encoding and the transcript
        events are read on the same connection, so the model decodes while
        the user is still speaking. The terminating chunk is sent when
        ``audio`` is exhausted; the server treats a connection closed
        without it as an error rather than as end of speech.
        """
        params = {
            "model": model,
            "sample_rate": str(fmt.sample_rate),
            "channels": str(fmt.channels),
            "sample_format": "s16le",
        }
        if language:
            params["language"] = language
        if prompt:
            params["prompt"] = prompt

        async def body() -> AsyncGenerator[bytes]:
            async for chunk in audio:
                if chunk:
                    yield chunk

        deltas: list[str] = []
        async with (
            self._request(
                "POST",
                "/v1/audio/transcriptions/live",
                params=params,
                data=body(),
                headers={
                    "Content-Type": "application/octet-stream",
                    "Accept": "text/event-stream",
                },
            ) as response,
            aclosing(iter_sse(response)) as events,
        ):
            async for event in events:
                event_type = event.get("type")
                if event_type == "transcript.text.delta":
                    deltas.append(str(event.get("delta") or ""))
                elif event_type == "transcript.text.done":
                    return str(event.get("text") or "")
        # The done event carries the full transcript; deltas are a fallback.
        return "".join(deltas)
