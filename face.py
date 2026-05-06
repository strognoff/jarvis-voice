"""
face.py — Jarvis animated ASCII/emoji face display
"""

import sys
import time
import threading
from pathlib import Path


FACES_FILE = Path(__file__).parent / "faces.txt"


class JarvisFace:
    EMOTIONS = [
        "start", "idle", "listening", "thinking", "speaking",
        "happy", "sad", "excited", "angry", "sleeping", "off"
    ]

    def __init__(self):
        self._current_emotion = "start"
        self._lock = threading.Lock()
        self._running = False
        self._thread = None
        self._animation_frames = 0
        self._faces = self._load_faces()

    def _load_faces(self) -> dict:
        """Parse faces.txt into a dict of emotion -> lines."""
        faces = {}
        current_emotion = None
        current_lines = []

        if not FACES_FILE.exists():
            return self._default_faces()

        with open(FACES_FILE) as f:
            for line in f:
                line = line.rstrip("\n")
                if not line.strip():
                    continue
                if line.startswith("[") and line.endswith("]"):
                    if current_emotion:
                        faces[current_emotion] = "\n".join(current_lines)
                    current_emotion = line[1:-1]
                    current_lines = []
                else:
                    current_lines.append(line)

        if current_emotion:
            faces[current_emotion] = "\n".join(current_lines)

        return faces if faces else self._default_faces()

    def _default_faces(self) -> dict:
        return {
            "start":   "🤖 JARVIS\n🤖 JARVIS",
            "idle":    "  ╭─────────╮\n  │  ¯\\_/¯  │\n  │  (o o)  │\n  ╰─────────╯",
            "listening": "  ╭─────────╮\n  │  ◉ ◉  │\n  │  (o o) │\n  ╰─────────╯",
            "thinking": "  ╭─────────╮\n  │  ¯◡¯  │\n  │  ( -) │\n  ╰─────────╯",
            "speaking": "  ╭─────────╮\n  │  ◉ ◉  │\n  │  (ω ω)│\n  ╰─────────╯",
            "happy":   "  ╭─────────╮\n  │  ◉ ◉  │\n  │  (‿‿) │\n  ╰─────────╯",
            "sad":     "  ╭─────────╮\n  │  · ·  │\n  │  (╥╥) │\n  ╰─────────╯",
            "excited": "  ╭─────────╮\n  │  ◉ ◉  │\n  │  (★ω★)│\n  ╰─────────╯",
            "angry":   "  ╭─────────╮\n  │  ╬ ╬  │\n  │  (⌣_⌣)│\n  ╰─────────╯",
            "sleeping": "  ╭─────────╮\n  │  · − ·  │\n  │  (--o) │\n  ╰─────────╯",
            "off":     "  ╭─────────╮\n  │  · ·  │\n  │  (x x) │\n  ╰─────────╯",
        }

    def set_emotion(self, emotion: str, duration: float = 0):
        """Set the current face emotion. Optional duration to auto-revert."""
        with self._lock:
            if emotion not in self._faces:
                emotion = "idle"
            self._current_emotion = emotion
            self._render()

        if duration > 0:
            def revert():
                time.sleep(duration)
                self.set_emotion("idle")
            threading.Thread(target=revert, daemon=True).start()

    def _render(self):
        """Print the current face to terminal."""
        face = self._faces.get(self._current_emotion, self._faces["idle"])
        # Move cursor up and clear previous face
        lines = face.count("\n") + 1
        sys.stdout.write(f"\033[{lines}A\033[K")
        sys.stdout.write(face + "\n")
        sys.stdout.flush()

    def animate(self, emotion: str, times: int = 3, interval: float = 0.3):
        """Animate an emotion briefly (e.g. blink)."""
        original = self._current_emotion
        for _ in range(times):
            self.set_emotion(emotion)
            time.sleep(interval)
        self.set_emotion(original)

    def start_animation_loop(self):
        """Start background idle animation (slow blink)."""
        self._running = True
        def loop():
            while self._running:
                time.sleep(4)
                if self._current_emotion == "idle":
                    self.set_emotion("idle")
        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()

    def stop_animation_loop(self):
        self._running = False