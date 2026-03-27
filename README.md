# OpenClaw Bridge

Offline wake-word voice assistant bridge for [OpenClaw](https://github.com/openclaw) Agent.

Real-time microphone listening → wake-word detection → speech-to-text → send to Agent → voice playback. Multi-turn conversation support with smart buffering.

## Features

- **Offline ASR/TTS/KWS** — sherpa-onnx, no cloud API needed, low latency
- **Dual wake engine** — KWS streaming keyword spotting (primary) + ASR text matching (fallback)
- **Multi-turn dialogue** — follow-up questions without re-waking (8s window)
- **Smart buffering** — queues new speech while waiting for slow Agent responses
- **WebSocket long-connection** — Ed25519 device signing, auto-reconnect, CLI fallback
- **Message tagging** — `[bridge]` prefix lets Agent switch personality for voice interactions

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Download models (~300MB)
python download_model.py

# 3. Configure OpenClaw connection in config.py
#    OPENCLAW_URL, OPENCLAW_TOKEN, OPENCLAW_SESSION_KEY

# 4. Run
python main.py
```

Say the wake word (default: `小龙虾`), then speak your command. After Agent responds, you can ask follow-up questions within 8 seconds without re-waking.

## Configuration

Edit `config.py` before first run:

| Parameter | Description |
|-----------|-------------|
| `OPENCLAW_URL` | Gateway address (default: `http://127.0.0.1:18789/`) |
| `OPENCLAW_TOKEN` | WebSocket auth token |
| `OPENCLAW_SESSION_KEY` | Agent session key (e.g. `agent:main:main`) |
| `VOICE_BRIDGE_TAG` | Message prefix for Agent to identify voice source (default: `[bridge] `) |
| `WAKE_WORD` | Wake word for ASR text matching (default: `小龙虾`) |
| `KWS_KEYWORDS` | Wake words for KWS engine |
| `TTS_LENGTH_SCALE` | Speech speed (0.5~2.0, lower = faster, default: 0.7) |

### Chinese Users

Just make sure the 4 connection parameters are correct, everything else works out of the box:

```python
OPENCLAW_URL = "http://127.0.0.1:18789/"
OPENCLAW_TOKEN = "your-token-here"
OPENCLAW_SESSION_KEY = "agent:main:main"
VOICE_BRIDGE_TAG = "[bridge] "   # your Agent should recognize this tag
```

### English Users

To use with English speech, you need to replace the models:

1. **ASR model** — change `MODEL_DIR` to an English sherpa-onnx model (e.g. `sherpa-onnx-paraformer-en`)
2. **TTS model** — change `TTS_MODEL_DIR` to an English VITS model
3. **Wake word** — change `WAKE_WORD` and `KWS_KEYWORDS` (e.g. `"hey claw"`)
4. **KWS model** — change `KWS_MODEL_DIR` to an English keyword spotter model, and add pinyin mapping in `kws_engine.py`
5. Or disable KWS entirely: `KWS_ENABLED = False` (uses ASR text matching only)

Set `TTS_KEEP_ENGLISH = True` if your TTS model supports English.

## CLI Arguments

```bash
python main.py                          # normal run
python main.py --debug                  # enable DEBUG logs
python main.py --openclaw-url URL       # override gateway URL
python main.py --model-dir DIR          # override ASR model path
python main.py --no-kws                 # disable KWS, use ASR matching only
```

## Architecture

```
Microphone → VAD segmentation → KWS wake detection
                                      ↓
                              ┌─ ASR text matching (fallback)
                              └─ KWS streaming (primary)
                                      ↓
                              Five-state machine
                              (IDLE → WAKE → LISTEN → PLAY → FOLLOWUP)
                                      ↓
                              WebSocket → OpenClaw Agent
                                      ↓
                              TTS playback → Follow-up listening
```

## Requirements

- Python 3.10+
- Microphone
- OpenClaw Gateway running
- Windows / macOS / Linux

## License

MIT
