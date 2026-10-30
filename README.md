# audio.cpp for Home Assistant

Use the text-to-speech and speech-recognition models of an [audio.cpp](https://github.com/0xShug0/audio.cpp) server (`audiocpp_server`) in Home Assistant and Assist.

- **TTS engines** (`tts.*`) for any `tts` or voice-cloning model on the server, such as Pocket TTS, Kokoro, OmniVoice or Chatterbox.
- **STT engines** (`stt.*`) for any `asr` model, such as Nemotron or Parakeet.
- **Streaming both ways.** Assist starts speaking an LLM reply sentence by sentence while it is still being written, and a streaming ASR model transcribes while you are still talking.
- **Everything runs locally**, on your own audio.cpp server.

## Requirements

- Home Assistant 2026.10.0 or newer.
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
| Extra request options | Extra fields sent with every speech request, such as `seed` or an `options` object with model options. |

Saving a TTS engine first checks the speed and extra request options against the options the server says the model takes (audio.cpp v0.9.1 or newer). A typo or a renamed option is flagged with a suggestion, for example "`guidance` is not an option of omnivoice; did you mean `guidance_scale`?". If you know the model takes it anyway, save again without changes to keep it.

It then runs a short test synthesis ("Hello."). This catches settings the model rejects, and records the sample rate that the streaming endpoint doesn't report. The test can take a while if the server first has to load the model.

With a server older than v0.9.1, the speed field is always shown and only the test synthesis checks the settings.

### STT engine settings

| Setting | What it does |
|---|---|
| Languages | The languages this engine offers to Home Assistant. |
| Use the live streaming endpoint | Send audio while it is captured, so the model decodes as you speak. Only offered for models the server runs with `mode: streaming`. |
| Language sent to the model | Nothing (default), a language code (`en`), or a full tag (`en-US`). Some models reject a language field. |
| Recognition prompt | Optional context such as names or hotwords, for models that support it. |

## Model notes

- **VoxCPM2 streaming** needs `{"options": {"retry_badcase": false}}` in the extra request options. The form suggests it for you.
- **Chatterbox** only reads the language from the request, so an Italian engine needs `{"language": "it"}` in the extra request options. It has no streaming, and the speed field appears only if the server's model spec declares speed control.
- **OmniVoice** detects the language itself, so one engine can serve several languages. For pseudo-streaming, set `text_chunk_size` in the server's `default_request_options`.
- audio.cpp runs **one request per model at a time** unless the server is started with `--parallel-jobs`.

## Troubleshooting

- **Cannot connect:** check the URL, and that `audiocpp_server` is running and listening on an address Home Assistant can reach.
- **Test synthesis failed:** the form shows the server's own error message. Usually the model doesn't support a setting such as speed or voice.
- **Repair "TTS engine … has settings … does not accept":** each time the server entry loads, saved engines are checked against what the server says each model takes. This usually follows a server upgrade that renamed an option. Reconfigure the engine to fix it, or ignore the issue if the model does take the setting.
- **Debug logs:** add this to `configuration.yaml`:

  ```yaml
  logger:
    logs:
      custom_components.audiocpp: debug
  ```

## Development

The tests run against a fake server that implements the audio.cpp wire format:

```bash
pip install homeassistant==2026.10.2 pytest-homeassistant-custom-component mutagen ha-ffmpeg
cd custom_components/audiocpp && pytest
```

Implementation notes and the audio.cpp API findings are in [`custom_components/audiocpp/CLAUDE.md`](custom_components/audiocpp/CLAUDE.md).

## License

[MIT](LICENSE)
