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
import shutil
import json
import re
import unicodedata
import traceback
from urllib import request as urlrequest
from urllib import error as urlerror
from urllib.parse import quote
from pathlib import Path

APP_DIR = Path(__file__).parent

# ── Config ─────────────────────────────────────────────────────────
GATEWAY_URL   = os.environ.get("JARVIS_GATEWAY_URL", "http://localhost:18789")
AUTH_TOKEN    = os.environ.get("JARVIS_AUTH_TOKEN", "")
SESSION_KEY   = os.environ.get("JARVIS_SESSION",    "agent:main:subagent:jarvis-tui")
OPENCLAW_SESSION_ID = os.environ.get("JARVIS_OPENCLAW_SESSION_ID", "jarvis-tui")
OPENCLAW_TIMEOUT = int(os.environ.get("JARVIS_OPENCLAW_TIMEOUT", "90"))
SESSION_DIR   = "/data/data/com.termux/files/home/.openclaw/agents/main/sessions"
MEMORY_DIR    = "/data/data/com.termux/files/home/.openclaw/memory"

# Audio settings  
SAMPLE_RATE   = 16000
FRAME_DURATION_MS = 30  # 30ms WebRTC standard
ENERGY_THRESHOLD  = 800
WAKE_WORD = os.environ.get("JARVIS_WAKE_WORD", "hey jarvis").strip().lower()
REQUIRE_WAKE_WORD = os.environ.get("JARVIS_REQUIRE_WAKE_WORD", "0").lower() not in ("0", "false", "no")
QUESTION_TIMEOUT = float(os.environ.get("JARVIS_QUESTION_TIMEOUT", "45"))
CONVERSATION_IDLE_TIMEOUT = float(os.environ.get("JARVIS_CONVERSATION_IDLE_TIMEOUT", "15"))
STT_CONTINUATION_TIMEOUT = float(os.environ.get("JARVIS_STT_CONTINUATION_TIMEOUT", "2.5"))
STT_MAX_PARTS = int(os.environ.get("JARVIS_STT_MAX_PARTS", "2"))
TTS_SETTLE_SECONDS = float(os.environ.get("JARVIS_TTS_SETTLE_SECONDS", "0.8"))
TTS_WORDS_PER_MINUTE = float(os.environ.get("JARVIS_TTS_WORDS_PER_MINUTE", "155"))
TTS_MAX_WAIT_SECONDS = float(os.environ.get("JARVIS_TTS_MAX_WAIT_SECONDS", "18"))

MEDIA_ENV = os.environ.copy()
MEDIA_ENV.pop("LD_LIBRARY_PATH", None)

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
    "idle":      ["🤖", "🙂", "🤖", "😴"],
    "wake":      ["👀", "🤖", "👂"],
    "listening": ["🎤", "👂", "🎙️"],
    "thinking":  ["🤔", "🧠", "💭"],
    "speaking":  ["🔊", "🗣️", "😊"],
    "happy":     ["😊", "😄", "🙂"],
    "done":      ["✅", "🙂"],
    "error":     ["❌", "😕"],
}

def log(msg):
    print(f"[jarvis-tui] {msg}", flush=True)

def log_exception(context: str, exc: BaseException):
    trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    log(f"{context}: {type(exc).__name__}: {exc}")
    log(trace)
    try:
        with open(APP_DIR / "jarvis_error.log", "a") as f:
            f.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] {context}\n")
            f.write(trace)
    except Exception:
        pass

def print_banner():
    print()
    print(f"  ╔══════════════════════════════════════╗  v{__version__}")
    print("  ║       🤖 JARVIS VOICE TUI 🤖         ║")
    print("  ╚══════════════════════════════════════╝")
    print()

class Screen:
    def __init__(self):
        self.lock = threading.Lock()
        self.state = "idle"
        self.status = "Starting..."
        self.question = ""
        self.answer = ""
        self.frame = 0
        self._started = False

    def update(self, state=None, status=None, question=None, answer=None):
        with self.lock:
            if state is not None:
                self.state = state
            if status is not None:
                self.status = status
            if question is not None:
                self.question = question
            if answer is not None:
                self.answer = answer
            self.draw_locked()

    def tick(self):
        with self.lock:
            self.frame += 1
            self.draw_locked()

    def draw_locked(self):
        faces = FACES.get(self.state, FACES["idle"])
        emoji = faces[self.frame % len(faces)]
        if not self._started:
            print("\033[2J\033[H", end="")
            self._started = True
        else:
            print("\033[H", end="")

        question = self.question or "(waiting)"
        answer = self.answer or "(waiting)"
        lines = [
            f"  JARVIS VOICE TUI  v{__version__}",
            "  " + "=" * 38,
            f"  {emoji}  {self.status[:34]:34}",
            "",
            f"  You:    {question[:64]}",
            f"  Jarvis: {answer[:64]}",
            "",
            f"  Wake phrase: \"{WAKE_WORD}\"",
            "  Press Ctrl+C to stop.",
            " " * 78,
        ]
        print("\n".join(lines), end="\n", flush=True)


SCREEN = Screen()


def render(face, status=""):
    SCREEN.update(state=face, status=status)

def speech_text(text: str) -> str:
    """Make rich assistant text safer for Android TTS without changing screen output."""
    text = re.sub(r"[*_`#>\[\]()]|https?://\S+", " ", text)
    cleaned = []
    for ch in text:
        category = unicodedata.category(ch)
        if category in ("So", "Sk") or ch in "\ufe0e\ufe0f":
            cleaned.append(" ")
        elif ch in "→←↑↓↖↗↘↙•":
            cleaned.append(" ")
        else:
            cleaned.append(ch)
    text = "".join(cleaned)
    text = re.sub(r"\s+", " ", text).strip()
    return text or "Done."

def speak(text: str):
    spoken = speech_text(text)
    try:
        subprocess.run(["termux-tts-speak", spoken], capture_output=True, timeout=30)
    except Exception as e:
        log(f"TTS error: {e}")
    return spoken

def wait_for_tts(text: str):
    words = max(1, len(text.split()))
    estimated = words / max(1.0, TTS_WORDS_PER_MINUTE) * 60.0
    delay = min(TTS_MAX_WAIT_SECONDS, max(TTS_SETTLE_SECONDS, estimated + TTS_SETTLE_SECONDS))
    log(f"TTS wait: {delay:.1f}s")
    time.sleep(delay)

def best_transcript(lines):
    """Pick the most complete Android STT partial."""
    if not lines:
        return ""
    return max(lines, key=lambda line: (len(line.split()), len(line)))

def listen(timeout: float = QUESTION_TIMEOUT) -> str:
    """Use termux-speech-to-text to capture and transcribe speech."""
    try:
        result = subprocess.run(
            ["termux-speech-to-text"],
            capture_output=True, text=True, timeout=timeout
        )
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if len(lines) > 1:
            log(f"STT partials: {' | '.join(lines[-3:])}")
        return best_transcript(lines)
    except subprocess.TimeoutExpired:
        return ""
    except Exception as e:
        log(f"STT error: {e}")
        return ""

def merge_transcripts(parts):
    merged = ""
    for part in (p.strip() for p in parts if p and p.strip()):
        if not merged:
            merged = part
            continue
        lower_merged = merged.lower()
        lower_part = part.lower()
        if lower_part in lower_merged:
            continue
        if lower_merged in lower_part:
            merged = part
            continue
        merged = f"{merged} {part}"
    return merged.strip()

def listen_long(timeout: float = QUESTION_TIMEOUT) -> str:
    """Capture a full user turn, including one short continuation if STT cut off early."""
    parts = []
    first = listen(timeout=timeout)
    if first:
        parts.append(first)

    for _ in range(max(0, STT_MAX_PARTS - 1)):
        if not parts:
            break
        extra = listen(timeout=STT_CONTINUATION_TIMEOUT)
        if not extra:
            break
        parts.append(extra)

    return merge_transcripts(parts)

def save_wav(path, audio_data, sample_rate=SAMPLE_RATE):
    with wave.open(str(path), 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(audio_data)

def wait_for_termux_recording(path: Path, timeout_s: float):
    """Wait for Termux:API's async microphone recorder to finish writing a file."""
    deadline = time.time() + timeout_s
    last_size = -1
    stable_checks = 0

    while time.time() < deadline:
        is_recording = False
        try:
            info = subprocess.run(
                ['termux-microphone-record', '-i'],
                capture_output=True, text=True, timeout=1
            )
            is_recording = '"isRecording": true' in info.stdout
        except Exception:
            pass

        size = path.stat().st_size if path.exists() else 0
        if size > 0 and not is_recording:
            if size == last_size:
                stable_checks += 1
            else:
                stable_checks = 0
            if stable_checks >= 1:
                return True

        last_size = size
        time.sleep(0.1)

    return path.exists() and path.stat().st_size > 0

# ── OpenClaw Gateway ────────────────────────────────────────────────
def _reply_from_openclaw_json(data: dict) -> str:
    result = data.get("result") if isinstance(data, dict) else None
    if not isinstance(result, dict):
        return ""

    meta = result.get("meta")
    if isinstance(meta, dict):
        text = meta.get("finalAssistantVisibleText") or meta.get("finalAssistantRawText")
        if isinstance(text, str) and text.strip():
            return text.strip()

    payloads = result.get("payloads")
    if isinstance(payloads, list):
        parts = []
        for payload in payloads:
            if isinstance(payload, dict) and isinstance(payload.get("text"), str):
                parts.append(payload["text"].strip())
        if parts:
            return "\n".join(part for part in parts if part).strip()
    return ""

def send_message_cli(text: str) -> str:
    if shutil.which("openclaw") is None:
        log("OpenClaw CLI not found")
        return ""

    try:
        result = subprocess.run(
            [
                "openclaw", "agent",
                "--session-id", OPENCLAW_SESSION_ID,
                "--message", text,
                "--json",
                "--timeout", str(OPENCLAW_TIMEOUT),
            ],
            capture_output=True, text=True, timeout=OPENCLAW_TIMEOUT + 15
        )
    except subprocess.TimeoutExpired:
        log(f"OpenClaw timed out after {OPENCLAW_TIMEOUT}s")
        return ""
    except Exception as e:
        log(f"OpenClaw CLI error: {e}")
        return ""

    if result.returncode != 0:
        err = (result.stderr or result.stdout).strip()
        log(f"OpenClaw CLI failed: {err[:240]}")
        return ""

    try:
        return _reply_from_openclaw_json(json.loads(result.stdout))
    except Exception as e:
        log(f"OpenClaw JSON parse error: {e}")
        return ""

def send_message(session_key: str, text: str) -> str:
    cli_reply = send_message_cli(text)
    if cli_reply:
        return cli_reply

    session = quote(session_key, safe="")
    url = f"{GATEWAY_URL}/sessions/{session}/messages"
    payload = json.dumps({
        "kind": "text",
        "content": text,
        "sender": {"id": "jarvis-tui", "label": "jarvis-tui"},
    }).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if AUTH_TOKEN:
        headers["Authorization"] = f"Bearer {AUTH_TOKEN}"

    req = urlrequest.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urlrequest.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8")
            if resp.status == 200:
                result = json.loads(body)
                return result.get("reply", "")
            log(f"Gateway error: HTTP {resp.status}: {body[:160]}")
    except urlerror.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        log(f"Gateway error: HTTP {e.code}: {body[:160]}")
    except Exception as e:
        log(f"Gateway error: {e}")
    return ""

def extract_question(text: str):
    """Return (is_wake, question) for a transcribed wake phrase."""
    cleaned = " ".join(text.lower().replace(",", " ").split())
    if not REQUIRE_WAKE_WORD:
        return True, text.strip()
    if WAKE_WORD not in cleaned:
        return False, ""
    idx = cleaned.find(WAKE_WORD)
    question = text[idx + len(WAKE_WORD):].strip(" ,.!?")
    return True, question

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

    def _read_pcm_frames(self, wav_file: Path):
        """Convert a recorded chunk to raw 16 kHz PCM and yield 30 ms frames."""
        result = subprocess.run([
            'ffmpeg', '-hide_banner', '-loglevel', 'error',
            '-i', str(wav_file),
            '-ar', str(self.sample_rate), '-ac', '1',
            '-f', 's16le', '-acodec', 'pcm_s16le', '-'
        ], capture_output=True, timeout=5, env=MEDIA_ENV)
        if result.returncode != 0:
            log(f"ffmpeg failed: {result.stderr.decode(errors='ignore').strip()}")
            return []

        pcm = result.stdout
        usable = len(pcm) - (len(pcm) % self.frame_size)
        return [pcm[i:i + self.frame_size] for i in range(0, usable, self.frame_size)]

    def _wait_for_recording(self, wav_file: Path, timeout_s: float = 3.0):
        """Wait until Termux:API finishes writing the async microphone recording."""
        return wait_for_termux_recording(wav_file, timeout_s)

    def vad_loop(self, on_wake, silence_threshold_frames=5):
        """
        Main loop: record small chunks → read PCM → VAD check.
        When speech ends (silence_threshold_frames consecutive silence),
        trigger on_wake callback with the speech audio captured so far.
        """
        work_dir = Path("/data/data/com.termux/files/home/jarvis-voice")
        wav_file = work_dir / "vad_chunk.wav"

        speech_frames = 0
        silence_frames = 0
        min_speech = 4
        min_silence = silence_threshold_frames
        is_speaking = False
        threshold = ENERGY_THRESHOLD  # local alias, avoids late binding issues in loops

        while self.running:
            # ── S1: Remove old files ────────────────────────────────
            for f in (wav_file,):
                if f.exists():
                    try:
                        os.remove(f)
                    except Exception:
                        pass

            # Use lock so test functions can record without race conditions
            with _recording_lock:
                # termux-microphone-record streams output to stdout as it records,
                # so we must NOT let subprocess.run() collect it (that causes
                # communicate() to hang indefinitely and trigger TimeoutExpired).
                # We Popen with devnull output FDs so the process runs freely;
                # a SIGTERM after timeout is the backup if it gets stuck.
                try:
                    proc = subprocess.Popen(
                        ['termux-microphone-record', '-f', str(wav_file), '-l', '1'],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        start_new_session=True
                    )
                    ret = proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.terminate()
                    proc.wait(timeout=1)
                    print('[vad] ⚠ termux-mic timed out — was terminated')
                    continue

                if ret != 0:
                    print(f'[vad] ⚠ termux-mic exited with code {ret}')
                    continue

                if not self._wait_for_recording(wav_file):
                    print('[vad] ⚠ recording did not finish writing')
                    continue

            # ── S4: Check WAV was created ──────────────────────
            if not wav_file.exists():
                print('[vad] ⚠ wav file not created — is mic working?')
                continue

            file_size = wav_file.stat().st_size
            if file_size < 2000:
                print(f'[vad] ⚠ wav file too small ({file_size}B) — is mic working?')
                continue

            frames = self._read_pcm_frames(wav_file)
            if not frames:
                print('[vad] skip: no audio data')
                continue

            for chunk_data in frames:
                is_speech = False

                if self.vad:
                    try:
                        is_speech = self.vad.is_speech(chunk_data, self.sample_rate)
                    except Exception:
                        is_speech = False

                rms = compute_rms(chunk_data)
                if rms > threshold:
                    is_speech = True

                if is_speech:
                    speech_frames += 1
                    silence_frames = 0
                    if not is_speaking and speech_frames >= min_speech:
                        is_speaking = True
                        render("wake", "Voice heard. Say the wake phrase...")
                elif is_speaking:
                    silence_frames += 1
                    if silence_frames >= min_silence:
                        if speech_frames >= min_speech:
                            log(f"Speech activity ended — frames={speech_frames}, RMS={rms:.0f}")
                            try:
                                on_wake()
                            except Exception as e:
                                log_exception("Wake handler error", e)
                        speech_frames = 0
                        silence_frames = 0
                        is_speaking = False
                else:
                    silence_frames = 0
                    speech_frames = 0

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
        self.pending_intent = None

    def answer_question(self, question: str) -> str:
        """Send a conversational turn to OpenClaw, with a small local fallback."""
        response = send_message(SESSION_KEY, question)
        if response:
            return response

        text = question.lower().strip()
        if self.pending_intent == "weather_location":
            self.pending_intent = None
            location = question.strip()
            if location.lower().startswith("in "):
                location = location[3:].strip()
            return f"I don't have live weather access right now, but I understood the location as {location}."

        response = get_response(question)
        if "weather" in text and "which location" in response.lower():
            self.pending_intent = "weather_location"
        else:
            self.pending_intent = None
        return response

    def on_wake(self):
        """Called when VAD detects end of speech utterance."""
        if self.state != "idle":
            return
        log("Wake detected")
        self.state = "listening"
        self.pending_intent = None
        question = ""

        if REQUIRE_WAKE_WORD:
            render("listening", f'Listening for "{WAKE_WORD}"...')
            text = listen_long()
            SCREEN.update(question=text or "")
            is_wake, question = extract_question(text)

            if not is_wake:
                if text:
                    log(f"Ignored speech without wake phrase: {text}")
                self.state = "idle"
                render("idle", f'Waiting for "{WAKE_WORD}"...')
                return

        if not question:
            spoken = speak("Yes?")
            wait_for_tts(spoken)
            render("listening", "Listening for your question...")
            question = listen_long()
            SCREEN.update(question=question or "")

        while question:
            log(f"You: {question}")
            self.state = "thinking"
            render("thinking", "Thinking...")
            response = self.answer_question(question)
            SCREEN.update(answer=response)
            log(f"Jarvis: {response}")
            self.state = "speaking"
            render("speaking", "Speaking...")
            spoken = speak(response)
            wait_for_tts(spoken)
            self.state = "listening"
            render("listening", "Listening for your reply...")
            question = listen_long(timeout=CONVERSATION_IDLE_TIMEOUT)
            SCREEN.update(question=question or "")

        if not question:
            log("(no speech detected)")
            render("idle", f'No reply for {CONVERSATION_IDLE_TIMEOUT:.0f}s. Waiting for "{WAKE_WORD}"...')
            time.sleep(1)

        self.state = "idle"
        render("idle", f'Waiting for "{WAKE_WORD}"...')

    def run(self):
        print_banner()

        # Verify termux-api
        missing = [cmd for cmd in ("termux-tts-speak", "termux-speech-to-text", "termux-microphone-record", "ffmpeg")
                   if shutil.which(cmd) is None]
        if missing:
            print("  ❌ termux-api not found! Run: pkg install termux-api")
            print(f"     Missing command(s): {', '.join(missing)}")
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
        render("idle", f'Waiting for "{WAKE_WORD}"...')

        try:
            while self.running:
                SCREEN.tick()
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
    wav_file = work_dir / "vad_test_chunk.wav"

    # Remove existing file (termux-mic refuses to overwrite)
    if wav_file.exists():
        try:
            wav_file.unlink()
        except Exception:
            pass

    # Step 1: Record 3 seconds directly to WAV
    print("  🎤 Recording 3 seconds from microphone...", flush=True)
    print("     (Speak clearly during these 3 seconds)", flush=True)

    with _recording_lock:
        record_proc = subprocess.Popen(
            f'termux-microphone-record -f {wav_file} -l 3',
            shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        ret = record_proc.wait()
        if ret == 0:
            wait_for_termux_recording(wav_file, 5.0)

    if not wav_file.exists():
        return False, "Microphone file not created — is Termux microphone permission granted?"

    size = wav_file.stat().st_size
    print(f"  📁 Recorded file: {size} bytes", flush=True)

    if size < 2000:
        return False, f"File too small ({size}B) — microphone captured no audio. Check Termux microphone permission."

    # Step 2: Run WebRTC VAD
    print("  🧠 Running WebRTC VAD...", flush=True)
    vad = webrtcvad.Vad(mode=3)
    vad.set_mode(3)

    convert = subprocess.run([
        'ffmpeg', '-hide_banner', '-loglevel', 'error',
        '-i', str(wav_file),
        '-ar', '16000', '-ac', '1',
        '-f', 's16le', '-acodec', 'pcm_s16le', '-'
    ], capture_output=True, timeout=10, env=MEDIA_ENV)
    if convert.returncode != 0:
        err = convert.stderr.decode(errors='ignore').strip()
        return False, f"ffmpeg could not decode microphone file: {err}"

    rate = 16000
    audio = convert.stdout

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
    for f in (wav_file,):
        if f.exists():
            try:
                os.remove(f)
            except Exception:
                pass

    # Step 3: Report
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

def test_mic(duration=3.0, out_path='/data/data/com.termux/files/home/jarvis-voice/mic_test.wav'):
    """Quick mic test — records `duration` seconds, checks file, plays it back."""
    wav_file = Path(out_path)
    # Remove existing file (termux-mic refuses to overwrite)
    if wav_file.exists():
        try:
            wav_file.unlink()
        except Exception:
            pass

    print(f"🎤 Recording {duration}s... (speak clearly)")

    with _recording_lock:
        proc = subprocess.Popen(
            f'termux-microphone-record -f {wav_file} -l {int(duration)}',
            shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        ret = proc.wait()
        if ret == 0:
            wait_for_termux_recording(wav_file, duration + 2.0)

    if not wav_file.exists():
        print(f"❌ FAIL — file not created: {wav_file}")
        return False

    size = wav_file.stat().st_size
    if size < 2000:
        print(f"❌ FAIL — file too small ({size} bytes), likely empty recording")
        return False

    print(f"✅ Mic working! File: {wav_file} ({size} bytes)")

    # Play back the recording
    print(f"🔊 Playing back recording...")
    play_result = subprocess.run(
        ['ffplay', '-loglevel', 'quiet', '-nodisp', '-autoexit', str(wav_file)],
        capture_output=True, timeout=int(duration + 5), env=MEDIA_ENV
    )
    if play_result.returncode != 0:
        print(f"⚠️  Playback failed — audio saved at: {wav_file}")
        print(f"   Try: ffplay {wav_file}")
    else:
        print(f"   ✅ Playback complete")

    # Quick ffprobe check
    probe = subprocess.run(
        ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_format', str(wav_file)],
        capture_output=True, text=True, timeout=10, env=MEDIA_ENV
    )
    if probe.returncode == 0:
        import json
        info = json.loads(probe.stdout)
        dur = info.get('format', {}).get('duration', '?')
        print(f"   Duration: {float(dur):.1f}s")
    return True


__version__ = "1.7.0"

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
            parser.add_argument("-f", "--file", default="/data/data/com.termux/files/home/jarvis-voice/mic_test.wav")
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
