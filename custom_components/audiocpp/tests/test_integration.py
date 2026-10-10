"""End-to-end tests against a fake audio.cpp server."""

from __future__ import annotations

from collections.abc import AsyncGenerator
import io
import shutil
import wave

from aiohttp.test_utils import TestServer
import pytest

from homeassistant.components import stt, tts
from homeassistant.components.tts import TTSAudioRequest
from homeassistant.components.tts.const import DATA_TTS_MANAGER
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from custom_components.audiocpp.api import normalize_url
from custom_components.audiocpp.const import DOMAIN
from custom_components.audiocpp.tts import _take_sentences

from custom_components.audiocpp.tests.fake_server import SAMPLE_RATE, FakeAudioCpp


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations, socket_enabled):
    """Load custom_components/ and allow loopback sockets in every test."""


@pytest.fixture
async def fake() -> AsyncGenerator[tuple[FakeAudioCpp, str]]:
    server_state = FakeAudioCpp()
    server = TestServer(server_state.app, host="127.0.0.1")
    await server.start_server()
    # Pasting the OpenAI-style base URL must work too.
    yield server_state, str(server.make_url("/v1/"))
    await server.close()


async def _add_server(hass: HomeAssistant, url: str):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"url": url})
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    await hass.async_block_till_done()
    entry = hass.config_entries.async_get_entry(result["result"].entry_id)
    assert entry.data["url"] == normalize_url(url)
    assert not entry.data["url"].endswith("/v1")
    return entry


async def _add_engine(hass, entry, kind: str, model: str, settings: dict):
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, kind), context={"source": "user"}
    )
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "model"
    listed = [o["value"] for o in result["data_schema"].schema["model"].config["options"]]
    expected = {"tts": ["kitt-en", "kokoro", "chatterbox-italian"], "stt": ["nemotron-stream", "parakeet"]}
    assert listed == expected[kind]
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"model": model}
    )
    assert result["step_id"] == "settings", result
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], settings
    )
    await hass.async_block_till_done()
    return result


def _entity(hass, domain_component, platform: str, subentry_id: str):
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, subentry_id)
    assert entity_id, f"no {platform} entity for {subentry_id}"
    return hass.data[domain_component].get_entity(entity_id)


def _subentry(entry, kind) -> ConfigSubentry:
    return next(s for s in entry.subentries.values() if s.subentry_type == kind)


async def _text(*chunks: str) -> AsyncGenerator[str]:
    for chunk in chunks:
        yield chunk


async def _collect(response) -> bytes:
    return b"".join([chunk async for chunk in response.data_gen])


def _expected_pcm(text: str) -> bytes:
    return b"".join(ord(ch).to_bytes(2, "little", signed=True) for ch in text)


def _frames(wav_bytes: bytes) -> bytes:
    """Return the PCM of a WAV, whatever chunks ffmpeg wrote before the data."""
    data = wav_bytes.index(b"data") + 8
    return wav_bytes[data:]


def test_sentence_split() -> None:
    sentences, rest = _take_sentences(
        "Hi. This is the first full sentence. Version 3.5 ships now! And more"
    )
    # "Hi." is too short alone, so it joins the next sentence; "3.5" is not a break.
    assert sentences == ["Hi. This is the first full sentence.", "Version 3.5 ships now!"]
    assert rest == "And more"


async def test_server_flow_errors(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"url": "ftp://nope"}
    )
    assert result["errors"] == {"url": "invalid_url"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"url": "http://127.0.0.1:1"}
    )
    assert result["errors"] == {"base": "cannot_connect"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"url": "http://user:secret@127.0.0.1:8080"}
    )
    assert result["errors"] == {"url": "invalid_url"}


async def test_reconfigure_keeps_api_key(hass: HomeAssistant, fake) -> None:
    _, url = fake
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"url": url, "api_key": "proxy-token"}
    )
    entry = result["result"]
    await hass.async_block_till_done()
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "reconfigure", "entry_id": entry.entry_id}
    )
    # The stored secret is not sent back to pre-fill the form.
    assert "proxy-token" not in str(result["data_schema"].schema)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"url": url})
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["api_key"] == "proxy-token"


async def test_streaming_tts(hass: HomeAssistant, fake) -> None:
    server, url = fake
    entry = await _add_server(hass, url)
    result = await _add_engine(
        hass, entry, "tts", "kitt-en",
        {"name": "KITT English", "languages": ["en"], "voice": "kitt_eng",
         "streaming": True, "split_sentences": True},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    sub = _subentry(entry, "tts")
    # The probe found the rate the stream itself never reports.
    assert sub.data["sample_rate"] == SAMPLE_RATE and sub.data["channels"] == 1

    entity = _entity(hass, tts.DATA_COMPONENT, "tts", sub.subentry_id)
    assert entity.async_supports_streaming_input()
    assert [v.voice_id for v in entity.async_get_supported_voices("en")] == ["kitt_eng", "alba"]

    server.speech_requests.clear()
    first = "Good evening, Michael. "
    second = "I am scanning the area now."
    response = await entity.async_stream_tts_audio(
        TTSAudioRequest(language="en", options={}, message_gen=_text("Good eve", "ning, Michael. I am scan", "ning the area now."))
    )
    assert response.extension == "wav"
    audio = await _collect(response)
    with wave.open(io.BytesIO(audio), "rb") as reader:
        assert reader.getframerate() == SAMPLE_RATE
    assert audio[44:] == _expected_pcm(first.strip()) + _expected_pcm(second)
    # One SSE request per sentence, voice sent, model's format fields forced.
    assert [r["input"] for r in server.speech_requests] == [first.strip(), second]
    assert all(r["stream_format"] == "sse" and r["response_format"] == "pcm" and r["voice"] == "kitt_eng" for r in server.speech_requests)

    # An SSE error event surfaces as a Home Assistant error.
    response = await entity.async_stream_tts_audio(
        TTSAudioRequest(language="en", options={}, message_gen=_text("Please FAIL now."))
    )
    with pytest.raises(HomeAssistantError, match="boom"):
        await _collect(response)

    # The non-streaming entry point returns a complete, correctly sized WAV.
    extension, data = await entity.async_get_tts_audio("Short test sentence here.", "en", {})
    with wave.open(io.BytesIO(data), "rb") as reader:
        assert reader.getnframes() == len("Short test sentence here.")


async def test_offline_tts_and_probe_error(hass: HomeAssistant, fake) -> None:
    server, url = fake
    entry = await _add_server(hass, url)

    # Speed is rejected by kitt-en: the test synthesis shows the server's reason.
    result = await _add_engine(
        hass, entry, "tts", "kitt-en",
        {"name": "x", "languages": ["en"], "streaming": False, "split_sentences": True, "speed": 1.5},
    )
    assert result["errors"] == {"base": "probe_failed"}
    assert "does not support speed" in result["description_placeholders"]["error"]

    result = await _add_engine(
        hass, entry, "tts", "kokoro",
        {"name": "Kokoro", "languages": ["it-IT", "en"], "split_sentences": False,
         "extra_options": {"seed": 7, "model": "hijack"}},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    sub = _subentry(entry, "tts")
    assert sub.data["streaming"] is False  # offline model: never streams
    entity = _entity(hass, tts.DATA_COMPONENT, "tts", sub.subentry_id)
    assert entity.default_language == "it-IT"

    server.speech_requests.clear()
    text = "Buonasera. Sono KITT, come posso aiutarti?"
    response = await entity.async_stream_tts_audio(
        TTSAudioRequest(language="it-IT", options={"voice": "alba"}, message_gen=_text("Buonasera. ", "Sono KITT, come posso aiutarti?"))
    )
    audio = await _collect(response)
    assert audio[44:] == _expected_pcm(text)
    (request,) = server.speech_requests
    assert request["model"] == "kokoro" and request["seed"] == 7 and request["voice"] == "alba"
    assert "stream_format" not in request


async def test_stt_live_and_file(hass: HomeAssistant, fake) -> None:
    server, url = fake
    entry = await _add_server(hass, url)
    result = await _add_engine(
        hass, entry, "stt", "nemotron-stream",
        {"name": "Nemotron", "languages": ["en"], "streaming": True,
         "language_hint": "full", "prompt": "KITT, Michael"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    sub = _subentry(entry, "stt")
    entity = _entity(hass, stt.DATA_COMPONENT, "stt", sub.subentry_id)
    metadata = stt.SpeechMetadata(
        language="en-US", format=stt.AudioFormats.WAV, codec=stt.AudioCodecs.PCM,
        bit_rate=stt.AudioBitRates.BITRATE_16, sample_rate=stt.AudioSampleRates.SAMPLERATE_16000,
        channel=stt.AudioChannels.CHANNEL_MONO,
    )

    async def mic() -> AsyncGenerator[bytes]:
        for _ in range(5):
            yield b"\x01\x00" * 160

    result = await entity.async_process_audio_stream(metadata, mic())
    assert result == stt.SpeechResult("live 1600 bytes", stt.SpeechResultState.SUCCESS)
    (live,) = server.live
    assert live["model"] == "nemotron-stream" and live["sample_rate"] == "16000"
    assert live["language"] == "en-US" and live["prompt"] == "KITT, Michael"
    assert live["chunked"] == "chunked"

    # Same model, file upload instead of live.
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "stt"), context={"source": "reconfigure", "subentry_id": sub.subentry_id}
    )
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {"model": "parakeet"})
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Parakeet", "languages": ["en"], "language_hint": "code"}
    )
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    entity = _entity(hass, stt.DATA_COMPONENT, "stt", sub.subentry_id)
    result = await entity.async_process_audio_stream(metadata, mic())
    assert result == stt.SpeechResult("heard 800 frames", stt.SpeechResultState.SUCCESS)
    (upload,) = server.transcriptions
    assert upload == {"model": "parakeet", "language": "en", "prompt": None,
                      "filename": "audio.wav", "frames": 800, "rate": 16000}


async def test_tts_through_home_assistant(hass: HomeAssistant, fake) -> None:
    """Go through Home Assistant's TTS manager, as tts.speak and Assist do.

    Calling the entity directly skips the manager's own checks; this caught an
    unnamed entity being refused with "TTS engine name is not set".
    """
    ffmpeg_bin = shutil.which("ffmpeg")
    if ffmpeg_bin is None:
        imageio_ffmpeg = pytest.importorskip("imageio_ffmpeg", reason="needs an ffmpeg binary")
        ffmpeg_bin = imageio_ffmpeg.get_ffmpeg_exe()
    assert await async_setup_component(hass, "ffmpeg", {"ffmpeg": {"ffmpeg_bin": ffmpeg_bin}})

    server, url = fake
    entry = await _add_server(hass, url)
    await _add_engine(
        hass, entry, "tts", "kitt-en",
        {"name": "omnivoice-eng", "languages": ["en"], "voice": "kitt_eng",
         "streaming": True, "split_sentences": True},
    )
    # The harness shares one config dir between tests and runs. Without
    # this, the fake server's tiny clips stay in its TTS file cache and are
    # served to later runs that ask for the same message.
    hass.data[DATA_TTS_MANAGER].use_file_cache = False
    sub = _subentry(entry, "tts")
    entity_id = er.async_get(hass).async_get_entity_id("tts", DOMAIN, sub.subentry_id)
    assert entity_id == "tts.omnivoice_eng"
    assert hass.states.get(entity_id).name == "omnivoice-eng"

    # A whole message, as tts.speak sends it. By default the manager hands
    # players MP3.
    stream = tts.async_create_stream(hass, entity_id, language="en")
    stream.async_set_message("Good evening, Michael. I am monitoring the area.")
    audio = b"".join([chunk async for chunk in stream.async_stream_result()])
    assert stream.extension == "mp3" and len(audio) > 0

    # Asking for WAV keeps the samples exact, so the content can be checked.
    stream = tts.async_create_stream(
        hass, entity_id, language="en", options={"preferred_format": "wav"}
    )
    stream.async_set_message("Good evening, Michael. I am monitoring the area.")
    audio = b"".join([chunk async for chunk in stream.async_stream_result()])
    assert stream.extension == "wav"
    assert _frames(audio) == _expected_pcm("Good evening, Michael.") + _expected_pcm("I am monitoring the area.")

    # A streamed message, as Assist sends an LLM reply.
    stream = tts.async_create_stream(
        hass, entity_id, language="en", options={"preferred_format": "wav"}
    )
    stream.async_set_message_stream(_text("All qu", "iet tonight, Michael. ", "Shall I continue?"))
    audio = b"".join([chunk async for chunk in stream.async_stream_result()])
    assert _frames(audio) == _expected_pcm("All quiet tonight, Michael.") + _expected_pcm("Shall I continue?")
