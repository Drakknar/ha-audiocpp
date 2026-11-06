# CLAUDE.md

Context for picking this project up later. The user-facing docs are in README.md; this file covers how the integration was built, what was verified, and what is still open.

## What this is

`audiocpp` is a Home Assistant custom integration, distributed through HACS. It exposes the TTS and ASR models of an [audio.cpp](https://github.com/0xShug0/audio.cpp) server (`audiocpp_server`) as Home Assistant `tts` and `stt` entities, which Assist can use. Each engine can use the streaming or the non-streaming endpoints.

## Layout

This folder (`custom_components/audiocpp/`) is self-contained: it holds the code, these notes and the tests, so it can be copied or re-imported as one unit. Only `hacs.json` and `README.md` sit two levels up, at the repository root, because HACS needs them there.

```
<repo root>/
  hacs.json, README.md            HACS metadata (min HA 2026.11.0) and user docs
  custom_components/audiocpp/     <- this folder
    __init__.py   Entry setup: builds the client, checks /health (ConfigEntryAuthFailed on 401/403,
                  ConfigEntryNotReady with retry_after=60 on 503, ConfigEntryNotReady otherwise),
                  refreshes engine-option repair issues, forwards STT+TTS, update listener
                  reloads the entry on any subentry change
    api.py        AudioCppClient (aiohttp), SSE parser, WAV helpers, URL normalisation, errors,
                  ModelInfo/ModelParam (declared request options)
    options.py    check_speech_options: speed + extra_options vs the model's declared options;
                  speech_extra: extra_options with the instruction merged into `options`
    diagnostics.py  Server health + models (device/configured), engines; URL (also in errors),
                  key and extra_options redacted; "not loaded" without network calls
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
- **Subentry `tts`:** `model`, `languages`, `streaming`, `split_sentences`, and the optional `voice`, `speed`, `instruction` and `extra_options` (dict). With `instruction` comes `instruction_option`, the option name the model declares for it (`instruct` or `instruction`). Also `sample_rate` and `channels`, which the probe fills in.
- **Subentry `stt`:** `model`, `languages`, `streaming`, `language_hint` (`none`/`code`/`full`), and an optional `prompt`.
- **Title:** the engine name the user enters becomes the subentry title (`CONF_NAME` is removed from the stored data).

## Findings: audio.cpp server API

These come from the repository source (commit `c6b104e`, 2026-10-04), mainly `app/server/README.md` and `app/server/runtime.cpp`. Rechecked against `v0.9.1` (2026-10-12): none of the items below changed. Rechecked against `v0.9.2-drama3-base-small` (2026-10-19): only additions, noted below.

- **`GET /health`:** returns `{"status":"ok","backend":..,"models":N,...}`. Since v0.9.3 also `version`, `git` and `git_date`; the client keeps `"<version> (<git[:8]>)"` as `server_version`, shown as each engine device's `sw_version`.
- **`GET /v1/models`:** returns `{"data":[{id, family, task, mode, loaded, path}]}`.
  - Since `v0.9.2-drama3-base-small`, entries also carry `device` (the effective backend device index) and `configured` (false for a model loaded through `POST /v1/models/load`). `/health` reported no version before v0.9.3 (v0.9.3+ adds `version`, `git` and `git_date`), so `ModelInfo.instruct_field` uses the presence of `configured` to recognise that server.
  - Since v0.9.1, `?include_params=true` adds `params`: the request options the model's spec declares (`name`, `type`, `required`, `description`, `default`/`min`/`max`, enum `values`). A model declaring none returns `[]`; older servers omit the key (`ModelInfo.params` is then None). The list describes options and does not validate them. The client always sends the flag.
  - **Which request keys map to options** (`build_speech_request` in `server_protocol.cpp`, v0.9.2): `options.*` go to the model as is. The top-level `seed`, `temperature`, `top_k`, `top_p`, `max_tokens`, `max_steps`, `repetition_penalty`, `guidance_scale`, `num_inference_steps` and `reference_text` are copied into options under the same name, and `instructions` becomes `instruction`. Since `v0.9.2-drama3-base-small`, a top-level `instruct` goes to the option `instruct`, and sending both `instruct` and `instructions` is a 400. `options.py` accepts a top-level `instruct` only on such a server, and flags the pair. `speed`/`speaking_rate` goes to whichever of the two the model declares. `voice`, `voice_ref` and `language` (the text's language) are read by the server. Any other top-level key is silently ignored, which is why `options.py` says "put it inside `options`" for a declared option sent at the top level.
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
  - **Voice presets:** since `v0.9.2-drama3-base-small` they can carry `options`, which fill the request options the request does not set.
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

Verified against the source of the 2026.9.4 and 2026.10.1 tags and by running the tests on both. Rechecked against 2026.11.0 (2026-11-04): none of the modules used here changed between 2026.11.0b0 and 2026.11.0, and the tests pass on it.

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
  - The engine subentry flows do the same with the subentry `async_update_and_abort`, whose `reason` argument is new in 2026.10 (the minimum since 2026-10-30).
- **Validation:** core moved to probatio in 2026.10 and bans voluptuous; the config flow imports `probatio` directly, as core does (since 2026-10-30; `.ruff.toml` bans voluptuous too). `import voluptuous as vol` would still work, aliased to probatio's shim.
- **`LanguageSelector`:** single choice only. Languages therefore use `SelectSelector(multiple=True, custom_value=True)`, validated with a BCP-47-like regex.
- **References:** `homeassistant/components/openai_conversation` (`stt.py`, `tts.py`, `config_flow.py`) is the closest in-tree example of TTS/STT subentries.

## Design decisions

- **Probe on TTS save:** synthesize `"Hello."` without streaming. This validates the voice, speed and extra options, and stores `sample_rate` and `channels` for streaming. The cost is one inference, plus a model load if the server uses `lazy_load`.
- **Sentence splitting:** on by default, in both modes. A sentence ends at terminal punctuation followed by whitespace, at CJK punctuation, or at a blank line. Fragments under 20 characters are merged into the next sentence. Without streaming, each sentence's WAV must have the same format as the first, or the request raises an error.
- **Reserved request fields:** `extra_options` cannot override `model`, `input`, `stream`, `stream_format` or `response_format`.
- **Declared options (issues #2 and #4):** with a v0.9.1+ server, the TTS speed field appears only when the model declares `speed` or `speaking_rate`, bounded by the declared min/max (fallback 0.25–4.0, and a min of 0 or less is ignored because speed must be positive). Clone models get it the same way. A typed-in model the server doesn't list, or an older server, gets the old always-on field.
  - **Save check:** before the probe, `check_speech_options` flags unknown option names (with a difflib suggestion), declared options sent at the top level, enum values not in `values`, and numbers outside min/max. It runs only when the server reports params.
  - **Override:** because `params` only describes options, submitting the flagged speed and extra options again unchanged skips the check (`TTSSubentryFlow._flagged`), and the probe decides.
  - **Repair issue:** every entry setup (and so every reload or subentry change) runs the same check on each TTS subentry that sets speed, an instruction or extra options. Models are listed only when at least one such engine exists. Problems raise the non-fixable warning `engine_options_<entry_id>_<subentry_id>` (translation key `engine_options`), linked to the entry with `config_entry_id` (HA 2026.11). The check runs inside `ir.async_refresh_issues` matched by that id prefix, so issues of engines that are fine again or removed go, a user's "ignore" on valid ones stays, and unlinked issues from before 0.4.0 are cleaned up too. HA removes linked issues with the entry and hides them while it is disabled. `async_remove_entry` remains only to delete the unlinked pre-0.4.0 ids of an entry removed without ever loading under 0.4.0; it can go after a couple of releases. A failed model listing only logs at debug level, never blocks setup, and starts no refresh, so existing issues stay.
  - **Not done:** turning declared options into proper form fields instead of free-form `extra_options` (the "Further" item of issue #2).
- **API key (owner's decision, 2026-10-24):**
  - **Reconfigure:** a blank key field keeps the stored key only while the URL's host and port (scheme default if none) stay the same, so the key is never sent to a different server. For a different host or port no key is stored, and a 401/403 from the new server shows `api_key_for_new_server`, which asks for its key.
  - **Reauth:** a 401/403 from `/health` at setup raises `ConfigEntryAuthFailed`. `reauth_confirm` asks for a new key, checks it against the stored URL, and stores it. A failed entry has no update listener, so the flow uses `async_update_reload_and_abort`; an entry that is still loaded (reauth started by hand) uses `async_update_and_abort` and its update listener reloads it, which avoids the 2026.12 deprecation noted above.
- **Language hint:** the default `language_hint` is `none`, because some models reject a `language` field. The audio.cpp docs show `en` for multipart and `en-US` for Nemotron live. Since v0.9.4 ([#1190](https://github.com/0xShug0/audio.cpp/pull/1190)) Nemotron Transcribe (family `nemotron_transcribe`) maps regional tags (`en-US`, `pt_BR`) to a language name itself; the model first appeared in a release in v0.9.4, but `main` builds from before #1190 (the model landed on 2026-10-31) map only an exact code (`en`) and pass `en-US` through unmapped (`language_name` in `src/community_models/nemotron_transcribe/session.cpp`). The STT form therefore defaults to `code` for that family: on v0.9.4 `full` gains nothing over it.
- **Instruction (issue #7):** shown only when the model declares `instruct` or `instruction` (Qwen3-TTS, VoxCPM2 voice design), or, for a model the server doesn't describe, when one was stored. It is sent inside `options` under the declared name, which every server passes to the model as is. A field value wins over the same key in `extra_options.options`. A top-level `instruct`/`instructions` in the extra options is different: the server applies it after `options` (`instruct` sets the option `instruct`, `instructions` the option `instruction`; `build_speech_request`, v0.9.4), so it overrides the field when it maps to the declared name. The repair check covers it through `speech_extra`.
- **Voice preset options:** since `v0.9.2-drama3-base-small` a preset can carry `options`. The README points there for per-voice settings such as VoxCPM2's `retry_badcase`; the flow still suggests the extra option.
- **Busy server:** a 503 from `/health` at setup (`server_busy`/`insufficient_memory`) raises `ConfigEntryNotReady(retry_after=60)`. HA keeps its backoff as the floor, so retries come after at least a minute instead of 5 s. audio.cpp sends no `Retry-After` header.
- **Partial transcripts:** the STT entity implements `async_process_audio_stream_with_progress` (HA 2026.11). The live endpoint's deltas are yielded as `PartialSpeechResult` with the transcript so far; the file path yields only the final result. `async_process_audio_stream` is not implemented: HA calls entities only through `internal_async_process_audio_stream(_with_progress)`. No Assist pipeline code read partials in 2026.11.0, so the payoff comes when the pipeline or frontend does.
- **Diagnostics:** `/health`, `/v1/models` (with `device`/`configured`), `server_version` and each engine's data. The URL and API key are redacted.
- **Minimum version:** 2026.11.0 (raised from 2026.10.0 on 2026-11-06 for the issue-registry refresh, `config_entry_id` on issues, `PartialSpeechResult` and `retry_after`; from 2026.9.0 on 2026-10-30 for probatio). CI then runs one harness, core's `pytest-homeassistant`, at the version it reads from `hacs.json`; add a second venv again once the minimum falls behind the current release.
- **Manifest metadata (issue #25):** `categories: ["ai_voice.speech"]` and `connectivity: ["network"]`, as core's speech integrations use them for the new marketplace listing.
- **SSE parsing:** done by hand rather than with `readline`, because a base64 audio line can exceed aiohttp's line-length limit.

## Verification status

- **Automated tests:** `pytest` gives 7 passed against HA 2026.9.4 and 2026.10.1 on Python 3.14 (2026-10-12; `test_tts_through_home_assistant` needs an `ffmpeg` binary on PATH and is skipped without one). The tests use a fake server that follows the documented wire format, including full-duplex live STT.
  - **Declared options (2026-10-17):** 3 new tests (speed field, save check, repair issue). The fake server reports `PARAMS` per model, and `FakeAudioCpp.params = None` acts as a pre-v0.9.1 server. 9 passed, ffmpeg test skipped, on HA 2026.10.1 on the Windows PC.
  - **audio.cpp `v0.9.2-drama3-base-small` (2026-10-19):** the fake server lists `device`/`configured`, and rejects a top-level `instruct` sent together with `instructions` (`FakeAudioCpp.instruct_field = False` acts as an older server). 1 new test. 10 passed, ffmpeg test skipped, on HA 2026.10.1 on Python 3.14. Rebased onto 0.3.0 (2026-10-27): 15 passed, ffmpeg test skipped.
  - **0.4.0 (2026-11-06):** 5 new tests (instruction field, repair issue removed with the entry, busy retry, server version + diagnostics, Nemotron Transcribe hint), the legacy cleanup and entry link in the existing repair issue test, and partials in the live STT test. 20 passed, ffmpeg test skipped, on HA 2026.11.0 with `pytest-homeassistant` 2026.11.0 from PyPI on the Windows PC; that run predates 171b253 (instruction kept after a form error) and was not repeated. The later diagnostics tests (redacted error, entry not loaded) and the whitespace-only delta in the live STT test have not been run yet either. That harness has no option to fail tests on deprecated-API reports for custom integrations (checked 2026-11-06), so issue #25's second idea isn't possible yet.
  - **HA 2026.11.0 (2026-11-04):** `pytest-homeassistant-custom-component` stops at 0.13.375 (HA 2026.10.4) and fails to import on 2026.11, because core removed a private logger helper it imports. Its README now calls it broken and points to core's own `pytest-homeassistant` ([#190049](https://github.com/home-assistant/core/pull/190049)). `tests/conftest.py` loaded the old harness by name when `pytest_homeassistant` wasn't installed (dropped in 0.4.0 with the 2026.10 run). It also points `hass_config_dir` at `hass_tmp_config_dir`, because the new `hass` fixture uses `tests/testing_config` without creating it, and the TTS cache then fails to initialise. `pytest.ini` adds `-p no:respx` so HA can alias `httpx` to `httpx2` before respx imports it. 16 passed (ffmpeg test included) on HA 2026.11.0 with `pytest-homeassistant` installed from the core tag, and on HA 2026.10.4 with 0.13.375.
  - **Windows runs:** use stub `fcntl.py`/`resource.py` in site-packages and a `-p` plugin that turns `pytest_socket.disable_socket` into a no-op. The hass fixture's teardown then raises `WinError 10022` on some tests, which is an environment issue.
- **Code review (2026-10-05):** found 2 issues, both fixed and covered by tests. (1) Reconfiguring the server with a blank API key dropped the stored key; a blank key now keeps it (since 2026-10-24 only for the same host and port). (2) Credentials in the URL leaked into the entry title and logs; `normalize_url` now rejects them.
- **Bug found in real use (2026-10-05): "TTS engine name is not set".**
  - **Cause:** the entity used `has_entity_name=True` with `_attr_name=None`, so `entity.name` was None, and HA's TTS manager refuses such an engine. Every `tts.speak` failed, and Assist played no audio.
  - **Fix:** `has_entity_name=False` with `_attr_name = subentry.title`, as HA core's OpenAI TTS does. Entity ids are unchanged, since they live in the entity registry.
  - **Why the tests missed it:** they called the entity directly. `test_tts_through_home_assistant` now goes through `tts.async_create_stream`, the manager path.
  - **Real-server check:** passed through HA's TTS manager against the real OmniVoice server, for en and it, in default mp3, mp3, flac, and streamed-message modes.
  - **Test gotcha:** the harness's `testing_config/tts` file cache is shared across tests and runs. Turn it off with `hass.data[DATA_TTS_MANAGER].use_file_cache = False`, or stale clips get served. (Since 2026-11-04 each test gets its own config dir, see the HA 2026.11.0 note above, but the tests still turn the cache off.)
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
  - More STT sample rates.
  - Filtering voices by language.
  - Translations other than English.

## Running the tests

```bash
# Python >=3.14.2 (Home Assistant 2026.11's requires-python); core's harness has the same version as homeassistant
pip install homeassistant==2026.11.0 pytest-homeassistant==2026.11.0
# mutagen and ha-ffmpeg at the versions that Home Assistant release pins:
pip install -c "$(python -c 'import importlib.util, pathlib; print(pathlib.Path(importlib.util.find_spec("homeassistant").origin).parent / "package_constraints.txt")')" mutagen ha-ffmpeg
cd custom_components/audiocpp && pytest
```

CI (`.github/workflows/checks.yml`) installs the same way with `uv`: `homeassistant` and `pytest-homeassistant` first, then `mutagen` and `ha-ffmpeg` with `-c` Home Assistant's `package_constraints.txt` (both are listed there).

The PyPI `pytest-homeassistant` 0.1.x is an unrelated older project. Don't install `pytest-homeassistant-custom-component` alongside core's harness (it stopped at HA 2026.10.4).

`pytest.ini` puts the repository root on the path, so the folder must sit inside a `custom_components/` directory (the repository layout, or a Home Assistant config) for the imports to resolve. The tests use the harness's `socket_enabled` fixture because the fake server is real loopback HTTP.

## Upstream watch

`.github/workflows/compat-review.yml` runs daily. For each new version of a dependency, Claude reviews this integration once, following `.github/prompts/ha-compat.md`.
- **Home Assistant:** every stable release is reviewed, patch releases included. Betas are not (the owner's choice, 2026-10-30).
- **audio.cpp:** each published release is reviewed against the API notes above. Commits to its default branch are not reviewed until a release ships them.
- **Results:** a fix comes as a pull request; anything that needs the owner, as an issue labelled `upstream-watch` and assigned to the owner; new features worth using, as an `enhancement` issue.
- **Access:** Claude works with read-only access (secret `CLAUDE_CODE_OAUTH_TOKEN`). A separate job opens the pull request and issues.
- **Isolation (tested 2026-10-11):**
  - **Before Claude runs:** the review job turns on the action's subprocess secret scrub and installs bubblewrap, so Claude's commands don't see the Claude token or any other process. It also blocks reading other processes' memory and removes sudo, and Docker is switched off (Docker access amounts to root).
  - **After Claude:** the steps that follow ignore variables and PATH entries its commands could plant, and make the diff in a fresh clone.
  - **Publishing:** the report job won't publish output that contains the token, or a piece of it, and deletes the review's artifact.
  - **What's left:** Claude's commands never see the token, so leaking it would take a way around the sandbox. The scan is a second line of defence.
- **Bookkeeping:** `.github/scripts/watch.sh` records what was reviewed on the `watch-state` branch. Run the workflow by hand with `force` to review again.
