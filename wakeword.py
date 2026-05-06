"""
wakeword.py — Wake word detection ("Hey Jarvis")
Uses energy-based detection as a simple fallback when
proper wake word libraries aren't available.
"""

import time
import threading
import queue
import numpy as np


class WakeWordDetector:
    def __init__(self, config, callback):
        self.config = config
        self.callback = callback
        self.wake_word = config.wake_word.lower()
        self.running = False
        self.audio_queue = queue.Queue()
        self.audio = None

    def start(self):
        self.running = True
        # Start audio capture
        try:
            import pyaudio
            self.audio = pyaudio.PyAudio()
            stream = self.audio.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=16000,
                input=True,
                frames_per_buffer=1024
            )
            # Start detection loop in thread
            self._thread = threading.Thread(target=self._detection_loop, args=(stream,), daemon=True)
            self._thread.start()
        except ImportError:
            # Fall back to simple file monitoring
            self._thread = threading.Thread(target=self._simple_detection, daemon=True)
            self._thread.start()

    def _detection_loop(self, stream):
        """Continuously monitor audio stream for wake word pattern."""
        window_size = 16000  # 1 second
        energy_history = []
        triggered = False
        trigger_cooldown = 0

        while self.running:
            try:
                data = stream.read(1024, exception_on_overflow=False)
                audio_data = np.frombuffer(data, dtype=np.int16)
                energy = np.sqrt(np.mean(audio_data.astype(float)**2))

                energy_history.append(energy)
                if len(energy_history) > 30:  # keep 30 frames (~1 sec)
                    energy_history.pop(0)

                avg_energy = np.mean(energy_history)

                # Simple trigger: energy spike above average
                if energy > avg_energy * 4:
                    if not triggered and trigger_cooldown == 0:
                        triggered = True
                        # Callback in main thread
                        threading.Thread(target=self.callback, daemon=True).start()
                        trigger_cooldown = 30  # ~1 second cooldown

                if trigger_cooldown > 0:
                    trigger_cooldown -= 1

                # Reset if energy drops back to normal
                if energy < avg_energy * 1.5:
                    triggered = False

            except Exception as e:
                print(f"Detection error: {e}")
                time.sleep(0.1)

    def _simple_detection(self):
        """Fallback: wait for external trigger or file-based approach."""
        while self.running:
            time.sleep(0.5)
            # For now, just wait - termux-speech-totext handles wake
            pass

    def stop(self):
        self.running = False
        if self.audio:
            self.audio.terminate()

    def is_hotword(self, audio_chunk: bytes) -> bool:
        """
        Analyze an audio chunk for the wake word.
        Returns True if wake word is detected.
        For now uses VAD (voice activity detection) as a proxy.
        """
        try:
            audio = np.frombuffer(audio_chunk, dtype=np.int16)
            energy = np.sqrt(np.mean(audio.astype(float) ** 2))
            return energy > 2000  # Simple threshold
        except Exception:
            return False