#!/usr/bin/env python3
"""
tui.py — Jarvis Voice TUI for OpenClaw

Always-on voice assistant:
- Uses termux-microphone-record (looped) + WebRTC VAD for continuous monitoring
- Detects end-of-speech → triggers termux-speech-to-text
- Sends to OpenClaw sub-session → speaks back via termux-tts-speak
- Animated emoji face state display

Run: python3 tui.py
"""

import sys
import os
import time
import subprocess
import threading
import wave
import struct
from pathlib import Path

# ── Config ─────────────────────────────────────────────────────────
GATEWAY_URL   = os.environ.get("JARVIS_GATEWAY_URL", "http://localhost:18789")
AUTH_TOKEN    = os.environ.get("JARVIS_AUTH_TOKEN", "")
SESSION_KEY   = os.environ.get("JARVIS_SESSION",    "agent:main:subagent:jarvis-tui")
SESSION_DIR   = "/data/data/com.termux/files/home/.openclaw/agents/main/sessions"
MEMORY_DIR    = "/data/data/com.termux/files/home/.openclaw/memory"

# Audio settings  
SAMPLE_RATE   = 16000
FRAME_DURATION_MS = 30  # 30ms WebRTC standard
ENERGY_THRESHOLD  = 800

# ── WebRTC VAD ──────────────────────────────────────────────────────
try:
    import webrtcvad
    HAS_VAD = True
except ImportError:
    HAS_VAD = False

# ── Helpers (no numpy) ─────────────────────────────────────────────
def compute_rms(audio_bytes: bytes) -> float:
    """Compute RMS energy of 16-bit PCM without numpy."""
    n = len(audio_bytes) // 2
    if n == 0:
        return 0.0
    total = 0
    for i in range(0, len(audio_bytes) - 1, 2):
        s = struct.unpack_from('<h', audio_bytes, i)[0]
        total += s * s
    return (total / n) ** 0.5

# ── Face Emojis ─────────────────────────────────────────────────────
FACES = {
    "idle":      "🤖",
    "listening": "🎤",
    "thinking":  "🤔",
    "speaking":  "🔊",
    "happy":     "😊",
    "done":      "✅",
    "error":     "❌",
}

def log(msg):
    print(f"[jarvis-tui] {msg}", flush=True)

def print_banner():
    print()
    print(f"  ╔══════════════════════════════════════╗  v{__version__}")
    print("  ║       🤖 JARVIS VOICE TUI 🤖         ║")
    print("  ╚══════════════════════════════════════╝")
    print()

def render(face, status=""):
    emoji = FACES.get(face, FACES["idle"])
    line = f"  {emoji} {status}" if status else f"  {emoji}"
    print(line)

def speak(text: str):
    try:
        subprocess.run(["termux-tts-speak", text], capture_output=True, timeout=20)
    except Exception as e:
        log(f"TTS error: {e}")

def listen() -> str:
    """Use termux-speech-to-text to capture and transcribe speech."""
    try:
        result = subprocess.run(
            ["termux-speech-to-text"],
            capture_output=True, text=True, timeout=20
        )
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        return ""
    except Exception as e:
        log(f"STT error: {e}")
        return ""

def save_wav(path, audio_data, sample_rate=SAMPLE_RATE):
    with wave.open(str(path), 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(audio_data)

# ── OpenClaw Gateway ────────────────────────────────────────────────
def send_message(session_key: str, text: str) -> str:
    import requests
    url = f"{GATEWAY_URL}/sessions/{session_key}/messages"
    try:
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {AUTH_TOKEN}"},
            json={"kind": "text", "content": text,
                  "sender": {"id": "jarvis-tui", "label": "jarvis-tui"}},
            timeout=60
        )
        if resp.status_code == 200:
            result = resp.json()
            return result.get("reply", "")
    except Exception as e:
        log(f"Gateway error: {e}")
    return ""

def get_response(text: str) -> str:
    """Local fallback response generator."""
    t = text.lower().strip()
    if any(g in t for g in ["hello", "hi", "hey"]):
        return "Hello! I'm Jarvis. What can I do for you today?"
    elif "how are you" in t:
        return "I'm doing great, thank you for asking!"
    elif any(g in t for g in ["your name", "who are you"]):
        return "I'm Jarvis, your personal AI assistant."
    elif "time" in t:
        from datetime import datetime
        return f"It's {datetime.now().strftime('%I:%M %p')} in London."
    elif "date" in t:
        from datetime import datetime
        return f"Today is {datetime.now().strftime('%A, %B %d')}"
    elif "weather" in t:
        return "I can check the weather for you. Which location?"
    elif "joke" in t:
        return "Why did the AI cross the road? To get to the other side of the neural network!"
    elif "thank" in t:
        return "You're welcome!"
    elif any(g in t for g in ["bye", "goodbye"]):
        return "Goodbye! I'll be here when you need me."
    else:
        return "Got it. Let me think about that."

# ── VAD Capture Loop ────────────────────────────────────────────────
class VADLoop:
    """
    WebRTC VAD loop using termux-microphone-record.
    
    Records audio to a rotating temp file, reads it in 30ms chunks,
    feeds through VAD. On speech-end detected, triggers callback.
    """

    def __init__(self, sample_rate=SAMPLE_RATE):
        self.sample_rate = sample_rate
        self.frame_size = int(sample_rate * FRAME_DURATION_MS / 1000) * 2  # bytes
        self.running = False
        self.vad = None

        if HAS_VAD:
            self.vad = webrtcvad.Vad(mode=3)
            self.vad.set_mode(3)
            log(f"WebRTC VAD initialised (mode 3)")
        else:
            log("WARNING: WebRTC VAD not available — using energy-only fallback")

    def _record_chunk(self, out_path: str, duration_s: float):
        """Record a short audio chunk using termux-microphone-record."""
        try:
            proc = subprocess.Popen(
                ['termux-microphone-record', '-f', out_path, '-d'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            time.sleep(duration_s)
            proc.terminate()
            proc.wait()
        except Exception as e:
            log(f"record error: {e}")

    def vad_loop(self, on_wake, silence_threshold_frames=5):
        """
        Main loop: record small chunks → read PCM → VAD check.
        When speech ends (silence_threshold_frames consecutive silence),
        trigger on_wake callback with the speech audio captured so far.
        """
        work_dir = Path("/data/data/com.termux/files/home/jarvis-voice")
        chunk_file = work_dir / "vad_chunk.m4a"
        wav_file = work_dir / "vad_chunk.wav"

        speech_buffer = b""
        speech_frames = 0
        silence_frames = 0
        min_speech = 2      # min frames of actual speech to trigger wake
        min_silence = silence_threshold_frames  # ~600ms silence = end of utterance
        is_speaking = False
        last_size = 0
        threshold = ENERGY_THRESHOLD  # local alias, avoids late binding issues in loops

        while self.running:
            # ── Record a ~500ms chunk ────────────────────────────────
            # Remove old chunk file first
            if chunk_file.exists():
                try:
                    os.remove(chunk_file)
                except Exception:
                    pass
            if wav_file.exists():
                try:
                    os.remove(wav_file)
                except Exception:
                    pass

            # Start recording (no -d flag — record until we kill it)
            record_proc = subprocess.Popen(
                ['termux-microphone-record', '-f', str(chunk_file)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            time.sleep(0.6)  # record for ~600ms
            # Kill hard — sigkill ensures process dies immediately
            record_proc.kill()
            try:
                record_proc.wait()
            except Exception:
                pass
            time.sleep(0.05)  # let filesystem flush

            # Check file was created
            if not chunk_file.exists():
                print('[vad] skip: chunk file not created')
                continue

            file_size = chunk_file.stat().st_size
            if file_size < 500:
                print(f'[vad] skip: file too small ({file_size}B)')
                continue  # too small, likely empty/broken

            # Convert to 16k mono PCM for VAD
            try:
                subprocess.run([
                    'ffmpeg', '-i', str(chunk_file),
                    '-ar', '16000', '-ac', '1', '-acodec', 'pcm_s16le',
                    '-frames:v', '0',
                    str(wav_file), '-y'
                ], capture_output=True, timeout=5)
            except Exception as e:
                print(f'[vad] skip: ffmpeg error: {e}')
                continue

            if not wav_file.exists():
                print('[vad] skip: wav file not created after ffmpeg')
                continue


            with wave.open(str(wav_file), 'rb') as wf:
                chunk_data = wf.readframes(wf.getnframes())

            if not chunk_data:
                print('[vad] skip: no audio data in wav')
                continue

            # ── VAD check on this chunk ───────────────────────────────
            num_samples = len(chunk_data) // 2
            is_speech = False

            if self.vad:
                try:
                    is_speech = self.vad.is_speech(chunk_data, self.sample_rate, num_samples)
                except Exception as e:
                    pass  # VAD threw on this chunk, fall through to energy

            # Energy fallback
            rms = compute_rms(chunk_data)
            if rms > threshold:
                is_speech = True

            # ── Debug: log every iteration (chunk stats) ─────────────
            chunk_bytes = len(chunk_data)
            debug_msg = (f"  chunk={chunk_bytes}B rms={rms:.0f} thr={threshold} "
                         f"vad={is_speech} buf={len(speech_buffer)//64}f "
                         f"speak={is_speaking} silence={silence_frames}")
            print(f"[vad] {debug_msg}", flush=True)  # always on its own line

            if is_speech:
                speech_frames += 1
                silence_frames = 0
                speech_buffer += chunk_data
                is_speaking = True
            else:
                if is_speaking:
                    silence_frames += 1
                    if silence_frames >= min_silence:
                        # End of utterance!
                        if speech_frames >= min_speech:
                            log(f"Speech end detected — {len(speech_buffer)//64} frames, RMS={rms:.0f}")
                            on_wake(speech_buffer)
                        # Reset
                        speech_buffer = b""
                        speech_frames = 0
                        silence_frames = 0
                        is_speaking = False
                else:
                    silence_frames = 0
                    speech_frames = 0

            # Trim buffer to 30s max
            max_bytes = SAMPLE_RATE * 30 * 2
            if len(speech_buffer) > max_bytes:
                speech_buffer = speech_buffer[-max_bytes:]

            # Detect if file size changed (mic was actually recording)
            cur_size = chunk_file.stat().st_size if chunk_file.exists() else 0
            last_size = cur_size

    def start(self):
        self.running = True

    def stop(self):
        self.running = False


# ── Main Jarvis TUI ────────────────────────────────────────────────
class JarvisTUI:
    def __init__(self):
        self.state = "idle"
        self.vad = None
        self.running = False

    def on_wake(self, audio_data: bytes):
        """Called when VAD detects end of speech utterance."""
        log(f"Wake! Audio buffer: {len(audio_data)//64} frames")
        self.state = "listening"
        render("listening", "Listening...")

        # Save for debug
        save_wav(Path("/data/data/com.termux/files/home/jarvis-voice/wake_audio.wav"), audio_data)

        # Use termux-speech-to-text to transcribe
        text = listen()

        if text:
            log(f"You: {text}")
            self.state = "thinking"
            render("thinking", "Thinking...")

            response = send_message(SESSION_KEY, text)
            if not response:
                response = get_response(text)

            log(f"Jarvis: {response}")
            self.state = "speaking"
            render("speaking", "Speaking...")
            speak(response)
            self.state = "done"
            time.sleep(1.5)
        else:
            log("(no speech detected)")

        self.state = "idle"

    def run(self):
        print_banner()

        # Verify termux-api
        try:
            subprocess.run(["termux-tts-engines"], capture_output=True, timeout=5)
        except FileNotFoundError:
            print("  ❌ termux-api not found! Run: pkg install termux-api")
            sys.exit(1)

        log(f"Gateway: {GATEWAY_URL}")
        log(f"Session: {SESSION_KEY}")
        log(f"VAD: {'WebRTC VAD ✓' if HAS_VAD else 'Energy-only (no webrtcvad)'}")
        log("Starting VAD loop... (Ctrl+C to stop)")
        print()

        self.running = True
        self.vad = VADLoop()

        vad_thread = threading.Thread(target=self.vad.vad_loop, args=(self.on_wake,), daemon=True)
        self.vad.start()
        vad_thread.start()

        try:
            while self.running:
                if self.state == "idle":
                    render("idle", "Listening for speech...")
                elif self.state == "listening":
                    render("listening", "Listening...")
                elif self.state == "thinking":
                    render("thinking", "Thinking...")
                elif self.state == "speaking":
                    render("speaking", "Speaking...")
                elif self.state == "done":
                    render("done", "Ready")
                time.sleep(0.25)
        except KeyboardInterrupt:
            print("\n\n  👋 Shutting down...")
            self.vad.stop()
            self.running = False


def test_mic(duration=1.0, out_path='/data/data/com.termux/files/home/jarvis-voice/mic_test.m4a'):
    """Quick mic test — records `duration` seconds and checks if file is created."""
    out = Path(out_path)
    if out.exists():
        out.unlink()

    print(f"🎤 Recording {duration}s...")

    proc = subprocess.Popen(
        ['termux-microphone-record', '-f', str(out), '-d'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    time.sleep(duration)
    proc.terminate()
    proc.wait()

    if not out.exists():
        print(f"❌ FAIL — file not created: {out}")
        print("   → Is termux-api installed? (pkg install termux-api)")
        print("   → Does Termux have microphone permission?")
        return False

    size = out.stat().st_size
    if size < 100:
        print(f"❌ FAIL — file too small ({size} bytes), likely empty recording")
        return False

    print(f"✅ Mic working! File: {out} ({size} bytes)")

    # Quick ffprobe check
    probe = subprocess.run(
        ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_format', str(out)],
        capture_output=True, text=True, timeout=10
    )
    if probe.returncode == 0:
        import json
        info = json.loads(probe.stdout)
        dur = info.get('format', {}).get('duration', '?')
        print(f"   Duration: {float(dur):.1f}s")
    return True


__version__ = "0.2.0"

if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] in ("--version", "-v"):
        print(f"jarvis-tui {__version__}")
        sys.exit(0)

    if len(sys.argv) > 1 and sys.argv[1] in ("--test-mic", "--mic-test", "-t"):
        import argparse
        parser = argparse.ArgumentParser(description=f"Jarvis Voice TUI v{__version__} — mic test")
        parser.add_argument("-d", "--duration", type=float, default=1.0)
        parser.add_argument("-f", "--file", default="/data/data/com.termux/files/home/jarvis-voice/mic_test.m4a")
        args, _ = parser.parse_known_args()
        ok = test_mic(duration=args.duration, out_path=args.file)
        sys.exit(0 if ok else 1)

    JarvisTUI().run()