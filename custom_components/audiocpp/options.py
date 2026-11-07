"""Check a TTS engine's request options against what its model declares.

Since audio.cpp v0.9.1, ``GET /v1/models?include_params=true`` lists the
request options each model's spec declares. The server only describes them
there; it validates a request when it runs. Checking here finds a typo or an
option renamed by a server upgrade (v0.9.2 renamed many) when the engine is
saved or set up, instead of when Assist next speaks.
"""

from collections.abc import Collection, Mapping
import difflib
import math
from typing import Any

from .api import RESERVED_SPEECH_KEYS, ModelInfo, ModelParam
from .const import CONF_EXTRA_OPTIONS, CONF_INSTRUCTION, CONF_INSTRUCTION_OPTION

# Top-level speech fields the server copies into the model's options, mapped
# to the option name it uses (``build_speech_request`` in the server's
# ``server_protocol.cpp``).
_COPIED_FIELDS = {
    "seed": "seed",
    "temperature": "temperature",
    "top_k": "top_k",
    "top_p": "top_p",
    "max_tokens": "max_tokens",
    "max_steps": "max_steps",
    "repetition_penalty": "repetition_penalty",
    "guidance_scale": "guidance_scale",
    "num_inference_steps": "num_inference_steps",
    "reference_text": "reference_text",
    "instructions": "instruction",
}
# Top-level fields the server reads itself. ``language`` is the text's
# language, which Chatterbox reads only from there.
_SERVER_FIELDS = frozenset({"voice", "voice_ref", "language", "return_text"})
# Copied into options as is, like ``_COPIED_FIELDS``, but only by servers that
# read it (``ModelInfo.instruct_field``).
_INSTRUCT = "instruct"
_SPEED_FIELDS = frozenset({"speed", "speaking_rate"})
_OPTIONS = "options"


def speech_extra(data: Mapping[str, Any]) -> dict[str, Any]:
    """Return an engine's extra request fields, its instruction included.

    The instruction goes in ``options`` under the name the model declares,
    which every server passes to the model as is.
    """
    extra = dict(data.get(CONF_EXTRA_OPTIONS) or {})
    if (text := data.get(CONF_INSTRUCTION)) and (
        name := data.get(CONF_INSTRUCTION_OPTION)
    ):
        options = extra.get(_OPTIONS)
        extra[_OPTIONS] = {
            **(options if isinstance(options, Mapping) else {}),
            name: text,
        }
    return extra


def _suggest(key: str, model: ModelInfo) -> str:
    names = [param.name for param in model.params or ()]
    if match := difflib.get_close_matches(key, names, n=1, cutoff=0.6):
        return f"; did you mean `{match[0]}`?"
    return ""


def _unknown(key: str, model: ModelInfo) -> str:
    return f"`{key}` is not an option of {model.id}{_suggest(key, model)}"


def _check_value(key: str, value: Any, param: ModelParam) -> str | None:
    if param.values and str(value) not in param.values:
        return f"`{key}` must be one of {', '.join(param.values)}"
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if param.min is not None and value < param.min:
        return f"`{key}` must be at least {param.min:g}"
    if param.max is not None and value > param.max:
        return f"`{key}` must be at most {param.max:g}"
    return None


def _check_option(key: str, value: Any, name: str, model: ModelInfo) -> str | None:
    if (param := model.param(name)) is None:
        return _unknown(key, model)
    return _check_value(key, value, param)


def _check_speed(key: str, value: Any, model: ModelInfo) -> str | None:
    if (param := model.speed_param) is None:
        # The server accepts 1 from any model as if speed were omitted.
        return None if value == 1 else f"{model.id} has no speed control"
    return _check_value(key, value, param)


def check_speech_options(
    model: ModelInfo, speed: float | None, extra: Mapping[str, Any] | None
) -> list[str]:
    """Return what the model's declared options say is wrong; [] if fine.

    Nothing is checked when the server does not report the options.
    """
    if model.params is None:
        return []
    problems: list[str | None] = []
    copied = dict(_COPIED_FIELDS)
    if model.instruct_field:
        copied[_INSTRUCT] = _INSTRUCT
        if extra and _INSTRUCT in extra and "instructions" in extra:
            problems.append("`instruct` and `instructions` cannot both be set")
    if speed is not None:
        problems.append(_check_speed("speed", speed, model))
    for key, value in (extra or {}).items():
        # The client sets the reserved fields itself and drops them from extra.
        if key in RESERVED_SPEECH_KEYS or key in _SERVER_FIELDS:
            continue
        if key == _OPTIONS:
            if not isinstance(value, Mapping):
                problems.append("`options` must be a mapping of option names to values")
                continue
            problems.extend(
                _check_option(name, option, name, model)
                for name, option in value.items()
            )
        elif key in _SPEED_FIELDS:
            problems.append(_check_speed(key, value, model))
        elif key in copied:
            problems.append(_check_option(key, value, copied[key], model))
        elif model.param(key) is not None:
            # The server ignores top-level fields it does not know.
            problems.append(f"`{key}` is a model option: put it inside `options`")
        else:
            problems.append(
                f"`{key}` is neither a speech request field nor an option of "
                f"{model.id}{_suggest(key, model)}"
            )
    return [problem for problem in problems if problem]


# Spec types a call may set: plain values. Path types (``path``,
# ``audio_path``, ``path_list``, ...) and lists are never offered.
_SCALAR_TYPES = frozenset({"int", "float", "bool", "string", "enum"})
# Declared options the integration sets itself, with where to set them instead.
_MANAGED = {
    "speed": "set the speed in the engine's settings",
    "speaking_rate": "set the speed in the engine's settings",
    "language": "use the language of the action call",
}
_TRUE = frozenset({"true", "on", "yes", "1"})
_FALSE = frozenset({"false", "off", "no", "0"})


def call_option_choices(model: ModelInfo) -> list[ModelParam]:
    """Return the declared options an engine may let ``tts.speak`` calls set.

    Only plain-valued options the integration does not manage itself. The
    owner still picks which of them calls may set (``CONF_CALL_OPTIONS``):
    the spec's type does not say whether a ``string`` option names a file on
    the server (``voice_samples``, ``previous_takes_file``, JSON with an
    ``audio_path``), so nothing is allowed by default.
    """
    return [
        param
        for param in model.params or ()
        if param.type in _SCALAR_TYPES and param.name not in _MANAGED
    ]


def call_option_names(model: ModelInfo, allowed: Collection[str]) -> list[str]:
    """Return the options a ``tts.speak`` call may name, in the model's order.

    These are the engine's allowed options that the model still declares
    with a plain type. Home Assistant refuses any other option before the
    engine is called.
    """
    return [param.name for param in call_option_choices(model) if param.name in allowed]


def _coerce(value: Any, param: ModelParam) -> Any:
    """Convert a call's value to the option's declared type, or raise ValueError."""
    name = param.name
    if param.type == "int":
        if isinstance(value, bool):
            raise ValueError(f"`{name}` must be a whole number")
        if isinstance(value, float) and value.is_integer():
            return int(value)
        try:
            return int(value)
        except TypeError, ValueError:
            raise ValueError(f"`{name}` must be a whole number") from None
    if param.type == "float":
        if isinstance(value, bool):
            raise ValueError(f"`{name}` must be a number")
        try:
            number = float(value)
        except TypeError, ValueError:
            raise ValueError(f"`{name}` must be a number") from None
        if not math.isfinite(number):
            raise ValueError(f"`{name}` must be a number")
        return number
    if param.type == "bool":
        if isinstance(value, bool):
            return value
        if str(value).lower() in _TRUE:
            return True
        if str(value).lower() in _FALSE:
            return False
        raise ValueError(f"`{name}` must be true or false")
    return str(value)


def call_options(
    model: ModelInfo, options: Mapping[str, Any], allowed: Collection[str]
) -> dict[str, Any]:
    """Return the model options a ``tts.speak`` call sets, converted and checked.

    Only the options :func:`call_option_names` returns are taken; other keys
    (``voice``, Home Assistant's ``preferred_*``) are not this function's
    business and are skipped. An empty value is skipped too, so a script can
    leave a field unset. Raises ValueError with a message for the caller on
    an invalid value.
    """
    names = set(call_option_names(model, allowed))
    chosen: dict[str, Any] = {}
    for name, value in options.items():
        if (
            name not in names
            or (param := model.param(name)) is None
            or value is None
            or value == ""
        ):
            continue
        value = _coerce(value, param)
        if problem := _check_value(name, value, param):
            raise ValueError(problem)
        chosen[name] = value
    return chosen
