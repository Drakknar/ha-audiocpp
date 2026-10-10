"""The audio.cpp integration.

One config entry is one audio.cpp server. Each TTS or STT engine is a
config subentry of it, bound to one model on that server.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, CONF_URL, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import AudioCppClient, AudioCppError

PLATFORMS: list[Platform] = [Platform.STT, Platform.TTS]

type AudioCppConfigEntry = ConfigEntry[AudioCppClient]


async def async_setup_entry(hass: HomeAssistant, entry: AudioCppConfigEntry) -> bool:
    """Set up an audio.cpp server from a config entry."""
    client = AudioCppClient(
        async_get_clientsession(hass),
        entry.data[CONF_URL],
        entry.data.get(CONF_API_KEY),
    )
    try:
        await client.health()
    except AudioCppError as err:
        raise ConfigEntryNotReady(str(err)) from err

    entry.runtime_data = client
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Adding, changing or removing an engine (subentry) fires this listener.
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(
    hass: HomeAssistant, entry: AudioCppConfigEntry
) -> None:
    """Reload so engines match the current subentries."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: AudioCppConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
