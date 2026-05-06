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
_recording_lock = threading.Lock()  # shared lock for mic recording
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
                ['termux-microphone-record', '-f', out_path, '-l', str(int(duration))],
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

        # Kill any stale recording from a previous run
        subprocess.run(['termux-microphone-record', '-q'],
                       capture_output=True, timeout=5)
        time.sleep(0.3)

        speech_buffer = b""
        speech_frames = 0
        silence_frames = 0
        min_speech = 2      # min frames of actual speech to trigger wake
        min_silence = silence_threshold_frames  # ~600ms silence = end of utterance
        is_speaking = False
        last_size = 0
        threshold = ENERGY_THRESHOLD  # local alias, avoids late binding issues in loops

        while self.running:
            # ── S1: Remove old files ────────────────────────────────
            for f in (chunk_file, wav_file):
                if f.exists():
                    try:
                        os.remove(f)
                    except Exception:
                        pass

            # Use lock so test functions can record without race conditions
            with _recording_lock:
                record_proc = subprocess.Popen(
                    ['termux-microphone-record', '-f', str(chunk_file), '-l', '1'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                record_proc.wait()  # wait for -l to auto-exit after 1s

            # ── S4: Check file was created ─────────────────────────
            if not chunk_file.exists():
                print('[vad] ⚠ chunk file not created after record — is mic working?')
                continue

            file_size = chunk_file.stat().st_size
            if file_size < 2000:
                print(f'[vad] ⚠ file too small ({file_size}B) — is mic working?')
                continue

            # ── S5: Convert to 16k mono PCM ───────────────────────
            try:
                result = subprocess.run([
                    'ffmpeg', '-i', str(chunk_file),
                    '-ar', '16000', '-ac', '1', '-acodec', 'pcm_s16le',
                    '-frames:v', '0',
                    str(wav_file), '-y'
                ], capture_output=True, timeout=5)
                if result.returncode != 0:
                    print(f'[vad] skip: ffmpeg failed rc={result.returncode} '
                          f'stderr={result.stderr[:120]}')
                    continue
            except Exception as e:
                print(f'[vad] skip: ffmpeg exception: {e}')
                continue

            if not wav_file.exists():
                print('[vad] skip: wav file not created after ffmpeg')
                continue


            # ── S6: Read WAV data ────────────────────────────────
            with wave.open(str(wav_file), 'rb') as wf:
                chunk_data = wf.readframes(wf.getnframes())

            if not chunk_data:
                print('[vad] skip: no audio data in wav')
                continue

            # ── S7: VAD check on this chunk ───────────────────────────────
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

            # ── S8: State machine ─────────────────────────────────
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




def test_vad_synthetic():
    """Test VAD on generated synthetic audio (no microphone needed). Returns (ok, message)."""
    import webrtcvad, wave, struct, math, os
    from pathlib import Path

    work_dir = Path("/data/data/com.termux/files/home/jarvis-voice")
    wav_file = work_dir / "vad_synthetic_test.wav"

    print("  🎙️  Testing WebRTC VAD on synthetic audio (no microphone)", flush=True)

    SAMPLE_RATE = 16000
    vad = webrtcvad.Vad(mode=3)

    # Generate 3 seconds: 1s silence, 1s 1kHz tone, 1s silence
    audio = b""
    for i in range(SAMPLE_RATE * 3):
        t = i / SAMPLE_RATE
        if 1.0 <= t < 2.0:
            # 1 second of 1kHz tone at 80% amplitude
            sample = int(0.8 * 16000 * math.sin(2 * math.pi * 1000 * t))
        else:
            sample = 0
        audio += struct.pack('<h', sample)

    # Save WAV
    with wave.open(str(wav_file), 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio)

    # Run VAD
    frame_size = int(SAMPLE_RATE * 30 / 1000) * 2  # 960 bytes
    n_speech = n_total = 0

    for i in range(0, len(audio) - frame_size, frame_size):
        chunk = audio[i:i+frame_size]
        n_total += 1
        try:
            is_sp = vad.is_speech(chunk, SAMPLE_RATE, 480)
        except Exception:
            is_sp = False
        if is_sp:
            n_speech += 1

    pct = n_speech / n_total * 100 if n_total > 0 else 0

    # Expected: ~33% speech (1s of 3s)
    # Allow wide margin: 20-60% is reasonable for 1kHz tone
    os.remove(wav_file)

    print(f"  📊 VAD on synthetic 1kHz tone (1s speech / 3s total):", flush=True)
    print(f"     Speech detected: {n_speech}/{n_total} frames ({pct:.1f}%)", flush=True)

    if pct < 15:
        return False, f"VAD detected only {pct:.1f}% — expected ~33% for 1kHz tone. VAD may be broken."
    elif pct > 70:
        return False, f"VAD detected {pct:.1f}% — too much. VAD may be too sensitive."
    else:
        return True, f"VAD working correctly ({pct:.1f}% detected, expected ~33%)"

def test_mic_vad():
    """Test microphone recording + VAD on real audio. Returns (ok, message)."""
    import tempfile, webrtcvad, wave, struct, os, subprocess

    work_dir = Path("/data/data/com.termux/files/home/jarvis-voice")
    chunk_file = work_dir / "vad_test_chunk.m4a"
    wav_file = work_dir / "vad_test_chunk.wav"

    # Clean up
    for f in (chunk_file, wav_file):
        if f.exists():
            os.remove(f)

    # Kill any existing recording first
    subprocess.run(['termux-microphone-record', '-q'],
                   capture_output=True, timeout=5)
    time.sleep(0.5)

    # Step 1: Record 3 seconds using -d (same approach that works in _record_chunk)
    print("  🎤 Recording 3 seconds from microphone...", flush=True)
    print("     (Speak clearly during these 3 seconds)", flush=True)

    with _recording_lock:
        record_proc = subprocess.Popen(
            ['termux-microphone-record', '-f', str(chunk_file), '-l', '3'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        record_proc.wait()  # wait for -l 3 to auto-exit

    if not chunk_file.exists():
        return False, "Microphone file not created — is Termux microphone permission granted? (Settings > Apps > Termux > Permissions > Microphone)"

    size = chunk_file.stat().st_size
    print(f"  📁 Recorded file: {size} bytes", flush=True)

    if size < 5000:
        return False, f"File too small ({size}B) — microphone captured no audio. Check Termux microphone permission."

    # Step 2: Convert to 16k mono
    print("  🔄 Converting to 16kHz mono WAV...", flush=True)
    result = subprocess.run([
        'ffmpeg', '-i', str(chunk_file),
        '-ar', '16000', '-ac', '1', '-acodec', 'pcm_s16le',
        str(wav_file), '-y'
    ], capture_output=True, timeout=10)

    if result.returncode != 0:
        err = result.stderr.decode('utf-8', errors='replace')[:200]
        return False, f"ffmpeg failed: {err}"

    if not wav_file.exists() or wav_file.stat().st_size < 1000:
        return False, "ffmpeg produced no audio output"

    # Step 3: Run WebRTC VAD
    print("  🧠 Running WebRTC VAD...", flush=True)
    vad = webrtcvad.Vad(mode=3)
    vad.set_mode(3)

    with wave.open(str(wav_file), 'rb') as wf:
        rate = wf.getframerate()
        n_frames = wf.getnframes()
        audio = wf.readframes(n_frames)

    if not audio:
        return False, "WAV has no audio data"

    frame_size = int(16000 * 30 / 1000) * 2  # 960 bytes
    n_speech = n_total = 0
    speech_rms_sum = silence_rms_sum = 0

    for i in range(0, len(audio) - frame_size, frame_size):
        chunk = audio[i:i+frame_size]
        n_total += 1
        num_samples = len(chunk) // 2
        try:
            is_sp = vad.is_speech(chunk, rate, num_samples)
        except Exception:
            is_sp = False
        n = len(chunk) // 2
        rms = (sum(struct.unpack_from('<h', chunk, j)[0] ** 2 for j in range(0, len(chunk) - 1, 2)) / n) ** 0.5
        if is_sp:
            n_speech += 1
            speech_rms_sum += rms
        else:
            silence_rms_sum += rms

    pct = n_speech / n_total * 100 if n_total > 0 else 0
    avg_speech = speech_rms_sum / n_speech if n_speech > 0 else 0
    avg_silence = silence_rms_sum / (n_total - n_speech) if n_total > n_speech else 0

    # Cleanup
    for f in (chunk_file, wav_file):
        if f.exists():
            os.remove(f)

    # Step 4: Report
    print(f"  📊 VAD results:")
    print(f"     Duration: {n_total * 30 / 1000:.1f}s ({n_total} frames)")
    print(f"     Speech frames: {n_speech}/{n_total} ({pct:.1f}%)")
    print(f"     Avg RMS (speech): {avg_speech:.0f}")
    print(f"     Avg RMS (silence): {avg_silence:.0f}")

    if pct < 5:
        return False, f"VAD={pct:.1f}% — microphone may be picking up silence. Check mic permission."
    elif pct < 20:
        return True, f"VAD OK ({pct:.1f}% speech detected). Low ratio — speak louder/closer to mic."
    else:
        return True, f"VAD working correctly ({pct:.1f}% speech detected)."

def _kill_existing_recording():
    """Kill any existing termux-microphone-record process so we can start fresh."""
    subprocess.run(['termux-microphone-record', '-q'],
                   capture_output=True, timeout=5)
    time.sleep(0.5)

def test_mic(duration=3.0, out_path='/data/data/com.termux/files/home/jarvis-voice/mic_test.m4a'):
    """Quick mic test — records `duration` seconds, checks file, plays it back."""
    out = Path(out_path)
    if out.exists():
        out.unlink()
    # Kill any hanging recording from a previous run
    _kill_existing_recording()

    print(f"🎤 Recording {duration}s... (speak clearly)")
    with _recording_lock:
        proc = subprocess.Popen(
            ['termux-microphone-record', '-f', str(out), '-l', str(int(duration))],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        proc.wait()  # wait for -l to auto-exit

    if not out.exists():
        print(f"❌ FAIL — file not created: {out}")
        print("   → Is termux-api installed? (pkg install termux-api)")
        print("   → Does Termux have microphone permission?")
        return False

    size = out.stat().st_size
    if size < 1000:
        print(f"❌ FAIL — file too small ({size} bytes), likely empty recording")
        return False

    print(f"✅ Mic working! File: {out} ({size} bytes)")

    # Play back the recording
    print(f"🔊 Playing back recording...")
    play_result = subprocess.run(
        ['ffplay', '-nodisp', '-autoexit', '-loglevel', 'quiet', str(out)],
        capture_output=True, timeout=int(duration + 5)
    )
    if play_result.returncode != 0:
        print(f"⚠️  Playback failed — audio saved at: {out}")
        print(f"   Try: ffplay {out}")

        print(f"   termux-media-player play {out}")
        print(f"   Audio saved at: {out}")

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


__version__ = "1.4.0"

if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] in ("--version", "-v"):
        print(f"jarvis-tui {__version__}")
        sys.exit(0)

    if len(sys.argv) > 1:
        if sys.argv[1] in ("--test-mic", "--mic-test", "-t"):
            # Legacy mic test
            import argparse
            parser = argparse.ArgumentParser(description=f"Jarvis Voice TUI v{__version__} — mic test")
            parser.add_argument("-d", "--duration", type=float, default=3.0)
            parser.add_argument("-f", "--file", default="/data/data/com.termux/files/home/jarvis-voice/mic_test.m4a")
            args, _ = parser.parse_known_args()
            ok = test_mic(duration=args.duration, out_path=args.file)
            sys.exit(0 if ok else 1)

        elif sys.argv[1] in ("--test-vad", "--vad-test"):
            # Full VAD pipeline test with microphone
            print(f"Jarvis Voice TUI v{__version__} — VAD pipeline test (microphone required)")
            print("=" * 50)
            print("This will record 3 seconds from your microphone and run")
            print("WebRTC VAD on the recording.")
            print()
            print("Press Enter to start recording (or Ctrl+C to cancel)...", end=" ", flush=True)
            input()
            ok, msg = test_mic_vad()
            print()
            if ok:
                print(f"✅ PASS: {msg}")
            else:
                print(f"❌ FAIL: {msg}")
            sys.exit(0 if ok else 1)

        elif sys.argv[1] in ("--test-vad-synthetic", "--vad-synthetic"):
            # VAD test on synthetic audio (no microphone needed)
            print(f"Jarvis Voice TUI v{__version__} — VAD synthetic test (no microphone)")
            print("=" * 50)
            print("Testing WebRTC VAD on generated 1kHz sine wave.")
            print("No microphone required.")
            print()
            ok, msg = test_vad_synthetic()
            print()
            if ok:
                print(f"✅ PASS: {msg}")
            else:
                print(f"❌ FAIL: {msg}")
            sys.exit(0 if ok else 1)

    JarvisTUI().run()