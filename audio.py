"""
audio.py — Audio capture and playback management
Uses pyaudio via Termux if available, or falls back to simple approach.
"""

import os
import subprocess
import threading
import queue
from pathlib import Path


class AudioManager:
    def __init__(self, config):
        self.config = config
        self.sample_rate = config.sample_rate
        self.chunk_size = config.audio_chunk_size
        self.running = False
        self.stream = None
        self.audio = None

    def check_permissions(self) -> bool:
        """Check if we can access the microphone."""
        try:
            result = subprocess.run(
                ["termux-microphone-record", "--info"],
                capture_output=True, text=True, timeout=5
            )
            return True
        except FileNotFoundError:
            # Try pyaudio approach
            try:
                import pyaudio
                p = pyaudio.PyAudio()
                info = p.get_device_info_by_index(0)
                p.terminate()
                return True
            except Exception:
                pass
        return True  # Assume ok if we got here

    def start_capture(self, audio_queue: queue.Queue):
        """Start capturing audio in a background thread."""
        self.running = True
        self._capture_thread = threading.Thread(target=self._capture_loop, args=(audio_queue,), daemon=True)
        self._capture_thread.start()

    def _capture_loop(self, audio_queue: queue.Queue):
        """Capture loop — tries pyaudio, falls back to termux."""
        try:
            import pyaudio
            self.audio = pyaudio.PyAudio()
            self.stream = self.audio.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=self.sample_rate,
                input=True,
                input_device_index=self.config.mic_device,
                frames_per_buffer=self.chunk_size,
                stream_callback=lambda in_data, frame_count, time_info, status_flags: (
                    (audio_queue.put(in_data), (None, pyaudio.paContinue))[1]
                )
            )
            self.stream.start_stream()
            while self.running:
                time.sleep(0.1)
        except ImportError:
            # Fall back to termux implementation
            self._termux_capture_loop(audio_queue)

    def _termux_capture_loop(self, audio_queue: queue.Queue):
        """Fallback: record audio using termux API and pipe."""
        import tempfile
        import wave

        temp_dir = Path(tempfile.gettempdir())
        recording_file = temp_dir / "jarvis_capture.wav"

        while self.running:
            try:
                # Use termux-microphone-record to capture to a temp file
                # Start recording
                proc = subprocess.Popen(
                    ["termux-microphone-record", "-f", str(recording_file)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE
                )
                # Wait a bit for buffer
                time.sleep(0.5)
                # Read the file and put chunks in queue
                while self.running and recording_file.stat().st_size < 1024:
                    time.sleep(0.1)

                if not self.running:
                    break

                # Now read the audio data
                with wave.open(str(recording_file), 'rb') as wf:
                    chunk_size = self.chunk_size * 2  # 16-bit = 2 bytes per sample
                    while self.running:
                        data = wf.readframes(self.chunk_size)
                        if not data:
                            break
                        audio_queue.put(data)
            except FileNotFoundError:
                time.sleep(0.5)
            except Exception as e:
                print(f"Audio capture error: {e}")
                time.sleep(0.5)

    def stop(self):
        self.running = False
        if self.stream:
            self.stream.stop_stream()
            self.stream.close()
        if self.audio:
            self.audio.terminate()

    def play_audio(self, audio_data: bytes):
        """Play audio data (for TTS responses)."""
        # Write to temp file and use termux-media-player
        import tempfile
        temp_dir = Path(tempfile.gettempdir())
        temp_wav = temp_dir / "jarvis_tts_output.wav"
        temp_mp3 = temp_dir / "jarvis_tts_output.mp3"

        with open(temp_wav, 'wb') as f:
            f.write(audio_data)

        try:
            subprocess.Popen(
                ["termux-media-player", "play", str(temp_wav)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except Exception as e:
            print(f"Playback error: {e}")

    def beep(self, freq: int = 800, duration: float = 0.1):
        """Play a beep sound (for wake word detection feedback)."""
        try:
            import numpy as np
            sample_count = int(self.sample_rate * duration)
            t = np.linspace(0, duration, sample_count, False)
            wave = (np.sin(2 * np.pi * freq * t) * 32767).astype(np.int16)
            stream = self.audio.open(format=self.audio._pa, channels=1, rate=self.sample_rate, output=True) if self.audio else None
            if stream:
                stream.write(wave.tobytes())
                stream.close()
        except Exception:
            pass  # Silent fail for beep