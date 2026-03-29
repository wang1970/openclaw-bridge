# Development Notes

## Background
Real-time voice bridge tool: continuous microphone monitoring → wake-word detection → speech-to-text → submit to OpenClaw Agent.
Natural voice interaction triggered by a customizable wake word.

---

## Feature Overview

### ✅ Done
- Continuous mic monitoring + VAD speech detection
- Dual-engine wake-word detection (KWS + ASR text matching)
- Offline speech recognition (ASR)
- Offline speech synthesis (TTS, MeloTTS Chinese+English)
- WebSocket long-connection + Ed25519 signing
- Multi-turn dialogue + smart buffering
- Wake feedback ("我在" / "小龙虾跑了")
- Message tagging (`[bridge]` prefix, Agent personality switching)
- Text cleaning + mic control + debug logging

### 🔲 TODO
- **Streaming TTS** — generate and play simultaneously to reduce perceived latency
- **Interruption** — user speech interrupts TTS playback, like Xiaomi AI assistant
- **Runtime speed control** — adjust speech rate via voice command or hotkey
- **Agent persona optimization** — more natural, voice-friendly responses
- **System tray icon** — minimize to tray, run in background
- **External config file** — move from config.py to JSON/YAML
- **Auto-start on boot** — launch automatically with the system

---

## V1: End-to-End Validation ✅ Done

### Features
1. Continuous microphone audio stream monitoring
2. **Wake-word detection**: KWS engine (primary) + ASR text matching (fallback)
3. VAD speech segmentation (with pre-buffer to prevent losing speech onset, tail silence trimming)
4. sherpa-onnx offline ASR
5. Submit text to OpenClaw Agent via **openclaw agent CLI**
6. Command-line launch with logging

### Verification (2026-03-27)
End-to-end full pipeline tested:
```
17:52:09  KWS wake word detected
17:52:12  ASR recognized "你在不在"
17:52:14  Submitted to OpenClaw (openclaw agent CLI)
17:52:20  AI responded: "在呢！猫哥，小龙虾二世随时待命 🦞 有啥事直接说就行～"
```
Total latency ~11s (Agent response ~6s, on OpenClaw side).

---

## V2: TTS Voice Playback ✅ Done
- sherpa-onnx MeloTTS offline TTS (`vits-melo-tts-zh_en`, 22050Hz, Chinese+English)
- sounddevice playback, mic pausing controlled by `TTS_MUTE_MIC` config
- Text cleaning: emoji filtering + markdown stripping (`clean_text_for_tts()`), Chinese+English mixed text handled by MeloTTS

## V3: Wake Feedback + UX Optimization ✅ Done
- **Warm-up prompt**: "please wait, starting up..."
- **Ready prompt**: "assistant ready"
- **Timeout prompt**: "assistant timed out, say the wake word again"
- **Single-char noise filter**: recognition results ≤ 1 character are discarded
- **Adjustable speech rate**: `TTS_LENGTH_SCALE` (current 0.7, range 0.5~2.0)
- **`_speak()` method reuse**: unified TTS playback logic

## V4: Gateway WebSocket Communication ✅ Done

### Problem
CLI subprocess has ~6s startup overhead per call, high Agent response latency.

### Solution
Reverse-engineered OpenClaw Gateway WebSocket protocol with Ed25519 device signing.

### Implementation Details
- **Protocol flow**: challenge → connect (with Ed25519 signature) → hello-ok → chat.send → streaming receive
- **Device signing**: `deviceId = hex(SHA-256(publicKey))`, signed content `v2|deviceId|clientId|clientMode|role|scopes|signedAtMs|token|nonce`
- **Key constants**: `client.id = "clawdbot-control-ui"`, `client.mode = "webchat"`
- **Message sending**: `chat.send`, params `{sessionKey, message, idempotencyKey}`
- **Streaming response**: `agent` event (stream=assistant/lifecycle/tool/end) + `chat` event (state=delta/final)
- **Key persistence**: `.device_key.json`, auto-generated Ed25519 keypair
- **Dependencies**: `websockets` (async) + `cryptography` (Ed25519)
- **CLI fallback**: auto-degrade to CLI mode when WebSocket unavailable

### Results
- Ready on auth success (no CLI warm-up needed), startup to ready < 1s
- Agent response latency reduced from 6s+ (CLI) to ~2s

## V5: Interaction Strategy Optimization ✅ Done

### Core Problem
V4's interaction strategy was too simple: required re-waking every time, no continuous dialogue, poor UX.

### Design Reference
Modeled after Xiaomi's "小爱同学" voice assistant:
- Clear waiting state after wake
- Maintains listening after response, supports follow-up (multi-turn)
- Natural pauses between sentences won't cut off speech
- Smart buffering for new messages while waiting for slow responses

### State Machine (Five States)

```
                      Wake Word                    User speaks (concat within 4s)
┌──────────┐ ─────────────────→ ┌──────────────┐ ─────────────→ ┌──────────────┐
│   IDLE    │                   │WAKE_DETECTED │                 │LISTENING_CMD  │
│ Listening │ ←── cooldown/TO ─ │ Waiting cmd  │ ←── skip WW ── │ Recording cmd │
└──────────┘                   └──────────────┘                 └──────┬───────┘
     ↑                                │                                │
     │                        CMD_TIMEOUT(15s)               CMD_END_SILENCE(4s)
     │                        Play "timed out"                       │
     │                                │                                ▼
     │                         ┌──────────────┐              ┌──────────────────┐
     │                         │    IDLE       │              │ _finish_command  │
     │                         └──────────────┘              │ concat → send/buf │
     │                                                       └───────┬──────────┘
     │                                                               │
     │  FOLLOWUP_TIMEOUT(8s)                                          │
     │  or wake word received                                         │
     │         ┌───────────────────────────────────────────────────────┘
     │         │
     │         ▼
     │  ┌──────────────────┐     Response      ┌──────────┐     TTS play   ┌──────────┐
     └──│   FOLLOWUP        │ ──────────────→ │  PLAYING  │ ────────────→ │ FOLLOWUP  │
        │ Follow-up (8s)    │                   │ TTS play  │               │ Can ask   │
        │ User can speak    │ ←────────────── │ Mic pause │               │ directly  │
        └──────────────────┘     FOLLOWUP     └──────────┘               └──────────┘
```

### Smart Buffering Strategy
Solves the problem of "sending messages when Agent can't handle concurrent messages well":

| Scenario | Behavior |
|----------|----------|
| Fast Agent response + user follow-up | Normal flow, send follow-up directly |
| Slow Agent response + user finished speaking | Buffer new message, send after response arrives → play → send buffered |
| New speech during buffer wait | Overwrite buffer (keep only latest) |

### Key Parameters (config.py)

| Parameter | Value | Description |
|-----------|-------|-------------|
| `WAKE_COOLDOWN` | 3.0s | Wake cooldown, checked in all states |
| `CMD_TIMEOUT` | 15.0s | Timeout waiting for first speech after wake |
| `CMD_END_SILENCE` | 4.0s | Silence duration to consider command finished (> natural pause ~2s) |
| `FOLLOWUP_TIMEOUT` | 8.0s | Follow-up listening window after Agent response |
| `TTS_LENGTH_SCALE` | 0.7 | TTS speech rate (0.5~2.0) |

### Deployment Configuration (config.py)

Parameters to be configured by users when deploying to external Hub:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `OPENCLAW_URL` | `http://127.0.0.1:18789/` | Gateway URL (change to domain/IP for remote) |
| `OPENCLAW_TOKEN` | `106d...` | WebSocket auth token. Found in OpenClaw Hub → Device Management → click your device → copy Token field |
| `OPENCLAW_SESSION` | `openclaw-bridge` | Session identifier |
| `OPENCLAW_SESSION_KEY` | `agent:main:main` | Agent session key (maps to frontend session param) |
| `VOICE_BRIDGE_TAG` | `[bridge] ` | Message prefix tag (Agent uses this to switch personality) |

### Voice Bridge Message Tagging
Every message sent to Agent is prefixed with `VOICE_BRIDGE_TAG` (default `[bridge] `). The Agent recognizes this prefix to identify messages from the voice bridge and switches to a voice-appropriate personality.
- Both WS and CLI channels support tagging
- Set to empty string to disable

### Other Improvements
- **Heartbeat filtering**: `HEARTBEAT_OK` and other system messages won't trigger TTS
- **ASR crash protection**: empty audio segments causing onnxruntime crash → try/except silent skip
- **Wake feedback**: TTS speaks "我在" on wake detection (both KWS and ASR paths)
- **Mic mute control**: `TTS_MUTE_MIC` config option to control mic pausing during TTS (default: off)

---

## Thread Architecture: Dual-Thread Producer-Consumer

```
┌─────────────────────┐    Event Queue    ┌─────────────────────┐
│  Audio Capture       │   (Queue)         │  Strategy Thread    │
│  (Producer)          │ ──────────────→   │  (Consumer)         │
│                      │                   │                     │
│ · Continuous mic     │                   │ · Dequeue events    │
│ · KWS wake detection │ ─ WakeEvent      │ · KWS wake → wait   │
│ · VAD energy detect  │ ─ UtteranceEvent │ · ASR recognition   │
│ · Split by volume    │ ─ AgentRespEvent │ · ASR text match WW │
│ · Emit speech+wake   │                   │ · 5-state machine   │
│ · Stateless, fast    │                   │ · Command concat    │
│                      │                   │ · Follow-up listen  │
│                      │                   │ · Smart buffering   │
└─────────────────────┘                   └─────────────────────┘
                                                    │
                                                    ▼
                                          ┌──────────────────┐
                                          │  OpenClawClient   │
                                          │  WebSocket (main) │
                                          │  CLI (fallback)   │
                                          └──────────────────┘
```

**Design Principles:**
- Audio thread **only segments + detects**: VAD cuts speech by RMS threshold, KWS real-time streaming wake detection
- Strategy thread **only decides**: dequeue events for ASR/state decision/command submission
- Threads communicate via `queue.Queue`, audio thread never blocks
- State machine runs only in strategy thread, no locks needed

---

## OpenClaw Client: WebSocket Primary Channel

```
┌─────────────────────────────────────────────┐
│            OpenClawClient                    │
│                                             │
│  ┌───────────────────────────────────┐      │
│  │ WebSocket (primary, default)      │      │
│  │ · websockets (asyncio)            │      │
│  │ · Ed25519 device signing          │      │
│  │ · challenge → connect → hello-ok  │      │
│  │ · chat.send messages              │      │
│  │ · Streaming response receive      │      │
│  │ · Auto-reconnect (5s interval)    │      │
│  │ · Ready on auth success           │      │
│  └───────────────────────────────────┘      │
│                                             │
│  ┌───────────────────────────────────┐      │
│  │ CLI fallback                      │      │
│  │ · when websockets/crypto missing  │      │
│  │ · openclaw agent --message        │      │
│  │ · Separate async thread           │      │
│  └───────────────────────────────────┘      │
└─────────────────────────────────────────────┘
```

### WebSocket Protocol Details
- **Connect URL**: `ws://127.0.0.1:18789` (converted from `OPENCLAW_URL`)
- **Device key**: `.device_key.json`, Ed25519 keypair persistence
- **Signature format**: `v2|{deviceId}|clawdbot-control-ui|webchat|operator|scopes|{signedAtMs}|{token}|{nonce}`
- **deviceId**: `hex(SHA-256(publicKey))`
- **sessionKey**: configured via `OPENCLAW_SESSION_KEY`
- **Response events**: `agent` (stream=assistant/lifecycle/tool/end) + `chat` (state=delta/final)
- **Heartbeat filtering**: `HEARTBEAT_OK` and system messages auto-skipped

---

## Tech Stack

| Component | Choice | Notes |
|-----------|--------|-------|
| Language | Python 3.10+ | |
| ASR | sherpa-onnx paraformer-zh-small | Offline Chinese recognition |
| KWS | sherpa-onnx zipformer-wenetspeech-3.3M | Streaming keyword spotting |
| VAD | RMS energy threshold | Custom impl, with pre-buffer + tail trimming |
| Audio | sounddevice | Cross-platform mic capture |
| Agent comm | websockets + Ed25519 signing | Gateway WebSocket protocol |
| TTS | sherpa-onnx MeloTTS (vits-melo-tts-zh_en) | Offline Chinese+English synthesis |
| Crypto | cryptography | Ed25519 device signing |

---

## Project Structure

```
openclaw-bridge/
├── main.py              # Main program (dual-thread + 5-state machine + multi-turn)
├── config.py            # Global config (thresholds, timeouts, model paths, OpenClaw params)
├── asr_engine.py        # sherpa-onnx offline ASR engine
├── kws_engine.py        # sherpa-onnx KWS wake-word engine
├── vad.py               # RMS energy VAD (with pre-buffer + tail trimming)
├── openclaw_client.py   # OpenClaw client (WebSocket primary + CLI fallback)
├── tts_engine.py        # TTS engine (text cleaning + synthesis + playback)
├── download_model.py    # One-click download ASR + KWS + TTS models
├── test_gateway.py      # Gateway WebSocket protocol test script
├── requirements.txt     # Python dependencies
├── .device_key.json     # Ed25519 device keypair (auto-generated)
└── models/
    ├── sherpa-onnx-paraformer-zh-small-2024-03-09/       # ASR model
    ├── sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01/  # KWS model
    └── vits-melo-tts-zh_en/                                  # TTS model (MeloTTS Chinese+English)
```

---

## Changelog

### 2026-03-27 V1-V3: End-to-End Validation + TTS + UX
- Built dual-thread architecture, KWS + VAD + ASR full pipeline
- CLI channel first successful communication (latency ~11s)
- Integrated VITS TTS, Agent response playback
- Warm-up prompts, timeout prompts, single-char filter, speech rate tuning

### 2026-03-27 V4: Wake Feedback + Strategy Refinement
- `_speak()` method extracted for reuse
- `TTS_LENGTH_SCALE` tuning 0.8→0.6→0.7
- Full-width tilde/space cleaning
- `max_num_sentences` 1→10
- `CMD_TIMEOUT` 5→8s

### 2026-03-27 V5: Gateway WebSocket Communication
- Reverse-engineered frontend JS, discovered Ed25519 signing protocol
- `deviceId = hex(SHA-256(publicKey))` (key discovery)
- `client.id` must be `clawdbot-control-ui`, `client.mode` must be `webchat`
- Python 3.14 cryptography API adaptation (`serialization.Encoding.Raw`)
- WebSocket full pipeline: auth → chat.send → streaming receive
- Integrated into OpenClawClient, CLI degraded to fallback
- Response latency from 6s+ to ~2s

### 2026-03-27 V5.1: Interaction Strategy Optimization
- Added FOLLOWUP multi-turn listening state (modeled after Xiaomi assistant)
- Smart buffering: buffer new messages during slow Agent responses
- `CMD_TIMEOUT` 15s / `CMD_END_SILENCE` 4s / `FOLLOWUP_TIMEOUT` 8s
- Heartbeat filtering, ASR crash protection

### 2026-03-27 V5.2: Message Tagging + Config Externalization + Bug Fixes
- Added `VOICE_BRIDGE_TAG`: message prefix for Agent personality switching
- Added `OPENCLAW_SESSION_KEY`: extracted sessionKey from hardcoded to config.py
- All deployment params externalized to config.py
- Fixed: non-wake-word ASR results leaking to logs in IDLE state
- Fixed: buffer state (`_waiting_response`/`_pending_cmd`) not cleared on FOLLOWUP→IDLE timeout

### 2026-03-27 V5.3: Wake Feedback + Mic Control Optimization
- TTS speaks "我在" on wake detection (both KWS and ASR paths)
- Added `TTS_MUTE_MIC` config: control mic pausing during TTS playback (default: False)
- Fixed: dead code after `return` in `_handle_wake_detected` (indentation bug)
- Project published to GitHub: https://github.com/wang1970/openclaw-bridge

### 2026-03-28 V5.4: TTS Model Upgrade to MeloTTS + Debug Logging
- TTS model upgraded from `vits-icefall-zh-aishell3` (8000Hz, Chinese only) to `vits-melo-tts-zh_en` (22050Hz, Chinese+English)
- `tts_engine.py` added `dict_dir`/`data_dir` params for MeloTTS support
- Removed `TTS_KEEP_ENGLISH` config (MeloTTS natively supports Chinese+English, no character filtering needed)
- DEBUG logs always written to `logs/debug.log` for troubleshooting
