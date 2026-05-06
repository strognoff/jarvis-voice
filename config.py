"""
config.py — Jarvis configuration
"""

import os
import json
from pathlib import Path


class Config:
    def __init__(self, config_path: str = None, verbose: bool = False):
        self.verbose = verbose

        # Default config
        self.mic_device = None  # None = default
        self.sample_rate = 16000
        self.audio_chunk_size = 1024
        
        # Wake word
        self.wake_word = "hey jarvis"
        self.wake_word_case_sensitive = False
        
        # STT (Speech-to-Text)
        # Options: "openai", "termux-api", "local"
        self.stt_engine = os.environ.get("JARVIS_STT_ENGINE", "termux-api")
        self.openai_api_key = os.environ.get("OPENAI_API_KEY", "")
        
        # TTS (Text-to-Speech)
        # Options: "termux-tts", "elevenlabs", "openai-tts"
        self.tts_engine = os.environ.get("JARVIS_TTS_ENGINE", "termux-tts")
        self.elevenlabs_api_key = os.environ.get("ELEVENLABS_API_KEY", "")

        # OpenClaw integration
        self.openclaw_session_key = os.environ.get("JARVIS_OPENCLAW_SESSION", "main")
        self.openclaw_api_url = os.environ.get("JARVIS_OPENCLAW_URL", "http://localhost:18789")
        self.openclaw_token = os.environ.get("JARVIS_OPENCLAW_TOKEN", "")

        # Load from file if exists
        if config_path:
            self.load_from_file(config_path)

    def load_from_file(self, path: str):
        path = Path(path)
        if not path.exists():
            return
        with open(path) as f:
            data = json.load(f)
        for key, value in data.items():
            setattr(self, key, value)

    def log(self, msg: str):
        if self.verbose:
            print(f"[Jarvis] {msg}")

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}