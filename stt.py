"""
stt.py — Speech-to-Text engine
Supports: termux-api (on-device), openai-whisper (API), local (faster-whisper)
"""

import os
import subprocess
import threading
import queue
from pathlib import Path


class STTEngine:
    def __init__(self, config):
        self.config = config
        self.engine = config.stt_engine or "termux-api"
        self.api_key = config.openai_api_key

    def listen(self, timeout: float = 10.0) -> str:
        """
        Listen for speech and return transcribed text.
        Returns empty string on failure/timeout.
        """
        if self.engine == "termux-api":
            return self._termux_stt(timeout)
        elif self.engine == "openai":
            return self._openai_stt(timeout)
        elif self.engine == "local":
            return self._local_stt(timeout)
        else:
            return self._termux_stt(timeout)

    def _termux_stt(self, timeout: float) -> str:
        """Use termux-speech-to-text for on-device STT (no API key needed)."""
        try:
            result = subprocess.run(
                ["termux-speech-to-text"],
                capture_output=True, text=True,
                timeout=timeout
            )
            text = result.stdout.strip()
            return text if text else ""
        except subprocess.TimeoutExpired:
            return ""
        except FileNotFoundError:
            print("termux-speech-to-text not found — install termux-api package")
            return ""
        except Exception as e:
            print(f"STT error: {e}")
            return ""

    def _openai_stt(self, timeout: float) -> str:
        """Use OpenAI Whisper API."""
        if not self.api_key:
            print("OPENAI_API_KEY not set — falling back to termux-api STT")
            return self._termux_stt(timeout)

        # Record audio to temp file
        import tempfile
        import wave

        temp_dir = Path(tempfile.gettempdir())
        audio_file = temp_dir / "jarvis_stt_input.wav"

        try:
            # Record using termux microphone
            rec_proc = subprocess.Popen(
                ["termux-microphone-record", "-f", str(audio_file)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            
            import time
            time.sleep(3)  # Record for 3 seconds
            
            # Stop recording
            rec_proc.terminate()
            rec_proc.wait()

            # Transcribe via OpenAI
            import requests
            with open(audio_file, 'rb') as f:
                files = {'file': f}
                data = {'model': 'whisper-1', 'language': 'en'}
                headers = {'Authorization': f'Bearer {self.api_key}'}
                resp = requests.post(
                    'https://api.openai.com/v1/audio/transcriptions',
                    files=files, data=data, headers=headers, timeout=30
                )
            
            if resp.status_code == 200:
                result = resp.json()
                return result.get('text', '').strip()
            else:
                print(f"OpenAI STT error: {resp.status_code} {resp.text}")
                return ""
        except Exception as e:
            print(f"OpenAI STT error: {e}")
            return ""
        finally:
            if audio_file.exists():
                audio_file.unlink()

    def _local_stt(self, timeout: float) -> str:
        """Use local faster-whisper (if installed and working)."""
        try:
            from faster_whisper import WhisperModel
            import tempfile
            import wave

            temp_dir = Path(tempfile.gettempdir())
            audio_file = temp_dir / "jarvis_stt_input.wav"

            # Record audio
            rec_proc = subprocess.Popen(
                ["termux-microphone-record", "-f", str(audio_file)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            import time
            time.sleep(3)
            rec_proc.terminate()
            rec_proc.wait()

            # Transcribe
            model = WhisperModel("tiny", device="cpu", compute_type="int8")
            segments, info = model.transcribe(str(audio_file), beam_size=5)
            text = "".join([seg.text for seg in segments]).strip()

            if audio_file.exists():
                audio_file.unlink()

            return text
        except ImportError:
            print("faster-whisper not installed — install with: pip install faster-whisper")
            return self._termux_stt(timeout)
        except Exception as e:
            print(f"Local STT error: {e}")
            return self._termux_stt(timeout)

    def transcribe_file(self, file_path: str) -> str:
        """Transcribe a specific audio file."""
        try:
            if self.engine == "openai" and self.api_key:
                return self._transcribe_openai_file(file_path)
            else:
                return self._transcribe_termux_file(file_path)
        except Exception as e:
            print(f"Transcribe file error: {e}")
            return ""

    def _transcribe_openai_file(self, file_path: str) -> str:
        """Transcribe an audio file using OpenAI Whisper API."""
        import requests

        with open(file_path, 'rb') as f:
            files = {'file': f}
            data = {'model': 'whisper-1', 'language': 'en'}
            headers = {'Authorization': f'Bearer {self.api_key}'}
            resp = requests.post(
                'https://api.openai.com/v1/audio/transcriptions',
                files=files, data=data, headers=headers, timeout=30
            )
        
        if resp.status_code == 200:
            return resp.json().get('text', '').strip()
        return ""

    def _transcribe_termux_file(self, file_path: str) -> str:
        """Use termux-speech-to-text with file input (if supported)."""
        # termux-speech-to-text doesn't support file input directly
        # Convert to compatible format and use termux API
        return ""