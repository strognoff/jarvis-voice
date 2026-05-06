# Jarvis Voice TUI

**Always-on voice assistant** for Android (Termux) powered by OpenClaw. Listens for your voice, processes commands via AI, and speaks replies out loud.

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

# Run the mic test first to verify audio works
python3 tui.py --test-mic

# Configure (optional — defaults work for local OpenClaw)
export JARVIS_GATEWAY_URL="http://localhost:18789"
export JARVIS_AUTH_TOKEN="your-gateway-token"
export JARVIS_SESSION="agent:main:subagent:jarvis-tui"

# Run!
python3 tui.py
```

---

## How it works

```
[Mic] ──► [WebRTC VAD always-on loop]
              500ms chunks → 16k mono PCM → 30ms frames
              Energy fallback (RMS > 800)
              End of speech at ~2.5s silence
                    │
                    ▼
         [termux-speech-to-text: Android STT]
                    │
                    ▼
          [OpenClaw sub-session: jarvis-tui]
                    │
                    ▼
        [termux-tts-speak + animated emoji face]
```

1. **VAD loop** — always-on, records 500ms chunks, runs through WebRTC VAD. Detects speech start + end of utterance (at ~2.5s natural pause)
2. **STT** — `termux-speech-to-text` (Android's built-in, no API key)
3. **AI response** — sends text to OpenClaw sub-session, gets reply
4. **TTS** — speaks response via `termux-tts-speak`
5. **Face** — animated emoji states in terminal

---

## VAD Tuning

The VAD is configured for natural conversation pauses:

| Parameter | Value | Meaning |
|-----------|-------|---------|
| Energy threshold | 800 RMS | Sensitivity for quiet speech |
| Silence threshold | 5 frames (~2.5s) | Wait this long after speech ends to trigger |
| Min speech frames | 2 frames (60ms) | Minimum speech to count as speaking |

### Why not "Hey Jarvis" wake phrase detection?

This VAD responds to **any speech** — not a specific wake word. Say anything, pause for 2-3 seconds, and Jarvis processes it. A proper "Hey Jarvis" phrase detector (like Silero VAD's keyword spotting) would need PyTorch, which isn't available on this platform.

---

## Key files

| File | Purpose |
|------|---------|
| `tui.py` | Main entry point — VAD loop + TUI + gateway integration |
| `wakeword.py` | WebRTC VAD engine + energy fallback |
| `stt.py` | STT engines (termux-api, OpenAI Whisper API) |
| `tts.py` | TTS engines (termux-tts-speak, ElevenLabs, OpenAI TTS) |
| `face.py` | Animated emoji face engine |
| `config.py` | Environment variable configuration |
| `requirements.txt` | Python dependencies |

---

## Speech-to-Text

**Primary:** `termux-speech-to-text` — on-device Android speech recognition, no API key needed.

**Fallback:** OpenAI Whisper API (set `OPENAI_API_KEY`).

> **Note:** `faster-whisper` is not available on Python 3.13 / Android ARM64 — `ctranslate2` has no compatible wheel.

---

## Text-to-Speech

**Primary:** `termux-tts-speak` — on-device Android neural TTS, no API key needed.
Check available voices: `termux-tts-engines`

**Fallback:** ElevenLabs or OpenAI TTS (set API key in environment).

---

## OpenClaw Integration

Connects as a sub-session (`agent:main:subagent:jarvis-tui`):

```bash
export JARVIS_GATEWAY_URL="http://localhost:18789"
export JARVIS_AUTH_TOKEN="your-token"
export JARVIS_SESSION="agent:main:subagent:jarvis-tui"
```

### Local fallback

If OpenClaw is unreachable, a rule-based response generator handles basic commands:
`hello`, `how are you`, `your name`, `time`, `date`, `weather`, `joke`, `thank`, `bye`

---

## Running as a Service

```bash
# Background (survives terminal close)
nohup python3 tui.py > jarvis.log 2>&1 &

# Watch log
tail -f jarvis.log

# Stop
pkill -f tui.py
```

---

## Troubleshooting

**`termux-api not found`** → `pkg install termux-api`

**Mic test fails** → Grant microphone permission to Termux in Android Settings → Apps → Termux → Permissions

**VAD not triggering** → Run with debug output enabled in tui.py (per-chunk RMS logging is already on every 5th iteration)

**Gateway connection refused** → `openclaw gateway start`

**webrtcvad import error** → The package is pre-patched to remove a `pkg_resources` dependency. Reinstall if needed.

---

## Known Limitations

| Issue | Cause | Workaround |
|-------|-------|------------|
| No wake word phrase | No PyTorch for Silero VAD | VAD responds to any speech |
| No `faster-whisper` | No ctranslate2 cp313 wheel | Use termux-speech-to-text |
| No `torch` | download.pytorch.org blocked | WebRTC VAD instead |
| No `numpy` | No cp313 Android ARM64 wheel | Pure Python RMS fallback |

---

## Project Status

**Working:**
- ✅ WebRTC VAD continuous monitoring
- ✅ termux-speech-to-text STT
- ✅ termux-tts-speak TTS
- ✅ OpenClaw sub-session integration
- ✅ Emoji face state display
- ✅ Local fallback responses
- ✅ `--test-mic` diagnostic flag

**In progress:**
- 🔄 VAD threshold tuning on real device
- 🔄 End-of-utterance detection optimisation

**Blocked:**
- ❌ `faster-whisper` / ctranslate2 — no cp313 wheel
- ❌ Silero VAD — no torch
- ❌ True wake-word phrase detection

---

## License

MIT