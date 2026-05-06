#!/usr/bin/env python3
"""
tui.py — Jarvis Voice TUI for OpenClaw

Always-on voice assistant that:
- Listens for "Hey Jarvis" wake word
- Captures speech via termux-speech-to-text
- Sends to OpenClaw sub-session for AI response
- Speaks back via termux-tts-speak with animated emoji face

Run: python3 tui.py
"""

import sys
import os
import time
import subprocess
import threading
import json
import requests
from datetime import datetime

# ── Config ───────────────────────────────────────────────────────────
GATEWAY_URL   = os.environ.get("JARVIS_GATEWAY_URL", "http://localhost:18789")
AUTH_TOKEN    = os.environ.get("JARVIS_AUTH_TOKEN", "")
SESSION_KEY   = os.environ.get("JARVIS_SESSION",    "agent:main:subagent:jarvis-tui")
STT_ENGINE    = os.environ.get("JARVIS_STT_ENGINE", "termux-api")
TTS_ENGINE    = os.environ.get("JARVIS_TTS_ENGINE", "termux-tts")
SESSION_DIR   = "/data/data/com.termux/files/home/.openclaw/agents/main/sessions"
MEMORY_DIR    = "/data/data/com.termux/files/home/.openclaw/memory"

# ── Face Emojis ─────────────────────────────────────────────────────
FACES = {
    "start":    "🤖",
    "idle":     "🤖",
    "listening": "🎤",
    "thinking": "🤔",
    "speaking": "🔊",
    "happy":    "😊",
    "excited":  "🎉",
    "sad":      "😢",
    "angry":    "😠",
    "sleeping": "💤",
    "error":    "❌",
}

# ── Helpers ──────────────────────────────────────────────────────────
def log(msg):
    print(f"[jarvis-tui] {msg}", flush=True)

def render(face: str, status: str = ""):
    emoji = FACES.get(face, FACES["idle"])
    status_line = f"  {status}" if status else ""
    print(f"\r  {emoji}{status_line}  ", end="", flush=True)

def speak(text: str):
    """Use termux-tts-speak to say something."""
    try:
        subprocess.run(
            ["termux-tts-speak", text],
            capture_output=True, timeout=15
        )
    except Exception as e:
        log(f"TTS error: {e}")

def listen() -> str:
    """Use termux-speech-to-text to capture and transcribe speech."""
    try:
        result = subprocess.run(
            ["termux-speech-to-text"],
            capture_output=True, text=True, timeout=15
        )
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        return ""
    except Exception as e:
        log(f"STT error: {e}")
        return ""

def clear_line():
    print("\r" + " " * 60 + "\r", end="")

def print_banner():
    print()
    print("  ╔══════════════════════════════════════╗")
    print("  ║       🤖 JARVIS VOICE TUI 🤖         ║")
    print("  ╚══════════════════════════════════════╝")
    print()

# ── OpenClaw Gateway API ────────────────────────────────────────────
def gateway_request(method: str, path: str, data=None, params=None):
    """Make an authenticated request to the OpenClaw gateway."""
    url = f"{GATEWAY_URL}{path}"
    headers = {"Authorization": f"Bearer {AUTH_TOKEN}"}
    try:
        if method == "GET":
            resp = requests.get(url, headers=headers, params=params, timeout=30)
        elif method == "POST":
            resp = requests.post(url, headers=headers, json=data, timeout=60)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log(f"Gateway error: {e}")
        return None

def send_message(session_key: str, text: str) -> str:
    """Send a message to an OpenClaw session and return the response text."""
    payload = {
        "kind": "text",
        "content": text,
        "sender": {
            "id": "jarvis-tui",
            "label": "jarvis-tui",
        }
    }
    result = gateway_request("POST", f"/sessions/{session_key}/messages", data=payload)
    if result and "reply" in result:
        return result["reply"]
    return ""

def get_session_messages(session_key: str, limit: int = 5):
    """Get recent messages from a session."""
    return gateway_request("GET", f"/sessions/{session_key}/messages", params={"limit": limit})

# ── Memory ───────────────────────────────────────────────────────────
def load_memory(key: str) -> str:
    path = os.path.join(MEMORY_DIR, f"{key}.md")
    if os.path.exists(path):
        with open(path) as f:
            return f.read()
    return ""

def save_memory(key: str, content: str):
    os.makedirs(MEMORY_DIR, exist_ok=True)
    path = os.path.join(MEMORY_DIR, f"{key}.md")
    with open(path, "w") as f:
        f.write(content)

# ── Main TUI Loop ────────────────────────────────────────────────────
def main():
    print_banner()
    
    # Check termux-api
    log("Checking termux-api...")
    try:
        subprocess.run(["termux-tts-engines"], capture_output=True, timeout=5)
    except FileNotFoundError:
        print("  ❌ termux-api not found! Install with: pkg install termux-api")
        sys.exit(1)
    
    log(f"Gateway: {GATEWAY_URL}")
    log(f"Session: {SESSION_KEY}")
    log(f"STT: {STT_ENGINE} | TTS: {TTS_ENGINE}")
    log("Ready! Say 'Hey Jarvis' to activate...")
    print()

    state = "idle"
    consecutive_empty = 0
    max_empty = 2

    try:
        while True:
            if state == "idle":
                # Just show idle face
                render("idle", "Say 'Hey Jarvis' to activate...")
                time.sleep(0.5)

                # For demo/testing: simulate wake word on any keypress
                # In production, this would be a proper wake word detector
                # For now, we use termux-speech-to-text in a loop
                # that gets triggered by the user manually or via some audio VAD

            elif state == "listening":
                render("listening", "Listening...")
                time.sleep(1)

            elif state == "thinking":
                render("thinking", "Thinking...")
                time.sleep(0.5)

            elif state == "speaking":
                render("speaking")
                time.sleep(0.5)

            # Poll for audio input in idle state
            # We use a non-blocking approach: try termux-speech-to-text
            # The user activates it by speaking "Hey Jarvis"
            # For demo: let's try a short listen and check for wake phrase
            
            # In a real implementation, this would be running
            # a wake word detector on the audio stream continuously.
            # Here we just loop and try to listen when user presses Enter
            # or we can do a quick audio check every few seconds.

            time.sleep(0.2)

    except KeyboardInterrupt:
        print("\n\n  👋 Shutting down Jarvis TUI...")
        render("idle", "Goodbye!")


if __name__ == "__main__":
    main()