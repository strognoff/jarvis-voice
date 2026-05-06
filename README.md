# Jarvis Voice TUI

**Always-on voice assistant** for Android (Termux) powered by OpenClaw. Listens for your voice, processes commands via AI, and speaks replies out loud.

> "Hey Jarvis, what's the weather in London?"

---

## Features

- 🎤 **Continuous voice monitoring** — WebRTC VAD detects when you start/stop speaking
- 🤖 **OpenClaw AI** — sends transcribed commands to an OpenClaw sub-session for rich responses  
- 🔊 **On-device TTS** — reads responses aloud via `termux-tts-speak` (no API keys)
- 😊 **Animated emoji face** — shows current state (idle / listening / thinking / speaking)
- 🌐 **No cloud dependencies** — runs entirely on-device (except OpenClaw gateway)
- 🔓 **Open source** — MIT licensed, self-hosted

---

## Requirements

- **Android** with [Termux](https://termux.com/) installed
- **Python 3.13+** (comes with Termux)
- **termux-api** package (`pkg install termux-api`)
- **OpenClaw** gateway running on the device (or accessible elsewhere)
- **Internet** — for OpenClaw AI responses (or use the local fallback)

### Termux packages needed

```bash
pkg install termux-api python ffmpeg sox
```

### Python dependencies

```bash
pip install -r requirements.txt
```

That's it. No PyTorch, no cloud API keys, no compilation.

---

## Quick Start

```bash
# Clone (or open the repo directory)
cd ~/jarvis-voice

# Install Python dependencies  
pip install -r requirements.txt

# Configure (optional — defaults work for local OpenClaw)
export JARVIS_GATEWAY_URL="http://localhost:18789"
export JARVIS_AUTH_TOKEN="your-gateway-token"
export JARVIS_SESSION="agent:main:subagent:jarvis-tui"

# Run!
python3 tui.py
```

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     Jarvis Voice TUI                        │
│                                                             │
│   🎤 Always-on loop                                         │
│      ┌──────────────────────────────────────────────┐      │
│      │  termux-mic-record → ffmpeg → WebRTC VAD     │      │
│      │  (500ms chunks, 16k mono, 30ms frames)       │      │
│      │                                               │      │
│      │  Speech? → buffer audio                      │      │
│      │  Silence (600ms)? → trigger wake ───────────┼──→   │
│      └──────────────────────────────────────────────┘      │
│                                                            │
│   on_wake:                                                 │
│      → termux-speech-to-text → transcribed text           │
│      → OpenClaw sub-session → AI response                  │
│      → termux-tts-speak → audio output                   │
│      → animated emoji face state                          │
└─────────────────────────────────────────────────────────────┘
```

### Key files

| File | Purpose |
|------|---------|
| `tui.py` | Main entry point — full VAD loop + TUI |
| `wakeword.py` | WebRTC VAD engine + fallback energy detection |
| `stt.py` | STT engines (termux-speech-to-text, OpenAI Whisper, faster-whisper) |
| `tts.py` | TTS engines (termux-tts-speak, ElevenLabs, OpenAI TTS) |
| `face.py` | Animated ASCII/emoji face engine |
| `audio.py` | Audio capture helpers |
| `config.py` | Environment variable configuration |

---

## Wake Word Detection

Jarvis uses **WebRTC VAD** (Voice Activity Detection) for always-on monitoring:

- Listens continuously via `termux-microphone-record`
- Converts 500ms chunks to 16kHz mono PCM with ffmpeg
- Feeds through WebRTC VAD (3 modes, 30ms frames)
- Energy-based fallback when VAD fails on a frame
- Detects end of speech at ~600ms consecutive silence

### Why not Silero VAD?

Silero VAD is higher quality but requires **PyTorch** — which isn't available for Python 3.13 on Android ARM64 (no compatible wheels, build from source fails). WebRTC VAD is a solid fallback that works out of the box.

### Wake word vs Push-to-talk

This is **always-on VAD** (listens continuously) — not push-to-talk. It detects when you start and stop speaking, so you don't need to hold a button. Say anything, pause, and Jarvis processes it.

---

## Speech-to-Text

Primary: **`termux-speech-to-text`** (on-device Android speech recognition)
- No API key needed
- Uses Android's built-in speech recognizer
- Opens as a system UI dialog

Fallback: OpenAI Whisper API (requires API key, configured via `OPENAI_API_KEY`)

### faster-whisper (offline)

`faster-whisper` is a great offline option but **not currently available** on Python 3.13 / Android ARM64 — `ctranslate2` has no compatible wheel and building from source fails due to missing `spawn.h`. This may change with future Python/distro releases.

---

## Text-to-Speech

Primary: **`termux-tts-speak`** (on-device Android neural TTS)
- No API key needed
- Uses Android's built-in TTS engine
- Configurable voices via `termux-tts-engines`

Fallback: ElevenLabs or OpenAI TTS (requires API key)

---

## OpenClaw Integration

Jarvis TUI connects to OpenClaw as a **sub-session** (`agent:main:subagent:jarvis-tui`):

```python
# Environment variables (or edit config.py)
JARVIS_GATEWAY_URL="http://localhost:18789"
JARVIS_AUTH_TOKEN="your-token"
JARVIS_SESSION="agent:main:subagent:jarvis-tui"
```

The sub-session inherits your OpenClaw configuration (model, skills, memory, etc.) while running as a separate session. Messages are sent via the gateway HTTP API and responses returned directly.

### Local fallback

If OpenClaw is unreachable, a simple rule-based response generator runs locally:
- "hello" → "Hello! How can I help you today?"
- "time" → current time in London
- "date" → today's date
- "weather" → weather check prompt
- "joke" → a joke
- etc.

---

## Running as a Service

### On Android (Termux Boot or manual)

```bash
# Start in background with nohup
nohup python3 tui.py > Jarvis.log 2>&1 &

# Or use the provided shell script
./jarvis.sh
```

### On desktop/server

The TUI can also run connected to a remote OpenClaw gateway:

```bash
JARVIS_GATEWAY_URL="http://192.168.1.100:18789" \
JARVIS_AUTH_TOKEN="your-token" \
JARVIS_SESSION="agent:main:subagent:desktop-jarvis" \
python3 tui.py
```

---

## Development

```bash
# Clone the repo
git clone https://github.com/strognoff/jarvis-voice.git
cd jarvis-voice

# Install dev dependencies
pip install -r requirements.txt

# Run
python3 tui.py

# Run tests (placeholder)
python3 wakeword.py  # tests VAD on existing audio file
```

### Testing VAD without a microphone

```bash
# Test WebRTC VAD on the included sample (or any .ogg file)
python3 wakeword.py
```

This runs VAD on an existing audio file and reports the speech ratio.

---

## Known Limitations

| Issue | Cause | Workaround |
|-------|-------|------------|
| No `faster-whisper` | No ctranslate2 wheel for cp313 Android ARM64 | Use `termux-speech-to-text` or OpenAI Whisper API |
| No `torch` / `silero-vad` | download.pytorch.org returns 403 | Use WebRTC VAD instead |
| No `numpy` wheel | No cp313 Android ARM64 wheel | Energy fallback uses pure Python struct |
| `termux-microphone-record` needs permission | Android audio permission | Grant microphone permission to Termux in Android settings |

---

## Project Status

**Working:**
- ✅ WebRTC VAD wake word detection
- ✅ termux-speech-to-text STT
- ✅ termux-tts-speak TTS
- ✅ OpenClaw sub-session integration
- ✅ Emoji face state display
- ✅ Local fallback responses

**In progress:**
- 🔄 Testing `termux-microphone-record` on real device
- 🔄 Proper wake word phrase detection ("Hey Jarvis")
- 🔄 Confidence threshold tuning

**Blocked:**
- ❌ `faster-whisper` — no cp313 Android ARM64 ctranslate2 wheel
- ❌ Silero VAD — no torch on this platform
- ❌ numpy — no cp313 wheel (pure Python fallback used instead)

---

## Contributing

1. Fork the repo
2. Create a feature branch  
3. Make changes + test
4. Push and open a PR

---

## License

MIT — do whatever you want with it.
