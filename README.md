# audio.cpp for Home Assistant

Use the text-to-speech and speech-recognition models of an [audio.cpp](https://github.com/0xShug0/audio.cpp) server (`audiocpp_server`) in Home Assistant and Assist.

- **TTS engines** (`tts.*`) for any `tts` or voice-cloning model on the server, such as Pocket TTS, Kokoro, OmniVoice or Chatterbox.
- **STT engines** (`stt.*`) for any `asr` model, such as Nemotron or Parakeet.
- **Streaming both ways.** Assist starts speaking an LLM reply sentence by sentence while it is still being written, and a streaming ASR model transcribes while you are still talking, passing the transcript so far to Home Assistant as it grows.
- **Everything runs locally**, on your own audio.cpp server.

## Requirements

- Home Assistant 2026.11.0 or newer.
- A running `audiocpp_server` that Home Assistant can reach over the network. If Home Assistant runs on another machine, the server must listen on a reachable address, not `127.0.0.1`.

## Installation

### HACS

1. HACS → ⋮ → **Custom repositories**.
2. Add `https://github.com/Drakknar/ha-audiocpp` as type **Integration**.
3. Install **audio.cpp**, then restart Home Assistant.

### Manual

Copy `custom_components/audiocpp` into your Home Assistant `config/custom_components/` folder, then restart Home Assistant.

## Setup

1. **Settings → Devices & services → Add integration → audio.cpp.**
2. Enter the server URL, for example `http://192.168.1.10:8080`. An OpenAI-style URL ending in `/v1` works too.
   - **API key:** audio.cpp has no authentication of its own. Fill this in only if a reverse proxy in front of the server expects a Bearer token.
     If the proxy later rejects the key, Home Assistant asks for a new one. Reconfiguring the server to a different host or port does not carry the key over: enter it again if the new server needs one.
3. On the new server entry, choose **Add TTS engine** or **Add STT engine**. Each engine is bound to one model on the server, and you can add as many as you like.
4. Pick the engines in **Settings → Voice assistants** for your Assist pipeline.

### TTS engine settings

| Setting | What it does |
|---|---|
| Languages | The languages this engine offers to Home Assistant; the first is the default. audio.cpp doesn't report what a model speaks, so pick what your model and voice actually support. |
| Voice | A voice id or a voice preset from the server config. Leave empty to use the model's `default_voice_preset`. |
| Use the streaming endpoint | Receive audio while it is generated. Only offered for models the server runs with `mode: streaming`. |
| Synthesize sentence by sentence | Start speaking after the first sentence instead of waiting for the whole reply. On by default. |
| Speed | Speech rate multiplier. Only shown for models with speed control (clone models included), limited to the range the model declares. |
| Instruction | How the model should speak, for example "a calm, low male voice". Only shown for models that declare an `instruct` or `instruction` option, such as Qwen3-TTS or VoxCPM2 voice design. |
| Extra request options | Extra fields sent with every speech request, such as `seed` or an `options` object with model options. Settings that belong to one voice can also go in its voice preset in the server's `server.json` (audio.cpp v0.9.2 and later), which then applies to every client. |
| Options a call may set | Model options that `tts.speak` calls may set per call (see below). Empty by default, so a call can set only the voice. |

Saving a TTS engine first checks the speed, instruction and extra request options against the options the server says the model takes (audio.cpp v0.9.1 or newer). A typo or a renamed option is flagged with a suggestion, for example "`guidance` is not an option of omnivoice; did you mean `guidance_scale`?". If you know the model takes it anyway, save again without changes to keep it.

It then runs a short test synthesis ("Hello."). This catches settings the model rejects, and records the sample rate that the streaming endpoint doesn't report. The test can take a while if the server first has to load the model.

With a server older than v0.9.1, the speed field is always shown and only the test synthesis checks the settings.

### Setting model options per call

The extra request options are fixed for an engine. To change a model option from one `tts.speak` call to the next, first pick it in the engine's **Options a call may set**, then pass it in the action's `options`, next to `voice`:

```yaml
action: tts.speak
target:
  entity_id: tts.breeze
data:
  media_player_entity_id: media_player.kitchen
  message: "The oven is ready."
  options:
    voice: Maia.wav
    instruct: Say it warmly, with a light smile.
    guidance_scale: 2
    seed: 5
```

This works for anything that calls the `tts.speak` action: scripts, automations, and programs using Home Assistant's API. The voice assistant (Assist) passes the engine only its pipeline's voice, so Assist replies keep using the engine's extra request options.

The setting lists the request options the model's spec declares with a plain value (whole number, number, true/false, text or a choice), such as `seed`, `temperature`, `guidance_scale` or `instruct`. Options of a file type, and `speed` (an engine setting) and `language` (the language of the call), are never offered. **Nothing is allowed by default.** Pick only options you want every Home Assistant user to control: some models declare options that name files on the server as plain text (for example `voice_samples` in MOSS-TTSD and VibeVoice), and a caller could then point the server at any file it can read.

Values from a template or a text field arrive as text and are converted to the declared type, then checked against the minimum, maximum and allowed values the spec gives; an empty value is ignored, so a script can leave a field unset. A value in the call wins over the same option in the engine's extra request options. A value the model does not accept fails the action with a message that says what is wrong, for example "`seed` must be at least 0", before anything is sent to the server. An option that isn't allowed fails with Home Assistant's own "unsupported options" error. The list is read when the engine loads, so a call made in the first moments after Home Assistant starts may still see only `voice`. With a server older than v0.9.1, which does not list options, only `voice` is accepted.

### STT engine settings

| Setting | What it does |
|---|---|
| Languages | The languages this engine offers to Home Assistant. |
| Use the live streaming endpoint | Send audio while it is captured, so the model decodes as you speak. Only offered for models the server runs with `mode: streaming`. |
| Language sent to the model | Nothing (default), a language code (`en`), or a full tag (`en-US`). Some models reject a language field. For Nemotron Transcribe the form suggests the language code, which the server turns into a language hint for the model (audio.cpp builds before v0.9.4 pass a full tag through unmapped). |
| Recognition prompt | Optional context such as names or hotwords, for models that support it. |

## Model notes

- **VoxCPM2 streaming** needs `{"options": {"retry_badcase": false}}` in the extra request options. The form suggests it for you. With audio.cpp v0.9.2 or newer you can put it in the voice preset's `options` in `server.json` instead, and clear the extra options.
- **Chatterbox** only reads the language from the request, so an Italian engine needs `{"language": "it"}` in the extra request options. It has no streaming, and the speed field appears only if the server's model spec declares speed control.
- **OmniVoice** detects the language itself, so one engine can serve several languages. For pseudo-streaming, set `text_chunk_size` in the server's `default_request_options`.
- audio.cpp runs **one request per model at a time** unless the server is started with `--parallel-jobs`.

## Troubleshooting

- **Cannot connect:** check the URL, and that `audiocpp_server` is running and listening on an address Home Assistant can reach.
- **Test synthesis failed:** the form shows the server's own error message. Usually the model doesn't support a setting such as speed or voice.
- **Repair "TTS engine … has settings … does not accept":** each time the server entry loads, saved engines are checked against what the server says each model takes. This usually follows a server upgrade that renamed an option. Reconfigure the engine to fix it, or ignore the issue if the model does take the setting.
- **Server busy:** if the server answers 503 at startup (every model busy, or not enough memory to load one), Home Assistant retries the entry after a minute instead of within seconds.
- **Which server build an engine runs on:** each engine's device page shows the server's version and commit (audio.cpp v0.9.3 or newer). **Download diagnostics** on the server entry adds the server's health report and model list, including the backend device each model runs on; the URL (also in error messages), the API key and the engines' extra options are redacted.
- **Debug logs:** add this to `configuration.yaml`:

  ```yaml
  logger:
    logs:
      custom_components.audiocpp: debug
  ```

## Development

The tests run against a fake server that implements the audio.cpp wire format:

```bash
pip install homeassistant==2026.11.0 pytest-homeassistant==2026.11.0 mutagen ha-ffmpeg
cd custom_components/audiocpp && pytest
```

`pytest-homeassistant` is core's test harness and has the same version as `homeassistant`. Don't install the older `pytest-homeassistant-custom-component` alongside it.

Implementation notes and the audio.cpp API findings are in [`custom_components/audiocpp/CLAUDE.md`](custom_components/audiocpp/CLAUDE.md).

## License

[MIT](LICENSE)
