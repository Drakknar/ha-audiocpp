"""A stand-in audio.cpp server speaking the documented wire format."""

from __future__ import annotations

import base64
import copy
import io
import json
import wave

from aiohttp import web

SAMPLE_RATE = 24000
MODELS = [
    {"id": "kitt-en", "family": "pocket_tts", "task": "tts", "mode": "streaming"},
    {"id": "kokoro", "family": "kokoro_tts", "task": "tts", "mode": "offline"},
    {"id": "chatterbox-italian", "family": "chatterbox", "task": "clon", "mode": "offline"},
    {"id": "nemotron-stream", "family": "nemotron_asr", "task": "asr", "mode": "streaming"},
    {"id": "parakeet", "family": "parakeet_tdt", "task": "asr", "mode": "offline"},
]
# Request options each model declares (``include_params``, audio.cpp v0.9.1+).
PARAMS = {
    "kitt-en": [
        {"name": "seed", "type": "int", "min": 0},
        {"name": "temperature", "type": "float", "default": 0.7, "min": 0},
        {"name": "text_chunk_tokens", "type": "int", "min": 1},
        {"name": "text_chunk_mode", "type": "enum", "preset": "text_chunk_mode_full",
         "values": ["word_budget", "tag_aware"]},
    ],
    "kokoro": [
        {"name": "seed", "type": "int", "min": 0},
        {"name": "speed", "type": "float", "default": 1, "min": 0.5, "max": 2.0},
    ],
    "chatterbox-italian": [
        {"name": "language", "type": "string"},
        {"name": "exaggeration", "type": "float", "default": 0.5},
        {"name": "speed", "type": "float", "default": 1},
    ],
    "nemotron-stream": [],
    "parakeet": [],
}


def _pcm(text: str) -> bytes:
    # One 16-bit sample per character, value = character code, so a test can
    # read back which text produced which audio.
    return b"".join(min(ord(ch), 32767).to_bytes(2, "little", signed=True) for ch in text)


def _wav(pcm: bytes) -> bytes:
    with io.BytesIO() as buffer:
        with wave.open(buffer, "wb") as writer:
            writer.setframerate(SAMPLE_RATE)
            writer.setsampwidth(2)
            writer.setnchannels(1)
            writer.writeframes(pcm)
        return buffer.getvalue()


def _error(status: int, message: str) -> web.Response:
    return web.json_response(
        {"error": {"message": message, "type": "invalid_request_error"}}, status=status
    )


class FakeAudioCpp:
    """Records what it was sent so tests can assert on it."""

    def __init__(self) -> None:
        self.speech_requests: list[dict] = []
        self.transcriptions: list[dict] = []
        self.live: list[dict] = []
        # None acts as a server before v0.9.1, which reports no params.
        self.params: dict[str, list[dict]] | None = copy.deepcopy(PARAMS)
        self.app = web.Application()
        self.app.router.add_get("/health", self.health)
        self.app.router.add_get("/v1/models", self.models)
        self.app.router.add_get("/v1/audio/voices", self.voices)
        self.app.router.add_post("/v1/audio/speech", self.speech)
        self.app.router.add_post("/v1/audio/transcriptions", self.transcribe)
        self.app.router.add_post("/v1/audio/transcriptions/live", self.transcribe_live)

    async def health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "backend": "cpu", "models": len(MODELS)})

    async def models(self, request: web.Request) -> web.Response:
        data = [{**m, "object": "model", "loaded": False} for m in MODELS]
        if self.params is not None and request.query.get("include_params") == "true":
            for entry in data:
                entry["params"] = self.params[entry["id"]]
        return web.json_response({"object": "list", "data": data})

    async def voices(self, request: web.Request) -> web.Response:
        return web.json_response({"voices": ["kitt_eng", "alba"]})

    async def speech(self, request: web.Request) -> web.StreamResponse:
        body = await request.json()
        self.speech_requests.append(body)
        model = next((m for m in MODELS if m["id"] == body["model"]), None)
        if model is None:
            return _error(400, f"unknown model: {body['model']}")
        if "speed" in body and body["model"] == "kitt-en":
            return _error(400, "model does not support speed")
        text = body["input"]
        if "stream_format" not in body:
            return web.Response(body=_wav(_pcm(text)), content_type="audio/wav")
        if model["mode"] != "streaming":
            return _error(500, "speech streaming requires a model configured with mode=streaming")
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        if "FAIL" in text:
            await response.write(b'data: {"type":"error","error":{"message":"boom"}}\n\n')
            await response.write_eof()
            return response
        pcm = _pcm(text)
        half = len(pcm) // 2 // 2 * 2
        for part in (pcm[:half], pcm[half:]):
            event = {"type": "speech.audio.delta", "audio": base64.b64encode(part).decode()}
            await response.write(f"data: {json.dumps(event)}\n\n".encode())
        await response.write(b'data: {"type":"speech.audio.done","timing":{"ttft_ms":1}}\n\n')
        await response.write(b"data: [DONE]\n\n")
        await response.write_eof()
        return response

    async def transcribe(self, request: web.Request) -> web.Response:
        form = await request.post()
        upload = form["file"]
        audio = upload.file.read()
        with wave.open(io.BytesIO(audio), "rb") as reader:
            frames = reader.getnframes()
            rate = reader.getframerate()
        self.transcriptions.append(
            {
                "model": form["model"],
                "language": form.get("language"),
                "prompt": form.get("prompt"),
                "filename": upload.filename,
                "frames": frames,
                "rate": rate,
            }
        )
        return web.json_response({"text": f" heard {frames} frames ", "timing": {}})

    async def transcribe_live(self, request: web.Request) -> web.StreamResponse:
        # Headers go out before the body is read, as the real server does, so
        # this only works if the client reads and writes at the same time.
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        received = 0
        chunks = 0
        async for chunk in request.content.iter_any():
            received += len(chunk)
            chunks += 1
            delta = {"type": "transcript.text.delta", "delta": f"[{len(chunk)}]"}
            await response.write(f"data: {json.dumps(delta)}\n\n".encode())
        self.live.append({**request.query, "bytes": received, "chunked": request.headers.get("Transfer-Encoding")})
        done = {"type": "transcript.text.done", "text": f"live {received} bytes"}
        await response.write(f"data: {json.dumps(done)}\n\n".encode())
        await response.write(b"data: [DONE]\n\n")
        await response.write_eof()
        return response
