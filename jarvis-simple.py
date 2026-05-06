#!/usr/bin/env python3
"""
jarvis-simple.py — Simple Jarvis CLI using only termux-api
Works immediately without extra dependencies.
Wake word: says "Hey Jarvis" to activate, then uses termux-speech-to-text
"""

import sys
import os
import time
import subprocess
import threading

APP_DIR = "/data/data/com.termux/files/home/jarvis-voice"

# Load face display
sys.path.insert(0, APP_DIR)
try:
    from face import JarvisFace
except ImportError:
    class JarvisFace:
        def set_emotion(self, e): pass
        def _render(self): pass


FACE = JarvisFace()
FACE.set_emotion("start")
print()
print("  ╭─────────╮")
print("  │  ◉ ◉  │")
print("  │  (o o) │")
print("  ╰─────────╯")
print()


def log(msg):
    print(f"  [jarvis] {msg}", flush=True)


def speak(text: str):
    """Use termux-tts-speak to say something."""
    try:
        subprocess.run(
            ["termux-tts-speak", text],
            capture_output=True, text=True, timeout=15
        )
    except Exception as e:
        log(f"TTS error: {e}")


def listen() -> str:
    """Use termux-speech-to-text to listen and transcribe."""
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


def play_beep():
    """Play a short beep to indicate wake word detection."""
    try:
        # Simple audio beep using termux-tone or sox
        subprocess.run(
            ["termux-volume", "music", "5"],
            capture_output=True, timeout=2
        )
    except:
        pass


def get_response(text: str) -> str:
    """Generate a response using local logic."""
    text_lower = text.lower().strip()

    if any(g in text_lower for g in ["hello", "hi", "hey"]):
        return "Hello! How can I help you today?"
    elif "how are you" in text_lower:
        return "I'm doing great, thanks for asking! Ready to assist."
    elif "your name" in text_lower or "who are you" in text_lower:
        return "My name is Jarvis, your personal AI assistant."
    elif "thank" in text_lower:
        return "You're welcome!"
    elif "bye" in text_lower or "goodbye" in text_lower:
        return "Goodbye! I'll be here when you need me."
    elif "time" in text_lower:
        import datetime
        now = datetime.datetime.now()
        return f"It's {now.strftime('%I:%M %p')} in London."
    elif "date" in text_lower:
        import datetime
        now = datetime.datetime.now()
        return f"Today is {now.strftime('%A, %B %d')}"
    elif any(q in text_lower for q in ["what", "how", "why", "when", "where", "who", "which"]):
        return "That's a good question. I'm not sure I have the full answer right now, but I'm always happy to help you find out!"
    elif "weather" in text_lower:
        return "I can check the weather for you. What location are you interested in?"
    elif "joke" in text_lower:
        return "Why did the AI cross the road? To get to the other side of the neural network!"
    elif "sing" in text_lower or "song" in text_lower:
        return "🎵 Doe, a deer, a female deer... Ray, a drop of golden sun! 🎵"
    else:
        return "Got it. Let me think about that."


def main():
    FACE.set_emotion("idle")
    log("Ready! Say 'Hey Jarvis' to activate...")
    print()
    print("  ╭─────────╮")
    print("  │  ¯\\_/¯  │")
    print("  │  (o o)  │")
    print("  ╰─────────╯")
    print()

    listening = False

    try:
        while True:
            # Listen for audio trigger (VAD simulation via termux)
            # For "Hey Jarvis" detection, we rely on the user pressing enter
            # or we can poll the microphone level using termux
            cmd = input(">>> ").strip().lower() if sys.platform != "android" else ""

            if "hey jarvis" in cmd or cmd == "jarvis":
                listening = True
                play_beep()
                FACE.set_emotion("excited")
                print()
                log("Listening...")
                print("  ╭─────────╮")
                print("  │  ◉ ◉  │")
                print("  │  (o o) │")
                print("  ╰─────────╯")
                print()

                text = listen()

                if text:
                    log(f"You said: {text}")
                    FACE.set_emotion("thinking")
                    print("  ╭─────────╮")
                    print("  │  ¯◡¯  │")
                    print("  │  ( -) │")
                    print("  ╰─────────╯")
                    print()

                    response = get_response(text)
                    log(f"Jarvis: {response}")
                    FACE.set_emotion("speaking")
                    speak(response)
                    FACE.set_emotion("happy")
                    print("  ╭─────────╮")
                    print("  │  ◉ ◉  │")
                    print("  │  (‿‿) │")
                    print("  ╰─────────╯")
                    print()
                else:
                    log("No speech detected.")
                    FACE.set_emotion("idle")

            elif cmd in ["quit", "exit", "bye", "stop"]:
                break

            else:
                if cmd and cmd != "":
                    print("Say 'Hey Jarvis' to activate...")
                time.sleep(0.5)

    except (KeyboardInterrupt, EOFError):
        pass

    print()
    print("  ╭─────────╮")
    print("  │  · ·  │")
    print("  │  (x x) │")
    print("  ╰─────────╯")
    print()
    log("Goodbye!")


if __name__ == "__main__":
    main()