# Jarvis Voice TUI 🎙️🤖

> Your always-on AI voice assistant — running on your phone, powered by OpenClaw.

Jarvis is a voice-first AI assistant that lives in your terminal. Say **"Hey Jarvis"** to talk to me — I'll listen, think, and speak back with a friendly animated face.

Built for **Termux on Android**, no cloud API keys required for basic operation.

---

## ✨ Features

- 🎤 **Always-on microphone** — listens for the "Hey Jarvis" wake word
- 🗣️ **On-device STT** — uses Android's built-in `termux-speech-to-text` (free, no API key)
- 🔊 **On-device TTS** — speaks responses via `termux-tts-speak` (Android neural voices)
- 😊 **Animated emoji face** — shows emotion states in real-time
- 🤖 **OpenClaw-powered AI** — runs as a persistent sub-session connected to your AI brain
- 🌙 **Always listening** — runs 24/7 in the background on your phone
- 📸 **Camera ready** — can take photos and send them back (permission required)

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Jarvis Voice TUI                     │
├─────────────────────────────────────────────────────────┤
│                                                         │
│   🎤 Mic  ──►  🔔 Wake Word  ──►  🗣️ STT Engine       │
│                            │           │                │
│                            │           ▼                │
│                      ┌─────┴─────┐                     │
│                      │  OpenClaw │  (your AI brain)    │
│                      │  Session  │                     │
│                      └─────┬─────┘                     │
│                            │                          │
│                      ┌─────┴─────┐                     │
│                      │    TTS    │  ──► 🔊 Speaker     │
│                      └───────────┘                     │
│                                                         │
│   Face: 🤖 😊 🤔 🔊 😢 🎉 💤  (emoji emotion states)  │
└─────────────────────────────────────────────────────────┘
```

---

## 🚀 Quick Start

### 1. Install termux-api

```bash
pkg install termux-api
```

### 2. Clone the repo

```bash
cd /data/data/com.termux/files/home
git clone https://github.com/strognoff/jarvis-voice
cd jarvis-voice
```

### 3. Run

```bash
python3 tui.py
```

You'll see the animated Jarvis face. Say **"Hey Jarvis"** to activate — then speak your command.

---

## 📱 Usage

### Activation

Say **"Hey Jarvis"** or press **Enter** to activate manual mode.

### Voice Commands

After activation, speak naturally. Examples:
- *"Hey Jarvis, what's the weather in London?"*
- *"Hey Jarvis, set a reminder for 5pm"*
- *"Hey Jarvis, what's trending on X?"*
- *"Hey Jarvis, tell me a joke"*

### Exit

Press **Ctrl+C** to gracefully shut down.

---

## 🎭 Emotion States

The face updates based on what Jarvis is doing:

| State | Emoji | When |
|-------|-------|------|
| Idle | 🤖 | Waiting for "Hey Jarvis" |
| Listening | 🎤 | Actively capturing speech |
| Thinking | 🤔 | Processing your request |
| Speaking | 🔊 | Speaking response aloud |
| Happy | 😊 | Response complete |
| Excited | 🎉 | Big news or celebration |
| Sad | 😢 | Something went wrong |
| Sleeping | 💤 | Going back to idle |
| Error | ❌ | Error occurred |

---

## ⚙️ Configuration

### Environment Variables

```bash
# OpenClaw gateway URL (default: http://localhost:18789)
export JARVIS_GATEWAY_URL="http://localhost:18789"

# OpenClaw auth token (from gateway config)
export JARVIS_AUTH_TOKEN="your-token-here"

# STT engine: termux-api (default), openai, local
export JARVIS_STT_ENGINE="termux-api"

# TTS engine: termux-tts (default), elevenlabs, openai-tts
export JARVIS_TTS_ENGINE="termux-tts"

# Session key for the sub-agent
export JARVIS_SESSION="agent:main:subagent:jarvis-tui"
```

### STT Engines

| Engine | Description | API Key Needed? |
|--------|-------------|-----------------|
| `termux-api` | Android built-in speech-to-text | ❌ No |
| `openai` | OpenAI Whisper API | ✅ Yes |
| `local` | faster-whisper running locally | ❌ No |

### TTS Engines

| Engine | Description | API Key Needed? |
|--------|-------------|-----------------|
| `termux-tts` | Android built-in neural TTS | ❌ No |
| `elevenlabs` | ElevenLabs expressive voices | ✅ Yes |
| `openai-tts` | OpenAI TTS API | ✅ Yes |

---

## 📁 Project Structure

```
jarvis-voice/
├── tui.py              # Main TUI entry point
├── jarvis.py           # Full app with all modules
├── jarvis-simple.py    # Simple standalone version
├── face.py             # Animated emoji face engine
├── audio.py            # Audio capture (pyaudio / termux)
├── wakeword.py         # Wake word detection (VAD)
├── stt.py              # Speech-to-text engines
├── tts.py              # Text-to-speech engines
├── config.py           # Configuration management
├── faces.txt           # Face sprite definitions
├── requirements.txt    # Python dependencies
├── jarvis.sh           # Shell launcher script
└── README.md           # This file
```

---

## 🔧 Development

### Install Python dependencies

```bash
pip install -r requirements.txt
```

### Run in verbose mode

```bash
python3 tui.py --verbose
```

### Use a custom config

```bash
python3 tui.py --config /path/to/config.json
```

---

## 🐛 Troubleshooting

### "termux-speech-to-text not found"

```bash
pkg install termux-api
```

### Microphone not working

1. Go to **Android Settings → Apps → Termux → Permissions**
2. Enable **Microphone** permission
3. Also check **Termux:API** app permissions

### Camera not working

Same as above — grant camera permission to Termux:API.

### Gateway connection refused

Make sure OpenClaw gateway is running:

```bash
openclaw gateway status
```

If not running, start it:

```bash
openclaw gateway start
```

---

## 🌟 What's Next

- [ ] **Wake word detection** — proper "Hey Jarvis" hotword detection (Porcupine / openwakeword)
- [ ] **Voice activity detection (VAD)** — auto-trigger listening without pressing Enter
- [ ] **OpenClaw AI integration** — full AI responses via OpenClaw session instead of local echo
- [ ] **Telegram bridge** — control Jarvis remotely via Telegram bot
- [ ] **Bluetooth headset support** — use BT mic for truly wireless operation
- [ ] **Custom wake words** — train your own wake word model

---

## 📜 License

MIT — do whatever you want with it.

---

## 🙏 Credits

Built with ❤️ for Termux on Android.

Powered by [OpenClaw](https://openclaw.ai/) — the open-source AI assistant framework.

Python + termux-api + a lot of coffee ☕