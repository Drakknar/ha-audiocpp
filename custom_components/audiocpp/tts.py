"""Text-to-speech engines backed by audio.cpp."""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterable
from contextlib import aclosing
import logging
import re
from typing import TYPE_CHECKING, Any, override

from homeassistant.components.tts import (
    ATTR_VOICE,
    TextToSpeechEntity,
    TtsAudioType,
    TTSAudioRequest,
    TTSAudioResponse,
    Voice,
)
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import AudioCppError, PcmFormat, build_wav, wav_header
from .const import (
    CONF_CHANNELS,
    CONF_EXTRA_OPTIONS,
    CONF_LANGUAGES,
    CONF_SAMPLE_RATE,
    CONF_SPEED,
    CONF_SPLIT_SENTENCES,
    CONF_STREAMING,
    CONF_VOICE,
    SUBENTRY_TTS,
)
from .entity import AudioCppEntity

if TYPE_CHECKING:
    from . import AudioCppConfigEntry

_LOGGER = logging.getLogger(__name__)

# audio.cpp runs one request per model at a time, so Home Assistant should
# not queue updates of its own on top.
PARALLEL_UPDATES = 0

# A sentence ends at terminal punctuation followed by whitespace (the
# whitespace is what tells a streamed "3." apart from "3.5"), at CJK
# terminal punctuation, or at a blank line.
_SENTENCE_END = re.compile(
    r"[.!?…]+[\"'”’)\]]*\s+"
    r"|[。！？]+[」』”’）]*"
    r"|\n\s*\n"
)
# Fragments shorter than this ride along with the next sentence, so "Dr."
# or "OK." do not become separate requests with their own prosody.
_MIN_SENTENCE_CHARS = 20


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: AudioCppConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up TTS engines."""
    for subentry in config_entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_TTS:
            continue
        async_add_entities(
            [AudioCppTTSEntity(config_entry, subentry)],
            config_subentry_id=subentry.subentry_id,
        )


def _take_sentences(buffer: str) -> tuple[list[str], str]:
    """Split complete sentences off ``buffer``; return them and the rest."""
    sentences: list[str] = []
    start = 0
    for match in _SENTENCE_END.finditer(buffer):
        candidate = buffer[start : match.end()].strip()
        if len(candidate) >= _MIN_SENTENCE_CHARS:
            sentences.append(candidate)
            start = match.end()
    return sentences, buffer[start:]


async def iter_sentences(chunks: AsyncIterable[str]) -> AsyncGenerator[str]:
    """Regroup streamed text into sentences as soon as each is complete."""
    buffer = ""
    async for chunk in chunks:
        buffer += chunk
        sentences, buffer = _take_sentences(buffer)
        for sentence in sentences:
            yield sentence
    if rest := buffer.strip():
        yield rest


async def _single(text: str) -> AsyncGenerator[str]:
    yield text


class AudioCppTTSEntity(TextToSpeechEntity, AudioCppEntity):
    """A TTS engine bound to one audio.cpp model."""

    _attr_supported_options = [ATTR_VOICE]

    def __init__(self, entry: AudioCppConfigEntry, subentry: ConfigSubentry) -> None:
        """Initialize the entity."""
        super().__init__(entry, subentry)
        data = subentry.data
        self._streaming: bool = data.get(CONF_STREAMING, False)
        self._split: bool = data.get(CONF_SPLIT_SENTENCES, True)
        self._voice: str | None = data.get(CONF_VOICE) or None
        self._speed: float | None = data.get(CONF_SPEED)
        self._extra: dict[str, Any] = data.get(CONF_EXTRA_OPTIONS) or {}
        self._stream_format: PcmFormat | None = (
            PcmFormat(data[CONF_SAMPLE_RATE], data.get(CONF_CHANNELS, 1))
            if data.get(CONF_SAMPLE_RATE)
            else None
        )
        languages: list[str] = data[CONF_LANGUAGES]
        self._attr_supported_languages = languages
        self._attr_default_language = languages[0]
        self._attr_default_options = (
            {ATTR_VOICE: self._voice} if self._voice else None
        )
        self._voices: list[Voice] = []

    @override
    async def async_added_to_hass(self) -> None:
        """Load the voice list once; the server's voices change only on restart."""
        await super().async_added_to_hass()
        try:
            voices = await self.client.voices(self.model)
        except AudioCppError as err:
            _LOGGER.debug("Could not list voices for %s: %s", self.model, err)
            return
        self._voices = [Voice(voice, voice) for voice in voices]

    @callback
    @override
    def async_get_supported_voices(self, language: str) -> list[Voice] | None:
        """Return the voices and presets the server offers for this model."""
        return self._voices or None

    async def _pcm(
        self, texts: AsyncIterable[str], options: dict[str, Any]
    ) -> AsyncGenerator[tuple[PcmFormat, bytes]]:
        """Synthesize each text in turn, yielding PCM with its format."""
        voice = options.get(ATTR_VOICE) or self._voice
        first_format: PcmFormat | None = None
        try:
            async for text in texts:
                if not text.strip():
                    continue
                if self._streaming:
                    if self._stream_format is None:
                        raise HomeAssistantError(
                            "Streaming is on but no sample rate is stored; "
                            "reconfigure this engine"
                        )
                    # aclosing: if the consumer stops early, close the HTTP
                    # stream now rather than whenever the generator is collected.
                    async with aclosing(
                        self.client.synthesize_stream(
                            self.model,
                            text,
                            voice=voice,
                            speed=self._speed,
                            extra=self._extra,
                        )
                    ) as stream:
                        async for pcm in stream:
                            yield self._stream_format, pcm
                    continue
                fmt, pcm = await self.client.synthesize(
                    self.model,
                    text,
                    voice=voice,
                    speed=self._speed,
                    extra=self._extra,
                )
                # Sentences are joined into one WAV, so they must agree.
                if first_format is None:
                    first_format = fmt
                elif fmt != first_format:
                    raise HomeAssistantError(
                        f"audio.cpp changed audio format mid-message "
                        f"({first_format} then {fmt})"
                    )
                yield fmt, pcm
        except AudioCppError as err:
            raise HomeAssistantError(f"audio.cpp TTS failed: {err}") from err

    def _texts(self, message_gen: AsyncIterable[str]) -> AsyncIterable[str]:
        if self._split:
            return iter_sentences(message_gen)
        return self._joined(message_gen)

    @staticmethod
    async def _joined(message_gen: AsyncIterable[str]) -> AsyncGenerator[str]:
        yield "".join([chunk async for chunk in message_gen])

    @override
    async def async_stream_tts_audio(
        self, request: TTSAudioRequest
    ) -> TTSAudioResponse:
        """Synthesize text as it streams in, emitting one streamed WAV.

        Overriding this tells Assist the engine takes streaming input, so a
        reply is spoken sentence by sentence while the LLM is still writing.
        """
        options = request.options or {}

        async def data_gen() -> AsyncGenerator[bytes]:
            header_sent = False
            async with aclosing(
                self._pcm(self._texts(request.message_gen), options)
            ) as chunks:
                async for fmt, pcm in chunks:
                    if not header_sent:
                        yield wav_header(fmt)
                        header_sent = True
                    yield pcm
            if not header_sent:
                raise HomeAssistantError("audio.cpp returned no audio")

        return TTSAudioResponse("wav", data_gen())

    @override
    async def async_get_tts_audio(
        self, message: str, language: str, options: dict[str, Any]
    ) -> TtsAudioType:
        """Synthesize a whole message into a complete WAV file."""
        texts = iter_sentences(_single(message)) if self._split else _single(message)
        fmt: PcmFormat | None = None
        pcm = bytearray()
        async for chunk_format, chunk in self._pcm(texts, options):
            fmt = chunk_format
            pcm.extend(chunk)
        if fmt is None:
            raise HomeAssistantError("audio.cpp returned no audio")
        return "wav", build_wav(fmt, bytes(pcm))
