"""Speech-to-text engines backed by audio.cpp."""

from __future__ import annotations

from collections.abc import AsyncIterable
import logging
from typing import TYPE_CHECKING, override

from homeassistant.components import stt
from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import CONF_PROMPT
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import AudioCppError, PcmFormat, build_wav
from .const import (
    CONF_LANGUAGE_HINT,
    CONF_LANGUAGES,
    CONF_STREAMING,
    LANGUAGE_HINT_CODE,
    LANGUAGE_HINT_FULL,
    LANGUAGE_HINT_NONE,
    SUBENTRY_STT,
)
from .entity import AudioCppEntity

if TYPE_CHECKING:
    from . import AudioCppConfigEntry

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: AudioCppConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up STT engines."""
    for subentry in config_entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_STT:
            continue
        async_add_entities(
            [AudioCppSTTEntity(config_entry, subentry)],
            config_subentry_id=subentry.subentry_id,
        )


class AudioCppSTTEntity(stt.SpeechToTextEntity, AudioCppEntity):
    """An STT engine bound to one audio.cpp model."""

    def __init__(self, entry: AudioCppConfigEntry, subentry: ConfigSubentry) -> None:
        """Initialize the entity."""
        super().__init__(entry, subentry)
        data = subentry.data
        self._streaming: bool = data.get(CONF_STREAMING, False)
        self._languages: list[str] = data[CONF_LANGUAGES]
        self._language_hint: str = data.get(CONF_LANGUAGE_HINT, LANGUAGE_HINT_NONE)
        self._prompt: str | None = data.get(CONF_PROMPT) or None

    @property
    @override
    def supported_languages(self) -> list[str]:
        """Return the languages chosen for this engine."""
        return self._languages

    # Assist captures 16 kHz, 16-bit mono PCM. That is all this engine
    # declares, since not every audio.cpp model resamples its input.
    @property
    @override
    def supported_formats(self) -> list[stt.AudioFormats]:
        """Return a list of supported formats."""
        return [stt.AudioFormats.WAV]

    @property
    @override
    def supported_codecs(self) -> list[stt.AudioCodecs]:
        """Return a list of supported codecs."""
        return [stt.AudioCodecs.PCM]

    @property
    @override
    def supported_bit_rates(self) -> list[stt.AudioBitRates]:
        """Return a list of supported bit rates."""
        return [stt.AudioBitRates.BITRATE_16]

    @property
    @override
    def supported_sample_rates(self) -> list[stt.AudioSampleRates]:
        """Return a list of supported sample rates."""
        return [stt.AudioSampleRates.SAMPLERATE_16000]

    @property
    @override
    def supported_channels(self) -> list[stt.AudioChannels]:
        """Return a list of supported channels."""
        return [stt.AudioChannels.CHANNEL_MONO]

    def _language(self, language: str) -> str | None:
        if self._language_hint == LANGUAGE_HINT_FULL:
            return language
        if self._language_hint == LANGUAGE_HINT_CODE:
            return language.split("-")[0]
        return None

    @override
    async def async_process_audio_stream(
        self, metadata: stt.SpeechMetadata, stream: AsyncIterable[bytes]
    ) -> stt.SpeechResult:
        """Transcribe the audio stream.

        Home Assistant delivers raw PCM even when the format says WAV, so the
        file path adds the header itself.
        """
        fmt = PcmFormat(metadata.sample_rate.value, metadata.channel.value)
        language = self._language(metadata.language)
        try:
            if self._streaming:
                text = await self.client.transcribe_live(
                    self.model, stream, fmt, language=language, prompt=self._prompt
                )
            else:
                pcm = bytearray()
                async for chunk in stream:
                    pcm.extend(chunk)
                text = await self.client.transcribe(
                    self.model,
                    build_wav(fmt, bytes(pcm)),
                    language=language,
                    prompt=self._prompt,
                )
        except AudioCppError as err:
            _LOGGER.error("audio.cpp STT failed with model %s: %s", self.model, err)
            return stt.SpeechResult(None, stt.SpeechResultState.ERROR)
        return stt.SpeechResult(text.strip(), stt.SpeechResultState.SUCCESS)
