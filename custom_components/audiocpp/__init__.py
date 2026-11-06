"""The audio.cpp integration.

One config entry is one audio.cpp server. Each TTS or STT engine is a
config subentry of it, bound to one model on that server.
"""

import logging
import re

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, CONF_MODEL, CONF_URL, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    AudioCppAuthError,
    AudioCppBusyError,
    AudioCppClient,
    AudioCppError,
    ModelInfo,
)
from .const import (
    CONF_EXTRA_OPTIONS,
    CONF_INSTRUCTION,
    CONF_SPEED,
    DOMAIN,
    SUBENTRY_TTS,
)
from .options import check_speech_options, speech_extra

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.STT, Platform.TTS]

# Seconds before setup retries when the server answers 503 (every model busy,
# or a model load would not fit in memory). Home Assistant's own backoff
# starts at 5 seconds, which would only add to the load.
BUSY_RETRY_AFTER = 60

type AudioCppConfigEntry = ConfigEntry[AudioCppClient]


async def async_setup_entry(hass: HomeAssistant, entry: AudioCppConfigEntry) -> None:
    """Set up an audio.cpp server from a config entry."""
    client = AudioCppClient(
        async_get_clientsession(hass),
        entry.data[CONF_URL],
        entry.data.get(CONF_API_KEY),
    )
    try:
        await client.health()
    except AudioCppAuthError as err:
        # A proxy in front of the server rejects the key: ask for a new one.
        raise ConfigEntryAuthFailed(str(err)) from err
    except AudioCppBusyError as err:
        raise ConfigEntryNotReady(str(err), retry_after=BUSY_RETRY_AFTER) from err
    except AudioCppError as err:
        raise ConfigEntryNotReady(str(err)) from err

    entry.runtime_data = client
    await _async_check_engine_options(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Adding, changing or removing an engine (subentry) fires this listener.
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))


async def _async_check_engine_options(
    hass: HomeAssistant, entry: AudioCppConfigEntry
) -> None:
    """Raise a repair issue for each TTS engine its model no longer accepts.

    A server upgrade can rename options (audio.cpp v0.9.2 renamed many), and
    an engine saved before it would otherwise only fail when Assist speaks.
    The refresh removes issues of engines that are fine again, or gone, and
    keeps a user's "ignore" on those still valid. Issues belong to the entry,
    so Home Assistant removes them with it and hides them while it is
    disabled.
    """
    # Only speed, extra options and the instruction are checked, so most
    # engines need no model listing at all.
    engines = [
        subentry
        for subentry in entry.subentries.values()
        if subentry.subentry_type == SUBENTRY_TTS
        and (
            subentry.data.get(CONF_SPEED) is not None
            or subentry.data.get(CONF_EXTRA_OPTIONS)
            or subentry.data.get(CONF_INSTRUCTION)
        )
    ]
    models: dict[str, ModelInfo] = {}
    if engines:
        try:
            models = {model.id: model for model in await entry.runtime_data.models()}
        except AudioCppError as err:
            # Not a reason to fail setup; the check runs again on the next one.
            _LOGGER.debug("Could not list models to check engine options: %s", err)
            return
    prefix = f"engine_options_{entry.entry_id}_"
    # Matched by id rather than by config_entry_id, so issues raised by
    # versions before 0.4.0, which were not linked to the entry, go too.
    with ir.async_refresh_issues(hass, DOMAIN, issue_id_pattern=re.escape(prefix)):
        for subentry in engines:
            model = models.get(subentry.data[CONF_MODEL])
            if model is None:
                continue
            problems = check_speech_options(
                model,
                subentry.data.get(CONF_SPEED),
                speech_extra(subentry.data),
            )
            if not problems:
                continue
            ir.async_create_issue(
                hass,
                DOMAIN,
                f"{prefix}{subentry.subentry_id}",
                config_entry_id=entry.entry_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="engine_options",
                translation_placeholders={
                    "engine": subentry.title,
                    "model": model.id,
                    "problems": "; ".join(problems),
                },
            )


async def _async_update_listener(
    hass: HomeAssistant, entry: AudioCppConfigEntry
) -> None:
    """Reload so engines match the current subentries."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: AudioCppConfigEntry) -> None:
    """Unload a config entry."""
    await hass.config_entries.async_unload_platforms_or_raise(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: AudioCppConfigEntry) -> None:
    """Remove repair issues raised before 0.4.0, which aren't linked to the entry.

    Setup removes them too, but an entry removed without ever loading under
    0.4.0 would leave them behind. Can go after a couple of releases.
    """
    prefix = f"engine_options_{entry.entry_id}_"
    # A refresh that keeps nothing deletes every issue it matched.
    with ir.async_refresh_issues(hass, DOMAIN, issue_id_pattern=re.escape(prefix)):
        pass
