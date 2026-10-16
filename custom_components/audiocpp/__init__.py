"""The audio.cpp integration.

One config entry is one audio.cpp server. Each TTS or STT engine is a
config subentry of it, bound to one model on that server.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, CONF_MODEL, CONF_URL, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import AudioCppClient, AudioCppError
from .const import CONF_EXTRA_OPTIONS, CONF_SPEED, DOMAIN, SUBENTRY_TTS
from .options import check_speech_options

_LOGGER = logging.getLogger(__name__)

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
    await _async_check_engine_options(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Adding, changing or removing an engine (subentry) fires this listener.
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


def _issue_prefix(entry: ConfigEntry) -> str:
    return f"engine_options_{entry.entry_id}_"


async def _async_check_engine_options(
    hass: HomeAssistant, entry: AudioCppConfigEntry
) -> None:
    """Raise a repair issue for each TTS engine its model no longer accepts.

    A server upgrade can rename options (audio.cpp v0.9.2 renamed many), and
    an engine saved before it would otherwise only fail when Assist speaks.
    Issues of engines that are fine again, or gone, are removed.
    """
    try:
        models = {model.id: model for model in await entry.runtime_data.models()}
    except AudioCppError as err:
        # Not a reason to fail setup; the check runs again on the next one.
        _LOGGER.debug("Could not list models to check engine options: %s", err)
        return
    prefix = _issue_prefix(entry)
    current: set[str] = set()
    for subentry in entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_TTS:
            continue
        model = models.get(subentry.data[CONF_MODEL])
        if model is None:
            continue
        problems = check_speech_options(
            model, subentry.data.get(CONF_SPEED), subentry.data.get(CONF_EXTRA_OPTIONS)
        )
        if not problems:
            continue
        issue_id = f"{prefix}{subentry.subentry_id}"
        current.add(issue_id)
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="engine_options",
            translation_placeholders={
                "engine": subentry.title,
                "model": model.id,
                "problems": "; ".join(problems),
            },
        )
    _async_delete_issues(hass, entry, keep=current)


def _async_delete_issues(
    hass: HomeAssistant, entry: ConfigEntry, keep: set[str] | None = None
) -> None:
    prefix = _issue_prefix(entry)
    for domain, issue_id in list(ir.async_get(hass).issues):
        if domain == DOMAIN and issue_id.startswith(prefix) and issue_id not in (keep or ()):
            ir.async_delete_issue(hass, DOMAIN, issue_id)


async def _async_update_listener(
    hass: HomeAssistant, entry: AudioCppConfigEntry
) -> None:
    """Reload so engines match the current subentries."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: AudioCppConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: AudioCppConfigEntry) -> None:
    """Remove the server's repair issues with it."""
    _async_delete_issues(hass, entry)
