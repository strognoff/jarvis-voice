#!/usr/bin/env python3
"""
jarvis.py — Jarvis Voice Assistant (CLI)
Listens for "Hey Jarvis", transcribes speech, responds with TTS and emoji face.
"""

import sys
import os
import time
import threading
import argparse
import readline
from pathlib import Path

# Add jarvis-voice to path
APP_DIR = Path(__file__).parent
sys.path.insert(0, str(APP_DIR))

from face import JarvisFace
from audio import AudioManager
from wakeword import WakeWordDetector
from stt import STTEngine
from tts import TTSEngine
from config import Config

VERSION = "0.1.0"


class Jarvis:
    def __init__(self, config: Config):
        self.config = config
        self.face = JarvisFace()
        self.audio = AudioManager(config)
        self.wakeword = WakeWordDetector(config, self.on_wake)
        self.stt = STTEngine(config)
        self.tts = TTSEngine(config)
        self.running = False
        self.conversation_active = False

    def on_wake(self):
        """Called when wake word is detected."""
        self.face.set_emotion("excited")
        print("\n🎤 Wake word detected — listening...")
        self.listen_and_respond()

    def listen_and_respond(self):
        """Listen for speech, process, respond, loop until silence."""
        self.conversation_active = True
        silence_count = 0
        max_silence = 2

        while self.conversation_active and silence_count < max_silence:
            self.face.set_emotion("listening")
            text = self.stt.listen()
            
            if text:
                silence_count = 0
                self.face.set_emotion("thinking")
                print(f"\n👤 You: {text}")

                # Get response from stdin (connected to OpenClaw)
                # For standalone mode, use terminal input
                response = self.get_response(text)
                
                if response:
                    self.face.set_emotion("speaking")
                    print(f"🤖 Jarvis: {response}")
                    self.tts.speak(response)
                    self.face.set_emotion("happy")
                    time.sleep(0.5)
            else:
                silence_count += 1
                if silence_count >= max_silence:
                    self.face.set_emotion("idle")
                    print("💤 Going back to sleep...")
                    self.conversation_active = False

    def get_response(self, text: str) -> str:
        """Override this to connect to OpenClaw or other AI backend."""
        # For now, simple echo + basic response
        text_lower = text.lower().strip()
        
        if "hello" in text_lower or "hi" in text_lower:
            return "Hello! How can I help you today?"
        elif "how are you" in text_lower:
            return "I'm doing great, thanks for asking! Ready to help."
        elif "your name" in text_lower:
            return "My name is Jarvis, your personal AI assistant."
        elif "thank" in text_lower:
            return "You're welcome!"
        elif "bye" in text_lower or "goodbye" in text_lower:
            return "Goodbye! I'll be here when you need me."
        elif text_lower.startswith(("what", "how", "why", "when", "where", "who")):
            return f"That's an interesting question. I'm not sure I have the answer right now, but I'm always learning."
        else:
            return "Got it. Let me think about that."

    def start(self):
        """Start all components."""
        print(f"\n🤖 Jarvis v{VERSION} — Starting up...")
        self.face.set_emotion("starting")
        
        # Check permissions
        if not self.audio.check_permissions():
            print("❌ Microphone permission required. Please grant it and restart.")
            sys.exit(1)
        
        # Start wake word detection
        self.wakeword.start()
        self.face.set_emotion("idle")
        print("👂 Listening for 'Hey Jarvis'... (Ctrl+C to quit)\n")
        
        self.running = True
        try:
            while self.running:
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("\n\n🛑 Shutting down...")
            self.stop()

    def stop(self):
        self.running = False
        self.wakeword.stop()
        self.audio.stop()
        self.face.set_emotion("off")
        print("👋 Goodbye!")


def main():
    parser = argparse.ArgumentParser(description="Jarvis Voice Assistant")
    parser.add_argument("--config", "-c", default=None, help="Config file path")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    args = parser.parse_args()

    config = Config(config_path=args.config, verbose=args.verbose)
    jarvis = Jarvis(config)
    jarvis.start()


if __name__ == "__main__":
    main()