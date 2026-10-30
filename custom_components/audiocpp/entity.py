"""Base entity for audio.cpp engines."""

from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import CONF_MODEL
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN

if TYPE_CHECKING:
    from . import AudioCppConfigEntry


class AudioCppEntity(Entity):
    """An engine bound to one model on an audio.cpp server."""

    # The TTS manager refuses to run an engine whose ``name`` is None, which
    # is what ``has_entity_name`` with ``_attr_name = None`` produces. HA
    # core's own OpenAI TTS/STT entities name themselves after the
    # subentry the same way.
    _attr_has_entity_name = False
    _attr_should_poll = False

    def __init__(self, entry: AudioCppConfigEntry, subentry: ConfigSubentry) -> None:
        """Initialize the entity."""
        self._attr_name = subentry.title
        self.entry = entry
        self.subentry = subentry
        self.client = entry.runtime_data
        self.model: str = subentry.data[CONF_MODEL]
        self._attr_unique_id = subentry.subentry_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, subentry.subentry_id)},
            name=subentry.title,
            manufacturer="audio.cpp",
            model=self.model,
            entry_type=DeviceEntryType.SERVICE,
        )
