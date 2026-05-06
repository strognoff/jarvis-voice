# Jarvis Voice Assistant 🤖

Your always-on voice assistant that runs on your phone via Termux.

## What it does

- 🎤 **Always listening** for "Hey Jarvis" wake word
- 🗣️ **Speech-to-text** — converts your speech to text (uses termux-speech-to-text or OpenAI Whisper API)
- 💬 **Responds** — generates a reply (local logic or connects to OpenClaw)
- 🔊 **Text-to-speech** — speaks the response back (termux-tts-speak)
- 😊 **Animated face** — shows emotion states in the terminal

## Quick Start

### Requirements

- Termux app on Android
- Termux:API package installed
- Python 3.10+

### Install

```bash
# Install termux-api package
pkg install termux-api

# Go to the jarvis-voice directory
cd /data/data/com.termux/files/home/jarvis-voice

# Install Python dependencies
pip install -r requirements.txt
```

### Run

```bash
# Simple version (uses termux-api only, no extra deps)
python3 jarvis-simple.py

# Full version (needs pyaudio/numpy, may have issues on Android/Python 3.13)
python3 jarvis.py
```

## Usage

**Simple mode**: Type `hey jarvis` to activate, then speak your command. The assistant listens via `termux-speech-to-text`.

**In simple mode**: Just say "Hey Jarvis" or type "jarvis" to activate.

**Telegram integration** (planned): Send a voice message on Telegram, OpenClaw transcribes it and responds.

## Architecture

```
┌──────────────────────────────────────────────┐
│              Jarvis Voice App                │
├──────────────────────────────────────────────┤
│  [face.py]     Animated ASCII/emoji face     │
│  [wakeword.py] Wake word detection (VAD)      │
│  [audio.py]    Audio capture (pyaudio/termux)│
│  [stt.py]      Speech-to-text engine         │
│  [tts.py]      Text-to-speech engine         │
│  [config.py]   Configuration & env vars     │
│  [jarvis.py]   Main app & conversation logic │
└──────────────────────────────────────────────┘
```

## Configuration

Set environment variables to configure:

```bash
# STT engine: "termux-api" (default, free) or "openai" (needs API key)
export JARVIS_STT_ENGINE="termux-api"

# TTS engine: "termux-tts" (default, free) or "elevenlabs" or "openai-tts"
export JARVIS_TTS_ENGINE="termux-tts"

# OpenAI key (for Whisper STT or TTS)
export OPENAI_API_KEY="your-key-here"

# ElevenLabs key (for TTS)
export ELEVENLABS_API_KEY="your-key-here"
```

## Current Status

| Component | Status | Notes |
|-----------|--------|-------|
| Face display (ASCII) | ✅ Works | jarvis-simple.py works standalone |
| STT (termux-speech-to-text) | ✅ Works | On-device, no API key needed |
| TTS (termux-tts-speak) | ✅ Works | On-device Android TTS |
| Wake word detection | 🔧 Needs work | Energy-based VAD only for now |
| pyaudio/numpy install | ❌ Blocked | Python 3.13 on Android — compatibility issues |
| OpenAI Whisper STT | ⏳ Pending | Needs API key to be configured |
| OpenClaw integration | 🔜 Future | Will connect to OpenClaw session |

## Fixing Issues

### Microphone permission
If you get mic errors, make sure Termux:API has microphone permission in Android Settings → Apps → Termux:API → Permissions.

### pyaudio fails to install
This is a known issue on Python 3.13/Android. Use `jarvis-simple.py` instead — it uses only termux-api and works fine.

### termux-speech-to-text says "No speech detected"
The termux-speech-to-text function opens an Android dialog. In the terminal, just type your message when prompted. For actual voice, ensure you're in a quiet environment and grant mic permission.

## TODO

- [ ] Proper wake word detection (openwakeword or porcupine)
- [ ] Connect to OpenClaw for full AI responses
- [ ] VAD (voice activity detection) to auto-trigger listening
- [ ] Push-to-talk mode (press Enter to talk, release to send)
- [ ] Telegram bot integration for remote control
- [ ] Wake word via external mic/Bluetooth headset
- [ ] Custom wake word training