"""
wakeword.py — Wake word detection using WebRTC VAD

Continuously monitors audio for speech activity.
When speech is detected and then stops → trigger wake.
"""

import os
import sys
import time
import wave
import tempfile
import subprocess
import threading
import queue
import webrtcvad
from pathlib import Path

# ── Config ─────────────────────────────────────────────────────────
DEFAULT_SAMPLE_RATE = 16000
DEFAULT_FRAME_DURATION_MS = 30  # 30ms frames
DEFAULT_AGGRESSIVENESS = 3  # 0-3, higher = more aggressive filtering
MIN_SPEECH_FRAMES = 3  # Min consecutive speech frames to count as talking
MIN_SILENCE_FRAMES = 10  # Min silence frames after speech to trigger wake


class WakeWordDetector:
    """
    Uses WebRTC VAD to continuously monitor audio for speech.
    Detects when a person starts speaking, waits for end of utterance,
    then triggers the callback.

    Works entirely locally — no API keys, no cloud.
    """

    def __init__(self, config=None, callback=None):
        self.callback = callback or (lambda: None)

        # Accept either a Config object or a plain dict
        if config is None:
            cfg = {}
        elif isinstance(config, dict):
            cfg = config
        else:
            # Config object — pull relevant attributes
            cfg = {
                "sample_rate": getattr(config, "sample_rate", DEFAULT_SAMPLE_RATE),
                "frame_duration_ms": getattr(config, "frame_duration_ms", DEFAULT_FRAME_DURATION_MS),
                "aggressiveness": getattr(config, "aggressiveness", DEFAULT_AGGRESSIVENESS),
                "mic_device": getattr(config, "mic_device", None),
            }
        self._raw_config = config

        self.sample_rate = cfg.get("sample_rate", DEFAULT_SAMPLE_RATE)
        self.frame_duration_ms = cfg.get("frame_duration_ms", DEFAULT_FRAME_DURATION_MS)
        self.aggressiveness = cfg.get("aggressiveness", DEFAULT_AGGRESSIVENESS)
        self.mic_device = cfg.get("mic_device", None)

        self.vad = webrtcvad.Vad(self.aggressiveness)

        self.running = False
        self._thread = None

        # Speech state tracking
        self.speech_frames = 0
        self.silence_frames = 0
        self.was_speaking = False

        # How many seconds of audio to keep for STT after wake
        self.audio_buffer_seconds = 10
        self.audio_buffer = b""
        self.audio_buffer_lock = threading.Lock()

    @property
    def frame_size(self) -> int:
        """Bytes per frame (16-bit mono = 2 bytes per sample)."""
        return int(self.sample_rate * self.frame_duration_ms / 1000) * 2

    def start(self):
        """Start the VAD loop in a background thread."""
        self.running = True
        self._thread = threading.Thread(target=self._vad_loop, daemon=True)
        self._thread.start()
        return self

    def _vad_loop(self):
        """
        Main VAD loop — reads raw PCM audio and feeds it to process_audio_chunk.
        Tries pyaudio first, then ffmpeg (pulseaudio/avfoundation/alsa), then sox.
        """
        # ── Method 1: PyAudio (best cross-platform support) ────────────
        try:
            import pyaudio

            def _pa_capture():
                pa = pyaudio.PyAudio()
                kwargs = dict(
                    format=pyaudio.paInt16,
                    channels=1,
                    rate=self.sample_rate,
                    input=True,
                    frames_per_buffer=self.frame_size // 2,
                )
                if self.mic_device is not None:
                    kwargs["input_device_index"] = self.mic_device
                stream = pa.open(**kwargs)
                stream.start_stream()
                try:
                    while self.running:
                        data = stream.read(self.frame_size // 2, exception_on_overflow=False)
                        self.process_audio_chunk(data)
                finally:
                    stream.stop_stream()
                    stream.close()
                    pa.terminate()

            _pa_capture()
            return
        except ImportError:
            pass
        except Exception as e:
            print(f"[wakeword] PyAudio capture failed: {e}", flush=True)

        # ── Method 2: ffmpeg ────────────────────────────────────────────
        # Pick the right input format per OS
        import platform
        system = platform.system()
        if system == "Darwin":
            ffmpeg_fmt, ffmpeg_src = "avfoundation", ":0"
        elif system == "Linux":
            ffmpeg_fmt, ffmpeg_src = "pulse", "default"
        else:
            ffmpeg_fmt, ffmpeg_src = "dshow", "audio=default"

        ffmpeg_cmd = [
            "ffmpeg", "-loglevel", "quiet",
            "-f", ffmpeg_fmt, "-i", ffmpeg_src,
            "-ar", str(self.sample_rate), "-ac", "1",
            "-f", "s16le", "-",
        ]
        try:
            proc = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            while self.running:
                data = proc.stdout.read(self.frame_size)
                if not data:
                    break
                self.process_audio_chunk(data)
            proc.terminate()
            return
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"[wakeword] ffmpeg capture failed: {e}", flush=True)

        # ── Method 3: sox ───────────────────────────────────────────────
        sox_cmd = [
            "sox", "-q", "-t", "alsa", "default",
            "-r", str(self.sample_rate), "-c", "1",
            "-e", "signed-integer", "-b", "16",
            "-t", "raw", "-",
        ]
        try:
            proc = subprocess.Popen(sox_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            while self.running:
                data = proc.stdout.read(self.frame_size)
                if not data:
                    break
                self.process_audio_chunk(data)
            proc.terminate()
            return
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"[wakeword] sox capture failed: {e}", flush=True)

        print("[wakeword] ⚠️  No audio capture backend found (tried pyaudio, ffmpeg, sox).", flush=True)
        print("[wakeword]    Install pyaudio: pip install pyaudio", flush=True)

    def process_audio_chunk(self, audio_data: bytes):
        """
        Process a chunk of audio data through VAD.
        Call this from your own audio capture loop.
        Returns True if the last frame was detected as speech.
        """
        with self.audio_buffer_lock:
            self.audio_buffer += audio_data
            # Trim buffer to max size
            max_size = int(self.sample_rate * self.audio_buffer_seconds * 2)
            if len(self.audio_buffer) > max_size:
                self.audio_buffer = self.audio_buffer[-max_size:]

        is_speech = False
        num_frames = len(audio_data) // self.frame_size

        for i in range(num_frames):
            frame = audio_data[i * self.frame_size: (i + 1) * self.frame_size]
            if len(frame) < self.frame_size:
                continue
            try:
                # webrtcvad.Vad.is_speech(frame, sample_rate) — two arguments only
                is_speech = self.vad.is_speech(frame, self.sample_rate)
            except Exception:
                is_speech = False

            if is_speech:
                self.speech_frames += 1
                self.silence_frames = 0
            else:
                self.silence_frames += 1
                if self.speech_frames > 0:
                    self.speech_frames -= 1

            # State machine
            if self.speech_frames >= MIN_SPEECH_FRAMES and not self.was_speaking:
                self.was_speaking = True

            elif self.was_speaking and self.silence_frames >= MIN_SILENCE_FRAMES:
                self.was_speaking = False
                self.speech_frames = 0
                self.silence_frames = 0

                with self.audio_buffer_lock:
                    buffer_copy = self.audio_buffer

                threading.Thread(target=self._do_wake, args=(buffer_copy,), daemon=True).start()

        return is_speech

    def _do_wake(self, audio_data: bytes):
        """Called when wake is triggered."""
        # Save audio for debugging to a portable temp path
        debug_path = Path(tempfile.gettempdir()) / "jarvis_debug_audio.wav"
        try:
            with wave.open(str(debug_path), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(self.sample_rate)
                wf.writeframes(audio_data)
        except Exception:
            pass

        self.callback()

    def stop(self):
        self.running = False
        if self._thread:
            self._thread.join(timeout=2)


def test_vad_on_file(audio_path: str):
    """Test VAD on an existing audio file."""
    wav_path = audio_path.replace(".ogg", "_16k.wav")

    # Convert to 16k mono
    subprocess.run(
        ["ffmpeg", "-i", audio_path, "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", wav_path, "-y"],
        capture_output=True,
    )

    vad = webrtcvad.Vad(3)

    speech_total = 0
    frame_total = 0

    with wave.open(wav_path, "rb") as wf:
        sample_rate = wf.getframerate()
        frame_size = int(sample_rate * 0.030) * 2

        while True:
            buf = wf.readframes(int(sample_rate * 0.030))
            if not buf or len(buf) < frame_size:
                break
            frame_total += 1
            try:
                if vad.is_speech(buf, sample_rate):
                    speech_total += 1
            except Exception:
                pass

    print(f"VAD Test: {frame_total} frames, {speech_total} speech frames ({speech_total / frame_total * 100:.1f}%)")
    return speech_total / frame_total if frame_total else 0


if __name__ == "__main__":
    test_file = "/data/data/com.termux/files/home/.openclaw/media/inbound/file_0---03041353-a5c1-4004-ad75-793c3737f531.ogg"
    if Path(test_file).exists():
        ratio = test_vad_on_file(test_file)
        if ratio > 0.3:
            print(f"✓ Speech detected (ratio: {ratio:.1%})")
        else:
            print(f"✗ No clear speech (ratio: {ratio:.1%})")
    else:
        print(f"Test file not found: {test_file}")
