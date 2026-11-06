"""Diagnostics for the audio.cpp integration."""

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_API_KEY, CONF_URL
from homeassistant.core import HomeAssistant

from . import AudioCppConfigEntry
from .api import AudioCppError

# The URL can name a host on the user's network; the key is a proxy's secret.
TO_REDACT = {CONF_API_KEY, CONF_URL}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: AudioCppConfigEntry
) -> dict[str, Any]:
    """Return the server's health and models, and the engines using them.

    The models include the backend device each runs on, which tells engines
    apart on a server with several GPUs.
    """
    client = entry.runtime_data
    server: dict[str, Any]
    try:
        server = {
            "health": await client.health(),
            "models": [asdict(model) for model in await client.models()],
        }
    except AudioCppError as err:
        server = {"error": str(err)}
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "server_version": client.server_version,
        "server": server,
        "engines": [
            {
                "type": subentry.subentry_type,
                "title": subentry.title,
                "data": dict(subentry.data),
            }
            for subentry in entry.subentries.values()
        ],
    }
