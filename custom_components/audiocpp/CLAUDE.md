# CLAUDE.md

Context for picking this project up later. The user-facing docs are in README.md; this file covers how the integration was built, what was verified, and what is still open.

## What this is

`audiocpp` is a Home Assistant custom integration, distributed through HACS. It exposes the TTS and ASR models of an [audio.cpp](https://github.com/0xShug0/audio.cpp) server (`audiocpp_server`) as Home Assistant `tts` and `stt` entities, which Assist can use. Each engine can use the streaming or the non-streaming endpoints.

## Layout

This folder (`custom_components/audiocpp/`) is self-contained: it holds the code, these notes and the tests, so it can be copied or re-imported as one unit. Only `hacs.json` and `README.md` sit two levels up, at the repository root, because HACS needs them there.

```
<repo root>/
  hacs.json, README.md            HACS metadata (min HA 2026.9.0) and user docs
  custom_components/audiocpp/     <- this folder
    __init__.py   Entry setup: builds the client, checks /health (ConfigEntryAuthFailed on 401/403,
                  ConfigEntryNotReady on other failures),
                  raises/clears engine-option repair issues, forwards STT+TTS, update listener
                  reloads the entry on any subentry change
    api.py        AudioCppClient (aiohttp), SSE parser, WAV helpers, URL normalisation, errors,
                  ModelInfo/ModelParam (declared request options)
    options.py    check_speech_options: speed + extra_options vs the model's declared options
    config_flow.py  Server flow (user/reconfigure/reauth) + subentry flows "tts" and "stt"
    entity.py     Base entity: one device per subentry, unique_id = subentry_id
    tts.py        TTS entity, sentence splitter
    stt.py        STT entity
    const.py, manifest.json, strings.json, translations/en.json
    CLAUDE.md     These notes
    pytest.ini    Test config (adds the repo root to the path, importlib import mode)
    tests/
      fake_server.py      aiohttp server implementing the audio.cpp wire format
      test_integration.py End-to-end tests (flows + entities) against the fake server
```

Home Assistant ignores `CLAUDE.md`, `pytest.ini` and `tests/` when the folder is installed; it only imports the integration modules.

## Data model

- **Config entry:** one audio.cpp server. Data is `url` (normalized) and an optional `api_key`.
- **Subentry `tts`:** `model`, `languages`, `streaming`, `split_sentences`, and the optional `voice`, `speed` and `extra_options` (dict). Also `sample_rate` and `channels`, which the probe fills in.
- **Subentry `stt`:** `model`, `languages`, `streaming`, `language_hint` (`none`/`code`/`full`), and an optional `prompt`.
- **Title:** the engine name the user enters becomes the subentry title (`CONF_NAME` is removed from the stored data).

## Findings: audio.cpp server API

These come from the repository source (commit `c6b104e`, 2026-10-04), mainly `app/server/README.md` and `app/server/runtime.cpp`. Rechecked against `v0.9.1` (2026-10-12): none of the items below changed.

- **`GET /health`:** returns `{"status":"ok","backend":..,"models":N,...}`.
- **`GET /v1/models`:** returns `{"data":[{id, family, task, mode, loaded, path}]}`.
  - Since v0.9.1, `?include_params=true` adds `params`: the request options the model's spec declares (`name`, `type`, `required`, `description`, `default`/`min`/`max`, enum `values`). A model declaring none returns `[]`; older servers omit the key (`ModelInfo.params` is then None). The list describes options and does not validate them. The client always sends the flag.
  - **Which request keys map to options** (`build_speech_request` in `server_protocol.cpp`, v0.9.2): `options.*` go to the model as is. The top-level `seed`, `temperature`, `top_k`, `top_p`, `max_tokens`, `max_steps`, `repetition_penalty`, `guidance_scale`, `num_inference_steps` and `reference_text` are copied into options under the same name, and `instructions` becomes `instruction`. `speed`/`speaking_rate` goes to whichever of the two the model declares. `voice`, `voice_ref` and `language` (the text's language) are read by the server. Any other top-level key is silently ignored, which is why `options.py` says "put it inside `options`" for a declared option sent at the top level.
  - Checked on the author's live server (2026-10-17): `omnivoice` declares `speed` (no min/max), `text_chunk_size`, `guidance_scale` and others; `nemotron-stream` declares its ASR options.
  - `task` is a token: `tts` and `asr` are the two used here; others include `vad`, `align`, `diar` and `clone`.
  - `mode` is `offline` or `streaming`.
- **`GET /v1/audio/voices?model=`:** returns `{"voices":[...]}`, which includes the preset names from `server.json` and the `voice_dir` voices.
- **`POST /v1/audio/speech`:**
  - **Response formats:**
    - Default: a 16-bit WAV.
    - `response_format: json`: base64 WAV wrapped in JSON.
    - `mp3`: only with an optional frontend module compiled in.
  - **Streaming:** a request containing `stream_format` (or `stream: true`) streams. This needs a model with `mode: streaming` and `response_format: pcm`.
    - SSE events: `{"type":"speech.audio.delta","audio":"<b64 s16le PCM>"}`, then `speech.audio.done`, then `data: [DONE]`.
    - The stream does not report the sample rate or channel count. This is why the TTS flow probes.
    - `stream_format: audio` gives raw chunked PCM instead (not used).
  - **Non-streaming on a streaming model:** works. The server runs the streaming path and returns the full WAV, and the probe relies on this.
  - **Speed:** `speed`/`speaking_rate` is accepted only by models with speed control; other models reject it. Since v0.9.1, `speed: 1` is accepted as omitted by models without speed control, and an explicit value (including 1) overrides the server's configured request defaults. Clone and voice-design models now get speed support too when their spec declares it.
  - **Voice resolution order:** `voice_ref` > a preset matching `voice` > a `voice_dir` wav > the model-native voice id. When `voice` is omitted, `default_voice_preset` is injected.
  - **VoxCPM2 streaming:** requires `options.retry_badcase=false`. The flow suggests this through `extra_options`.
- **`POST /v1/audio/transcriptions`:**
  - Takes JSON with a server-side path, or multipart with `file`, `model`, and the optional `language`, `prompt` and `stream`.
  - The upload filename must end in `.wav`. The response is `{"text","timing"}`.
- **`POST /v1/audio/transcriptions/live`:**
  - The body is raw PCM sent with `Transfer-Encoding: chunked`. Parameters go in the query: `model`, `sample_rate`, `channels`, `sample_format=s16le`, `language` and `prompt`.
  - Response headers are sent before the body is read, so the client must be full duplex. aiohttp with an async-generator body handles this.
  - SSE events: `transcript.text.delta`, then `transcript.text.done {text}`, then `[DONE]`.
  - Closing the connection without the terminating chunk is treated as an error.
  - The model must be in streaming mode.
- **Errors:**
  - HTTP errors: `{"error":{"message","type"}}`. 503 means `server_busy` or `insufficient_memory`.
  - After SSE headers have been sent, an error arrives as `data: {"type":"error","error":{"message":..}}`.
- **Authentication:** none in audio.cpp. The `api_key` field only sends `Authorization: Bearer` for a reverse proxy that requires it.
- **Concurrency:** one request per model at a time unless the server runs with `--parallel-jobs`.

## Findings: Home Assistant APIs

Verified against the source of the 2026.9.4 and 2026.10.1 tags and by running the tests on both.

- **TTS streaming input:** `TextToSpeechEntity.async_supports_streaming_input()` is true when `async_stream_tts_audio` is overridden. Assist then streams LLM text through `TTSAudioRequest.message_gen`.
  - The entity returns `TTSAudioResponse("wav", data_gen)`.
  - It also implements `async_get_tts_audio`, which returns a complete WAV.
- **Streamed WAV:** the header has 0 frames, the same convention as `wyoming/tts.py`. ffmpeg reads to the end of the data.
- **STT input:** `async_process_audio_stream` receives raw PCM even when `metadata.format` is WAV, so the non-streaming path adds the header itself. Assist sends 16 kHz, 16-bit mono, and that is all the entity declares.
- **Subentry reloads:** adding, updating or removing a subentry fires the entry's update listeners, which reload the entry.
  - Because of this, the flows update and abort without reloading (`async_update_and_abort`, or its equivalent by hand in the subentry flows; see below).
  - `async_update_reload_and_abort` with an update listener logs a deprecation warning that becomes breaking in 2026.12.
- **Abort wording (2026.10):** `async_update_and_abort` without `reason` now aborts with `translation_domain="homeassistant"`, so HA's generic "reconfigure successful" text replaces ours ([#180723](https://github.com/home-assistant/core/pull/180723), [#181225](https://github.com/home-assistant/core/pull/181225)).
  - The server flow passes `reason="reconfigure_successful"` (and `"reauth_successful"`) explicitly, which keeps `strings.json`.
  - The subentry `async_update_and_abort` only gained `reason` in 2026.10, so the subentry flows call `async_update_subentry` and `async_abort` themselves to stay compatible with 2026.9.
- **Voluptuous:** since 2026.9, HA aliases `voluptuous` to probatio (`homeassistant/__init__.py`), and 2026.10 bans it in core. `import voluptuous as vol` still works for custom integrations, with no warning.
- **`LanguageSelector`:** single choice only. Languages therefore use `SelectSelector(multiple=True, custom_value=True)`, validated with a BCP-47-like regex.
- **References:** `homeassistant/components/openai_conversation` (`stt.py`, `tts.py`, `config_flow.py`) is the closest in-tree example of TTS/STT subentries.

## Design decisions

- **Probe on TTS save:** synthesize `"Hello."` without streaming. This validates the voice, speed and extra options, and stores `sample_rate` and `channels` for streaming. The cost is one inference, plus a model load if the server uses `lazy_load`.
- **Sentence splitting:** on by default, in both modes. A sentence ends at terminal punctuation followed by whitespace, at CJK punctuation, or at a blank line. Fragments under 20 characters are merged into the next sentence. Without streaming, each sentence's WAV must have the same format as the first, or the request raises an error.
- **Reserved request fields:** `extra_options` cannot override `model`, `input`, `stream`, `stream_format` or `response_format`.
- **Declared options (issues #2 and #4):** with a v0.9.1+ server, the TTS speed field appears only when the model declares `speed` or `speaking_rate`, bounded by the declared min/max (fallback 0.25–4.0, and a min of 0 or less is ignored because speed must be positive). Clone models get it the same way. A typed-in model the server doesn't list, or an older server, gets the old always-on field.
  - **Save check:** before the probe, `check_speech_options` flags unknown option names (with a difflib suggestion), declared options sent at the top level, enum values not in `values`, and numbers outside min/max. It runs only when the server reports params.
  - **Override:** because `params` only describes options, submitting the flagged speed and extra options again unchanged skips the check (`TTSSubentryFlow._flagged`), and the probe decides.
  - **Repair issue:** every entry setup (and so every reload or subentry change) runs the same check on each TTS subentry that sets speed or extra options. Models are listed only when at least one such engine exists. Problems raise the non-fixable warning `engine_options_<entry_id>_<subentry_id>` (translation key `engine_options`). Issues for engines that are fine again or removed are deleted, and `async_remove_entry` clears them all. A failed model listing only logs at debug level and never blocks setup.
  - **Not done:** turning declared options into proper form fields instead of free-form `extra_options` (the "Further" item of issue #2).
- **API key (owner's decision, 2026-10-24):**
  - **Reconfigure:** a blank key field keeps the stored key only while the URL's host and port (scheme default if none) stay the same, so the key is never sent to a different server. For a different host or port no key is stored, and a 401/403 from the new server shows `api_key_for_new_server`, which asks for its key.
  - **Reauth:** a 401/403 from `/health` at setup raises `ConfigEntryAuthFailed`. `reauth_confirm` asks for a new key, checks it against the stored URL, and stores it. A failed entry has no update listener, so the flow uses `async_update_reload_and_abort`; an entry that is still loaded (reauth started by hand) uses `async_update_and_abort` and its update listener reloads it, which avoids the 2026.12 deprecation noted above.
- **Language hint:** the default `language_hint` is `none`, because some models reject a `language` field. The audio.cpp docs show `en` for multipart and `en-US` for Nemotron live.
- **Minimum version:** set to 2026.9.0, the oldest version verified (2026.9.4 and 2026.10.1 are tested). Older versions with subentries and TTS streaming may work, but this is untested.
- **SSE parsing:** done by hand rather than with `readline`, because a base64 audio line can exceed aiohttp's line-length limit.

## Verification status

- **Automated tests:** `pytest` gives 7 passed against HA 2026.9.4 and 2026.10.1 on Python 3.14 (2026-10-12; `test_tts_through_home_assistant` needs an `ffmpeg` binary on PATH and is skipped without one). The tests use a fake server that follows the documented wire format, including full-duplex live STT.
  - **Declared options (2026-10-17):** 3 new tests (speed field, save check, repair issue). The fake server reports `PARAMS` per model, and `FakeAudioCpp.params = None` acts as a pre-v0.9.1 server. 9 passed, ffmpeg test skipped, on HA 2026.10.1 on the Windows PC.
  - **Windows runs:** use stub `fcntl.py`/`resource.py` in site-packages and a `-p` plugin that turns `pytest_socket.disable_socket` into a no-op. The hass fixture's teardown then raises `WinError 10022` on some tests, which is an environment issue.
- **Code review (2026-10-05):** found 2 issues, both fixed and covered by tests. (1) Reconfiguring the server with a blank API key dropped the stored key; a blank key now keeps it (since 2026-10-24 only for the same host and port). (2) Credentials in the URL leaked into the entry title and logs; `normalize_url` now rejects them.
- **Bug found in real use (2026-10-05): "TTS engine name is not set".**
  - **Cause:** the entity used `has_entity_name=True` with `_attr_name=None`, so `entity.name` was None, and HA's TTS manager refuses such an engine. Every `tts.speak` failed, and Assist played no audio.
  - **Fix:** `has_entity_name=False` with `_attr_name = subentry.title`, as HA core's OpenAI TTS does. Entity ids are unchanged, since they live in the entity registry.
  - **Why the tests missed it:** they called the entity directly. `test_tts_through_home_assistant` now goes through `tts.async_create_stream`, the manager path.
  - **Real-server check:** passed through HA's TTS manager against the real OmniVoice server, for en and it, in default mp3, mp3, flac, and streamed-message modes.
  - **Test gotcha:** the harness's `testing_config/tts` file cache is shared across tests and runs. Turn it off with `hass.data[DATA_TTS_MANAGER].use_file_cache = False`, or stale clips get served.
- **Lint:** ruff (E,F,W,B,UP,SIM,ASYNC) is clean apart from one stylistic SIM105 note in `api.py`.
- **Real use (2026-10-10):** installed in the author's Home Assistant (2026.10.0) and tested by the author against their real `audiocpp_server`.

## Chatterbox notes (2026-10-05)

- **Task:** the Chatterbox session only accepts the voice-cloning (`clone`) or `vc` task, so `"task": "tts"` fails. It is offline only.
- **Language:** read only from the request's top-level `language` field. `default_request_options` cannot set it, because those go into the options map, and voice presets carry only `voice_id`, `voice_ref` and `reference_text`. An Italian engine therefore needs `extra_options: {"language": "it"}`.
- **reference_text:** injected only if the model's spec accepts it, so presets written for Pocket TTS are harmless.
- **Speed:** not supported.
- **Example config:** the author keeps a Chatterbox variant of the server config as `server.chatterbox.json`.

## Open items / TODO

- **Repository:** published as `Drakknar/ha-audiocpp` (MIT). `manifest.json` links there, and `codeowners` is `@Drakknar`.
- **Test against a real server:** the author's `server.json` defines:
  - `pocket-tts-english` and `pocket-tts-italian`: tts, streaming, with voice presets `kitt_eng`/`kitt_ita` set as `default_voice_preset`.
  - `nemotron-stream`: asr, streaming.

  The author's HA was 2026.9.4 at the time.
- **OmniVoice variant:** `server.omnivoice.json`, running on the author's server since then (2026-10-17: engines `omnivoice-eng`/`omnivoice-ita` on `omnivoice`, STT on `nemotron-stream`).
  - One `omnivoice` entry (task `tts`, mode `streaming`) holds both KITT presets, plus `nemotron-stream`.
  - **Language:** OmniVoice auto-detects it when the request omits `language`, so one entry serves both English and Italian.
  - **Pseudo-streaming:** it emits one SSE delta per text chunk only when `text_chunk_size` is set, so `default_request_options` sets it to 160.
  - **Speed:** supported.
  - **Verified:** the GGUF filename `omnivoice-q8_0.gguf` matches the package metadata.
  - **Since then, the author's own `server.json`** switched Pocket TTS to `-bf16.gguf` files in `offline` mode.
- **Possible improvements:**
  - Diagnostics.
  - More STT sample rates.
  - Filtering voices by language.
  - Translations other than English.

## Running the tests

```bash
# Python 3.14 is needed for HA 2026.9.x and 2026.10.x
pip install homeassistant==2026.10.1 pytest-homeassistant-custom-component==0.13.372 mutagen ha-ffmpeg
cd custom_components/audiocpp && pytest
```

`pytest.ini` puts the repository root on the path, so the folder must sit inside a `custom_components/` directory (the repository layout, or a Home Assistant config) for the imports to resolve. The tests use the harness's `socket_enabled` fixture because the fake server is real loopback HTTP.

## Upstream watch

`.github/workflows/compat-review.yml` runs daily. For each new version of a dependency, Claude reviews this integration once, following `.github/prompts/ha-compat.md`.
- **Home Assistant:** every stable release is reviewed, patch releases included, and so is the first beta of each minor release.
- **audio.cpp:** each published release is reviewed against the API notes above. Commits to its default branch are not reviewed until a release ships them.
- **Results:** a fix comes as a pull request; anything that needs the owner, as an issue labelled `upstream-watch` and assigned to the owner; new features worth using, as an `enhancement` issue.
- **Access:** Claude works with read-only access (secret `CLAUDE_CODE_OAUTH_TOKEN`). A separate job opens the pull request and issues.
- **Isolation (tested 2026-10-11):**
  - **Before Claude runs:** the review job turns on the action's subprocess secret scrub and installs bubblewrap, so Claude's commands don't see the Claude token or any other process. It also blocks reading other processes' memory and removes sudo, and Docker is switched off (Docker access amounts to root).
  - **After Claude:** the steps that follow ignore variables and PATH entries its commands could plant, and make the diff in a fresh clone.
  - **Publishing:** the report job won't publish output that contains the token, or a piece of it, and deletes the review's artifact.
  - **What's left:** Claude's commands never see the token, so leaking it would take a way around the sandbox. The scan is a second line of defence.
- **Bookkeeping:** `.github/scripts/watch.sh` records what was reviewed on the `watch-state` branch. Run the workflow by hand with `force` to review again.
