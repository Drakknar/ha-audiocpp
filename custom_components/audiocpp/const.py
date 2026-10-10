"""Constants for the audio.cpp integration."""

from typing import Final

DOMAIN: Final = "audiocpp"

SUBENTRY_TTS: Final = "tts"
SUBENTRY_STT: Final = "stt"

# Model ``task`` values reported by ``GET /v1/models``.
TASK_TTS: Final = "tts"
TASK_ASR: Final = "asr"
# Voice-cloning models (e.g. Chatterbox, configured with "task": "clone") are
# reported as "clon" and are served by the same /v1/audio/speech endpoint.
TASK_CLONE: Final = "clon"

# Model ``mode`` values reported by ``GET /v1/models``. Only a model the
# server runs in streaming mode accepts the streaming endpoints.
MODE_STREAMING: Final = "streaming"

CONF_LANGUAGES: Final = "languages"
CONF_STREAMING: Final = "streaming"
CONF_VOICE: Final = "voice"
CONF_SPEED: Final = "speed"
CONF_SPLIT_SENTENCES: Final = "split_sentences"
CONF_EXTRA_OPTIONS: Final = "extra_options"
CONF_SAMPLE_RATE: Final = "sample_rate"
CONF_CHANNELS: Final = "channels"
CONF_LANGUAGE_HINT: Final = "language_hint"

# How the pipeline language is passed to an ASR model. Models differ: the
# server docs show both ``en`` and ``en-US``, and a model that takes no
# language may reject the field, so the default sends nothing.
LANGUAGE_HINT_NONE: Final = "none"
LANGUAGE_HINT_CODE: Final = "code"
LANGUAGE_HINT_FULL: Final = "full"
LANGUAGE_HINTS: Final = [LANGUAGE_HINT_NONE, LANGUAGE_HINT_CODE, LANGUAGE_HINT_FULL]

# Text used to check a TTS engine at setup. It also tells us the sample
# rate and channel count, which the streaming endpoint does not report.
PROBE_TEXT: Final = "Hello."

# Every sample audio.cpp emits is 16-bit PCM.
SAMPLE_WIDTH: Final = 2
