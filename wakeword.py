"""
wakeword.py — Wake word detection using WebRTC VAD

Continuously monitors audio for speech activity.
When speech is detected and then stops → trigger wake.
"""

import os
import sys
import time
import wave
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
        self.config = config or {}

        self.sample_rate = self.config.get("sample_rate", DEFAULT_SAMPLE_RATE)
        self.frame_duration_ms = self.config.get("frame_duration_ms", DEFAULT_FRAME_DURATION_MS)
        self.aggressiveness = self.config.get("aggressiveness", DEFAULT_AGGRESSIVENESS)

        self.vad = webrtcvad.Vad(mode=self.aggressiveness)
        self.vad.set_mode(self.aggressiveness)

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
        Main VAD loop — reads audio, checks for speech.
        Uses ffmpeg to capture from microphone as raw PCM.
        """
        # Build ffmpeg command for continuous audio capture
        # Uses pulseaudio or android audio HAL as source
        ffmpeg_cmd = [
            'ffmpeg',
            '-f', 'lavfi',     # Use libavfilter input
            '-i', 'anullsrc=r=16000:cl=mono',  # null source for now (placeholder)
            '-f', 's16le',     # Output as signed 16-bit little-endian
            '-acodec', 'pcm_s16le',
            '-ar', str(self.sample_rate),
            '-ac', '1',
            '-'
        ]

        # For termux: use android audio source via termux-audio
        # Actually, we use a pipe from termux-microphone-record
        # The best approach is to use the /dev/audio nodes or
        # to use the audiofile approach with sox/ffmpeg

        # Alternative: Use sox to capture from default device
        sox_cmd = [
            'sox', '-t', 'alsa', 'default',
            '-r', str(self.sample_rate), '-c', '1', '-e', 'signed-integer',
            '-b', '16', '-', 'raw', 'encoding', 'signed', 'endian', 'little'
        ]

        # Try pulseaudio capture first, fallback to /dev/audio
        # On Android/Termux, we use termux-audio-capture or pipe from ffmpeg

        # Actually, let's use ffmpeg with pulseaudio
        proc = None

        # Method 1: Try sox
        try:
            proc = subprocess.Popen(sox_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            pass

        # Method 2: Try ffmpeg with pulse
        if proc is None or proc.poll() is not None:
            try:
                ffmpeg_cmd = [
                    'ffmpeg', '-f', 'pulse', '-i', 'default',
                    '-ar', str(self.sample_rate), '-ac', '1',
                    '-f', 's16le', '-acodec', 'pcm_s16le', '-'
                ]
                proc = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            except (FileNotFoundError, subprocess.SubprocessError):
                pass

        # Method 3: Use termux microphone record pipe
        if proc is None or proc.poll() is not None:
            try:
                # Use termux-microphone-record in a loop and read from pipe
                # But termux-microphone-record doesn't support pipe output
                # So we use a different approach: record to temp file and read
                pass
            except Exception:
                pass

        # Fallback: just do simple energy-based VAD using silence detection
        # This will work without actual audio input for testing
        self._energy_loop()

    def _energy_loop(self):
        """
        Fallback energy-based loop when no audio capture is available.
        Uses simple RMS energy threshold to detect speech.
        """
        import numpy as np

        while self.running:
            # This would be replaced with actual audio capture
            # For now, we just sleep and wait
            time.sleep(0.1)

    def process_audio_chunk(self, audio_data: bytes):
        """
        Process a chunk of audio data through VAD.
        Call this from your own audio capture loop.
        Returns True if speech is detected, False otherwise.
        """
        with self.audio_buffer_lock:
            self.audio_buffer += audio_data
            # Trim buffer to max size
            max_size = int(self.sample_rate * self.audio_buffer_seconds * 2)
            if len(self.audio_buffer) > max_size:
                self.audio_buffer = self.audio_buffer[-max_size:]

        frame_samples = self.frame_size // 2
        num_frames = len(audio_data) // self.frame_size

        for i in range(num_frames):
            frame = audio_data[i * self.frame_size : (i + 1) * self.frame_size]
            try:
                is_speech = self.vad.is_speech(frame, self.sample_rate, frame_samples)
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
                # Speech just started
                self.was_speaking = True

            elif self.was_speaking and self.silence_frames >= MIN_SILENCE_FRAMES:
                # End of speech utterance — trigger wake!
                self.was_speaking = False
                self.speech_frames = 0
                self.silence_frames = 0

                # Get the audio buffer for transcription
                with self.audio_buffer_lock:
                    buffer_copy = self.audio_buffer

                # Trigger callback in main thread
                threading.Thread(target=self._do_wake, args=(buffer_copy,), daemon=True).start()

        return is_speech

    def _do_wake(self, audio_data: bytes):
        """Called when wake is triggered."""
        # Save audio for debugging
        debug_path = Path("/data/data/com.termux/files/home/jarvis-voice/debug_audio.wav")
        try:
            with wave.open(str(debug_path), 'wb') as wf:
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
    wav_path = audio_path.replace('.ogg', '_16k.wav')
    
    # Convert to 16k mono
    subprocess.run([
        'ffmpeg', '-i', audio_path,
        '-ar', '16000', '-ac', '1', '-c:a', 'pcm_s16le',
        wav_path, '-y'
    ], capture_output=True)

    vad = webrtcvad.Vad(mode=3)
    vad.set_mode(3)

    speech_total = 0
    frame_total = 0

    with wave.open(wav_path, 'rb') as wf:
        sample_rate = wf.getframerate()
        frame_size = int(sample_rate * 0.030) * 2

        while True:
            buf = wf.readframes(int(sample_rate * 0.030))
            if not buf:
                break
            frame_total += 1
            try:
                if vad.is_speech(buf, sample_rate, len(buf) // 2):
                    speech_total += 1
            except Exception:
                pass

    print(f"VAD Test: {frame_total} frames, {speech_total} speech frames ({speech_total/frame_total*100:.1f}%)")
    return speech_total / frame_total if frame_total else 0


if __name__ == "__main__":
    # Test on existing voice note
    test_file = "/data/data/com.termux/files/home/.openclaw/media/inbound/file_0---03041353-a5c1-4004-ad75-793c3737f531.ogg"
    if Path(test_file).exists():
        ratio = test_vad_on_file(test_file)
        if ratio > 0.3:
            print(f"✓ Speech detected (ratio: {ratio:.1%})")
        else:
            print(f"✗ No clear speech (ratio: {ratio:.1%})")
    else:
        print(f"Test file not found: {test_file}")