"""Diagnostics for the audio.cpp integration."""

from dataclasses import asdict
import re
from typing import Any
from urllib.parse import urlsplit

from homeassistant.components.diagnostics import REDACTED, async_redact_data
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_API_KEY, CONF_URL
from homeassistant.core import HomeAssistant

from . import AudioCppConfigEntry
from .api import AudioCppError
from .const import CONF_EXTRA_OPTIONS

# The URL can name a host on the user's network; the key is a proxy's secret.
TO_REDACT = {CONF_API_KEY, CONF_URL}
# Extra options are free-form and can hold a reference voice, keys or tokens.
ENGINE_TO_REDACT = {CONF_EXTRA_OPTIONS}


def _redact_server(message: str, url: str) -> str:
    """Remove the server's URL and host from an error message.

    The client's errors quote the URL, and aiohttp's name the host.
    """
    for secret in (url, urlsplit(url).hostname):
        if secret:
            message = re.sub(re.escape(secret), REDACTED, message, flags=re.IGNORECASE)
    return message


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: AudioCppConfigEntry
) -> dict[str, Any]:
    """Return the server's health and models, and the engines using them.

    The models include the backend device each runs on, which tells engines
    apart on a server with several GPUs.
    """
    server: dict[str, Any]
    server_version: str | None = None
    if entry.state is not ConfigEntryState.LOADED:
        # No client, e.g. while setup retries on a busy server.
        server = {"error": "not loaded"}
    else:
        client = entry.runtime_data
        try:
            server = {
                "health": await client.health(),
                "models": [asdict(model) for model in await client.models()],
            }
        except AudioCppError as err:
            server = {"error": _redact_server(str(err), client.url)}
        server_version = client.server_version
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "server_version": server_version,
        "server": server,
        "engines": [
            {
                "type": subentry.subentry_type,
                "title": subentry.title,
                "data": async_redact_data(dict(subentry.data), ENGINE_TO_REDACT),
            }
            for subentry in entry.subentries.values()
        ],
    }
