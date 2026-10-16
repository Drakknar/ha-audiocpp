"""Check a TTS engine's request options against what its model declares.

Since audio.cpp v0.9.1, ``GET /v1/models?include_params=true`` lists the
request options each model's spec declares. The server only describes them
there; it validates a request when it runs. Checking here finds a typo or an
option renamed by a server upgrade (v0.9.2 renamed many) when the engine is
saved or set up, instead of when Assist next speaks.
"""

from __future__ import annotations

from collections.abc import Mapping
import difflib
from typing import Any

from .api import RESERVED_SPEECH_KEYS, ModelInfo, ModelParam

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
_SPEED_FIELDS = frozenset({"speed", "speaking_rate"})
_OPTIONS = "options"


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
                _check_option(name, option, name, model) for name, option in value.items()
            )
        elif key in _SPEED_FIELDS:
            problems.append(_check_speed(key, value, model))
        elif key in _COPIED_FIELDS:
            problems.append(_check_option(key, value, _COPIED_FIELDS[key], model))
        elif model.param(key) is not None:
            # The server ignores top-level fields it does not know.
            problems.append(f"`{key}` is a model option: put it inside `options`")
        else:
            problems.append(
                f"`{key}` is neither a speech request field nor an option of "
                f"{model.id}{_suggest(key, model)}"
            )
    return [problem for problem in problems if problem]
