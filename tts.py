"""
tts.py — Text-to-Speech engine
Supports: termux-tts (on-device), elevenlabs (API), openai-tts (API)
"""

import os
import subprocess
import threading


class TTSEngine:
    def __init__(self, config):
        self.config = config
        self.engine = config.tts_engine or "termux-tts"
        self.api_key = config.openai_api_key

    def speak(self, text: str):
        """
        Convert text to speech and play it.
        """
        if not text:
            return

        if self.engine == "termux-tts":
            self._termux_tts(text)
        elif self.engine == "elevenlabs":
            self._elevenlabs_tts(text)
        elif self.engine == "openai-tts":
            self._openai_tts(text)
        else:
            self._termux_tts(text)

    def _termux_tts(self, text: str):
        """Use Android's built-in TTS via termux-tts-speak."""
        try:
            subprocess.run(
                ["termux-tts-speak", text],
                capture_output=True, text=True, timeout=15
            )
        except FileNotFoundError:
            print("termux-tts-speak not found — install termux-api package")
        except subprocess.TimeoutExpired:
            print("TTS timeout")
        except Exception as e:
            print(f"TTS error: {e}")

    def _elevenlabs_tts(self, text: str):
        """Use ElevenLabs TTS API."""
        if not self.config.elevenlabs_api_key:
            print("ELEVENLABS_API_KEY not set — falling back to termux-tts")
            return self._termux_tts(text)

        try:
            import requests

            url = "https://api.elevenlabs.io/v1/text-to-speech/YOUR_VOICE_ID"
            headers = {
                "Accept": "audio/mp3",
                "Content-Type": "application/json",
                "xi-api-key": self.config.elevenlabs_api_key
            }
            data = {
                "text": text,
                "voice_id": "EXAVITQ4Xr0uq3Z3G5F5",  # Default voice
                "model_id": "eleven_monolingual_v1"
            }

            resp = requests.post(url, json=data, headers=headers, timeout=30)
            if resp.status_code == 200:
                import tempfile
                from pathlib import Path
                temp_dir = Path(tempfile.gettempdir())
                out_file = temp_dir / "jarvis_tts.mp3"
                with open(out_file, 'wb') as f:
                    f.write(resp.content)
                subprocess.Popen(
                    ["termux-media-player", "play", str(out_file)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
            else:
                print(f"ElevenLabs TTS error: {resp.status_code}")
        except Exception as e:
            print(f"ElevenLabs TTS error: {e}")

    def _openai_tts(self, text: str):
        """Use OpenAI TTS API."""
        if not self.api_key:
            print("OPENAI_API_KEY not set — falling back to termux-tts")
            return self._termux_tts(text)

        try:
            import requests
            import tempfile
            from pathlib import Path

            url = "https://api.openai.com/v1/audio/speech"
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }
            data = {
                "model": "tts-1",
                "input": text,
                "voice": "alloy",
                "response_format": "mp3"
            }

            resp = requests.post(url, json=data, headers=headers, timeout=30)
            if resp.status_code == 200:
                temp_dir = Path(tempfile.gettempdir())
                out_file = temp_dir / "jarvis_tts.mp3"
                with open(out_file, 'wb') as f:
                    f.write(resp.content)
                subprocess.Popen(
                    ["termux-media-player", "play", str(out_file)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
            else:
                print(f"OpenAI TTS error: {resp.status_code}")
        except Exception as e:
            print(f"OpenAI TTS error: {e}")

    def speak_async(self, text: str):
        """Speak text in a background thread."""
        t = threading.Thread(target=self.speak, args=(text,), daemon=True)
        t.start()

    def list_voices(self):
        """List available TTS voices (termux-tts)."""
        try:
            result = subprocess.run(
                ["termux-tts-engines"],
                capture_output=True, text=True, timeout=10
            )
            return result.stdout
        except Exception as e:
            return f"Error listing voices: {e}"