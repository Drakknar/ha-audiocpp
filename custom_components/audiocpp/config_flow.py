"""Config flow for the audio.cpp integration.

The main flow adds a server by URL. TTS and STT engines are config
subentries of that server: each picks a model from ``/v1/models`` and,
for a model the server runs in streaming mode, whether to use the
streaming endpoints.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any, override
from urllib.parse import urlsplit

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_API_KEY, CONF_MODEL, CONF_NAME, CONF_PROMPT, CONF_URL
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    ObjectSelector,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    AudioCppAuthError,
    AudioCppClient,
    AudioCppConnectionError,
    AudioCppError,
    ModelInfo,
    normalize_url,
)
from .const import (
    CONF_CHANNELS,
    CONF_EXTRA_OPTIONS,
    CONF_LANGUAGE_HINT,
    CONF_LANGUAGES,
    CONF_SAMPLE_RATE,
    CONF_SPEED,
    CONF_SPLIT_SENTENCES,
    CONF_STREAMING,
    CONF_VOICE,
    DOMAIN,
    LANGUAGE_HINT_NONE,
    LANGUAGE_HINTS,
    PROBE_TEXT,
    SUBENTRY_STT,
    SUBENTRY_TTS,
    TASK_ASR,
    TASK_CLONE,
    TASK_TTS,
)
from .options import check_speech_options

_LOGGER = logging.getLogger(__name__)

# Speed range offered when the model does not declare its own bounds.
SPEED_MIN = 0.25
SPEED_MAX = 4.0

# Offered in the language picker; any other tag can be typed in.
COMMON_LANGUAGES = [
    "en", "en-US", "en-GB", "it", "it-IT", "de", "de-DE", "fr", "fr-FR",
    "es", "es-ES", "pt", "pt-BR", "pt-PT", "nl", "nl-NL", "pl", "ru",
    "uk", "sv", "da", "nb", "fi", "cs", "el", "tr", "ar", "he", "hi",
    "ja", "ko", "zh", "zh-CN", "zh-TW",
]  # fmt: skip
_LANGUAGE_TAG = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")

SERVER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_URL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.URL)
        ),
        vol.Optional(CONF_API_KEY): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }
)


def _host_port(url: str) -> tuple[str | None, int | None]:
    """Return the host and port of a URL, with the scheme's default port."""
    parts = urlsplit(url)
    port = parts.port or {"http": 80, "https": 443}.get(parts.scheme)
    return (parts.hostname or "").lower() or None, port

REAUTH_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_API_KEY): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }
)


async def _validate_server(
    flow: ConfigFlow,
    user_input: dict[str, Any],
    current_key: str | None = None,
    current_url: str | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Check a server; return the data to store and any form errors.

    A blank API key keeps ``current_key``: the reconfigure form does not
    send the stored secret back to the browser to pre-fill it. The key is
    kept only while host and port stay those of ``current_url``, so it is
    never sent to a different server.
    """
    try:
        url = normalize_url(user_input[CONF_URL])
    except ValueError:
        return {}, {CONF_URL: "invalid_url"}
    data: dict[str, Any] = {CONF_URL: url}
    key_dropped = False
    if current_key and current_url and _host_port(url) != _host_port(current_url):
        key_dropped = True
        current_key = None
    if api_key := user_input.get(CONF_API_KEY) or current_key:
        data[CONF_API_KEY] = api_key
    client = AudioCppClient(async_get_clientsession(flow.hass), url, api_key)
    try:
        await client.health()
    except AudioCppAuthError:
        if key_dropped and not api_key:
            return data, {"base": "api_key_for_new_server"}
        return data, {"base": "invalid_auth"}
    except AudioCppConnectionError:
        return data, {"base": "cannot_connect"}
    except AudioCppError as err:
        _LOGGER.warning("audio.cpp server check failed: %s", err)
        return data, {"base": "not_audiocpp"}
    return data, {}


def _title(url: str) -> str:
    return f"audio.cpp ({urlsplit(url).netloc})"


class AudioCppConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add an audio.cpp server."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the server URL."""
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = await _validate_server(self, user_input)
            if not errors:
                self._async_abort_entries_match({CONF_URL: data[CONF_URL]})
                return self.async_create_entry(
                    title=_title(data[CONF_URL]),
                    data=data,
                )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(SERVER_SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the server URL or proxy key."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = await _validate_server(
                self, user_input, entry.data.get(CONF_API_KEY), entry.data[CONF_URL]
            )
            if not errors:
                if data[CONF_URL] != entry.data[CONF_URL]:
                    self._async_abort_entries_match({CONF_URL: data[CONF_URL]})
                # The entry's update listener reloads it. An explicit reason
                # keeps our own wording; since HA 2026.10 the default one is
                # translated by the homeassistant domain instead.
                return self.async_update_and_abort(
                    entry,
                    title=_title(data[CONF_URL]),
                    data=data,
                    reason="reconfigure_successful",
                )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                SERVER_SCHEMA, user_input or {CONF_URL: entry.data[CONF_URL]}
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start when the server (its proxy) rejects the stored API key."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a new API key and check it against the server."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = await _validate_server(
                self,
                {CONF_URL: entry.data[CONF_URL], CONF_API_KEY: user_input[CONF_API_KEY]},
            )
            if not errors:
                # An explicit reason keeps our own wording (see reconfigure).
                if entry.update_listeners:
                    # Loaded: the update listener reloads it.
                    return self.async_update_and_abort(
                        entry, data=data, reason="reauth_successful"
                    )
                return self.async_update_reload_and_abort(
                    entry, data=data, reason="reauth_successful"
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=REAUTH_SCHEMA,
            description_placeholders={"url": entry.data[CONF_URL]},
            errors=errors,
        )

    @classmethod
    @callback
    @override
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Return the engine types a server can host."""
        return {SUBENTRY_TTS: TTSSubentryFlow, SUBENTRY_STT: STTSubentryFlow}


class _EngineSubentryFlow(ConfigSubentryFlow):
    """Shared steps: pick a model, then engine settings."""

    _tasks: frozenset[str]

    def __init__(self) -> None:
        """Initialize the flow."""
        self._data: dict[str, Any] = {}
        self._models: dict[str, ModelInfo] = {}

    @property
    def _client(self) -> AudioCppClient:
        return self._get_entry().runtime_data

    @property
    def _model(self) -> ModelInfo | None:
        return self._models.get(self._data[CONF_MODEL])

    @property
    def _can_stream(self) -> bool:
        # A typed-in model the server did not list may still be streaming.
        model = self._model
        return model is None or model.streaming

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Add an engine."""
        if self._get_entry().state is not ConfigEntryState.LOADED:
            return self.async_abort(reason="entry_not_loaded")
        return await self.async_step_model()

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Change an engine."""
        if self._get_entry().state is not ConfigEntryState.LOADED:
            return self.async_abort(reason="entry_not_loaded")
        subentry = self._get_reconfigure_subentry()
        self._data = {**subentry.data, CONF_NAME: subentry.title}
        return await self.async_step_model()

    async def async_step_model(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Pick the model on the server."""
        errors: dict[str, str] = {}
        try:
            models = await self._client.models()
        except AudioCppError as err:
            _LOGGER.warning("Could not list audio.cpp models: %s", err)
            models = []
            errors["base"] = "cannot_list_models"
        self._models = {model.id: model for model in models}

        if user_input is not None:
            self._data[CONF_MODEL] = user_input[CONF_MODEL]
            return await self.async_step_settings()

        options = [
            SelectOptionDict(
                value=model.id, label=f"{model.id} ({model.family}, {model.mode})"
            )
            for model in models
            if model.task in self._tasks
        ]
        if not options and not errors:
            errors["base"] = "no_models"
        default = self._data.get(CONF_MODEL) or (options[0]["value"] if options else None)
        schema = vol.Schema(
            {
                vol.Required(CONF_MODEL, default=default)
                if default
                else vol.Required(CONF_MODEL): SelectSelector(
                    SelectSelectorConfig(
                        options=options,
                        custom_value=True,
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                )
            }
        )
        return self.async_show_form(step_id="model", data_schema=schema, errors=errors)

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Override per engine type."""
        raise NotImplementedError

    def _language_field(self) -> dict[Any, Any]:
        default = self._data.get(CONF_LANGUAGES) or [self.hass.config.language]
        options = list(dict.fromkeys([*default, *COMMON_LANGUAGES]))
        return {
            vol.Required(CONF_LANGUAGES, default=default): SelectSelector(
                SelectSelectorConfig(
                    options=options,
                    multiple=True,
                    custom_value=True,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            )
        }

    @staticmethod
    def _check_languages(languages: list[str]) -> str | None:
        if not languages or not all(_LANGUAGE_TAG.match(tag) for tag in languages):
            return "invalid_languages"
        return None

    def _keep_input(self, user_input: dict[str, Any]) -> None:
        """Keep the submitted settings to show them again with an error.

        A cleared optional field (voice, speed, extra options, prompt) is
        missing from ``user_input``, so the earlier value must not be kept
        either: the form would offer it again and the next save store it.
        """
        self._data = {CONF_MODEL: self._data[CONF_MODEL], **user_input}

    def _finish(self, data: dict[str, Any]) -> SubentryFlowResult:
        title = data.pop(CONF_NAME)
        if self.source == "user":
            return self.async_create_entry(title=title, data=data)
        # What async_update_and_abort does, but with our own wording: since
        # HA 2026.10 its default reason is translated by the homeassistant
        # domain, and its ``reason`` argument does not exist before 2026.10.
        self.hass.config_entries.async_update_subentry(
            self._get_entry(), self._get_reconfigure_subentry(), title=title, data=data
        )
        return self.async_abort(reason="reconfigure_successful")


class TTSSubentryFlow(_EngineSubentryFlow):
    """Add or change a TTS engine."""

    _tasks = frozenset({TASK_TTS, TASK_CLONE})

    def __init__(self) -> None:
        """Initialize the flow."""
        super().__init__()
        # Settings last flagged by the option check. Submitting them again
        # unchanged keeps them: the server only describes its options, so a
        # model may still take one it does not declare.
        self._flagged: tuple[Any, Any] | None = None

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Voice, languages and streaming for a TTS engine."""
        errors: dict[str, str] = {}
        placeholders = {"model": self._data[CONF_MODEL], "error": ""}
        if user_input is not None:
            data = {CONF_MODEL: self._data[CONF_MODEL], **user_input}
            data.setdefault(CONF_STREAMING, False)
            if not self._can_stream:
                data[CONF_STREAMING] = False
            extra = data.get(CONF_EXTRA_OPTIONS)
            checked = (data.get(CONF_SPEED), extra)
            if error := self._check_languages(data[CONF_LANGUAGES]):
                errors[CONF_LANGUAGES] = error
            elif extra is not None and not isinstance(extra, dict):
                errors[CONF_EXTRA_OPTIONS] = "invalid_extra_options"
            elif checked != self._flagged and (problems := self._check_options(data)):
                self._flagged = checked
                errors["base"] = "unknown_options"
                placeholders["error"] = "; ".join(problems)
            else:
                error_text = await self._probe(data)
                if error_text is None:
                    return self._finish(data)
                errors["base"] = "probe_failed"
                placeholders["error"] = error_text
            self._keep_input(user_input)

        return self.async_show_form(
            step_id="settings",
            data_schema=self.add_suggested_values_to_schema(
                await self._settings_schema(), self._suggested()
            ),
            errors=errors,
            description_placeholders=placeholders,
        )

    def _suggested(self) -> dict[str, Any]:
        suggested = dict(self._data)
        model = self._model
        # VoxCPM2 streaming requires retry_badcase=false (server README).
        if (
            CONF_EXTRA_OPTIONS not in suggested
            and model is not None
            and model.streaming
            and model.family == "voxcpm2"
        ):
            suggested[CONF_EXTRA_OPTIONS] = {"options": {"retry_badcase": False}}
        return suggested

    async def _settings_schema(self) -> vol.Schema:
        try:
            voices = await self._client.voices(self._data[CONF_MODEL])
        except AudioCppError as err:
            _LOGGER.debug("Could not list voices: %s", err)
            voices = []
        fields: dict[Any, Any] = {
            vol.Required(
                CONF_NAME, default=self._data.get(CONF_NAME, self._data[CONF_MODEL])
            ): TextSelector(),
            **self._language_field(),
            vol.Optional(CONF_VOICE): SelectSelector(
                SelectSelectorConfig(
                    options=voices, custom_value=True, mode=SelectSelectorMode.DROPDOWN
                )
            ),
        }
        if self._can_stream:
            model = self._model
            fields[
                vol.Required(
                    CONF_STREAMING,
                    default=self._data.get(
                        CONF_STREAMING, model is not None and model.streaming
                    ),
                )
            ] = BooleanSelector()
        fields[
            vol.Required(
                CONF_SPLIT_SENTENCES, default=self._data.get(CONF_SPLIT_SENTENCES, True)
            )
        ] = BooleanSelector()
        if (speed_range := self._speed_range()) is not None:
            fields[vol.Optional(CONF_SPEED)] = NumberSelector(
                NumberSelectorConfig(
                    min=speed_range[0],
                    max=speed_range[1],
                    step=0.05,
                    mode=NumberSelectorMode.BOX,
                )
            )
        fields[vol.Optional(CONF_EXTRA_OPTIONS)] = ObjectSelector()
        return vol.Schema(fields)

    def _speed_range(self) -> tuple[float, float] | None:
        """Return the speed field's bounds, or None to leave the field out.

        A server that reports options (v0.9.1+) says whether the model has
        speed control, clone models included. For an older server, or a
        model it did not list, the field is offered and the probe decides.
        """
        model = self._model
        if model is None or model.params is None:
            return SPEED_MIN, SPEED_MAX
        if (param := model.speed_param) is None:
            return None
        low = param.min if param.min is not None and param.min > 0 else SPEED_MIN
        high = param.max if param.max is not None and param.max > low else max(SPEED_MAX, low)
        return low, high

    def _check_options(self, data: dict[str, Any]) -> list[str]:
        if (model := self._model) is None:
            return []
        return check_speech_options(
            model, data.get(CONF_SPEED), data.get(CONF_EXTRA_OPTIONS)
        )

    async def _probe(self, data: dict[str, Any]) -> str | None:
        """Synthesize a short phrase with these settings.

        It catches settings the model rejects (some refuse ``speed`` or
        ``voice``) and gives the sample rate and channel count, which the
        streaming endpoint never reports. A model in streaming mode also
        answers a plain request with WAV, so one request serves both.
        """
        try:
            fmt, _ = await self._client.synthesize(
                data[CONF_MODEL],
                PROBE_TEXT,
                voice=data.get(CONF_VOICE) or None,
                speed=data.get(CONF_SPEED),
                extra=data.get(CONF_EXTRA_OPTIONS),
            )
        except AudioCppError as err:
            return str(err)
        data[CONF_SAMPLE_RATE] = fmt.sample_rate
        data[CONF_CHANNELS] = fmt.channels
        return None


class STTSubentryFlow(_EngineSubentryFlow):
    """Add or change an STT engine."""

    _tasks = frozenset({TASK_ASR})

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Languages and streaming for an STT engine."""
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {CONF_MODEL: self._data[CONF_MODEL], **user_input}
            data.setdefault(CONF_STREAMING, False)
            if not self._can_stream:
                data[CONF_STREAMING] = False
            if error := self._check_languages(data[CONF_LANGUAGES]):
                errors[CONF_LANGUAGES] = error
            else:
                return self._finish(data)
            self._keep_input(user_input)

        model = self._model
        fields: dict[Any, Any] = {
            vol.Required(
                CONF_NAME, default=self._data.get(CONF_NAME, self._data[CONF_MODEL])
            ): TextSelector(),
            **self._language_field(),
        }
        if self._can_stream:
            fields[
                vol.Required(
                    CONF_STREAMING,
                    default=self._data.get(
                        CONF_STREAMING, model is not None and model.streaming
                    ),
                )
            ] = BooleanSelector()
        fields[
            vol.Required(
                CONF_LANGUAGE_HINT,
                default=self._data.get(CONF_LANGUAGE_HINT, LANGUAGE_HINT_NONE),
            )
        ] = SelectSelector(
            SelectSelectorConfig(
                options=LANGUAGE_HINTS,
                translation_key=CONF_LANGUAGE_HINT,
                mode=SelectSelectorMode.DROPDOWN,
            )
        )
        fields[vol.Optional(CONF_PROMPT)] = TextSelector(
            TextSelectorConfig(multiline=True)
        )
        return self.async_show_form(
            step_id="settings",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(fields), self._data
            ),
            errors=errors,
            description_placeholders={"model": self._data[CONF_MODEL]},
        )
