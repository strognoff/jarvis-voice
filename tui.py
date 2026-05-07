#!/usr/bin/env python3
"""
tui.py — Jarvis Voice TUI for OpenClaw

Always-on voice assistant:
- Uses VAD (termux-microphone-record) for wake detection
- On wake: speaks "Yes?", listens for question, answers, loops until silence
- Transcribes speech with whisper-cli (local, no API key needed)
- Sends to OpenClaw sub-session → speaks back via termux-tts-speak
- Animated TUI display

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

# Audio settings
SAMPLE_RATE   = 16000
FRAME_DURATION_MS = 30
ENERGY_THRESHOLD  = 800
WAKE_WORD = os.environ.get("JARVIS_WAKE_WORD", "hello").strip().lower()
REQUIRE_WAKE_WORD = os.environ.get("JARVIS_REQUIRE_WAKE_WORD", "0").lower() not in ("0", "false", "no")
WAKE_WORD_TIMEOUT = float(os.environ.get("JARVIS_WAKE_WORD_TIMEOUT", "5.0"))
QUESTION_TIMEOUT = float(os.environ.get("JARVIS_QUESTION_TIMEOUT", "45"))
CONVERSATION_IDLE_TIMEOUT = float(os.environ.get("JARVIS_CONVERSATION_IDLE_TIMEOUT", "15"))

TTS_SETTLE_SECONDS = float(os.environ.get("JARVIS_TTS_SETTLE_SECONDS", "0.8"))
TTS_WORDS_PER_MINUTE = float(os.environ.get("JARVIS_TTS_WORDS_PER_MINUTE", "155"))
TTS_MAX_WAIT_SECONDS = float(os.environ.get("JARVIS_TTS_MAX_WAIT_SECONDS", "18"))

MEDIA_ENV = os.environ.copy()
MEDIA_ENV.pop("LD_LIBRARY_PATH", None)

# ── WebRTC VAD (optional) ───────────────────────────────────────────
try:
    import webrtcvad
    HAS_VAD = True
except ImportError:
    HAS_VAD = False

# ── Helpers ─────────────────────────────────────────────────────────
def compute_rms(audio_bytes: bytes) -> float:
    n = len(audio_bytes) // 2
    if n == 0:
        return 0.0
    total = 0
    for i in range(0, len(audio_bytes) - 1, 2):
        s = struct.unpack_from('<h', audio_bytes, i)[0]
        total += s * s
    return (total / n) ** 0.5

# ── TUI ─────────────────────────────────────────────────────────────

_STATE_META = {
    "idle":      ("😴", "Waiting..."),
    "busy":      ("🤖", ""),
    "wake":      ("👀", "Wake detected!"),
    "listening": ("🎤", "Listening"),
    "more":      ("🎤", "Listening for more"),
    "thinking":  ("🧠", "Thinking"),
    "speaking":  ("🔊", "Speaking"),
    "error":     ("❌", ""),
}

_DOTS = ["● ○ ○", "● ● ○", "● ● ●", "○ ○ ○"]
_SPIN = list("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")

_LOG_BUF: list[str] = []
_LOG_MAX = 50

def log(msg: str):
    entry = f"[{time.strftime('%H:%M:%S')}] {msg}"
    _LOG_BUF.append(entry)
    if len(_LOG_BUF) > _LOG_MAX:
        _LOG_BUF.pop(0)

def log_exception(context: str, exc: BaseException):
    trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    log(f"{context}: {type(exc).__name__}: {exc}")
    try:
        with open(APP_DIR / "jarvis_error.log", "a") as f:
            f.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] {context}\n")
            f.write(trace)
    except Exception:
        pass

def print_banner():
    pass

def _wrap(text: str, width: int, max_lines: int = 2) -> list[str]:
    words = text.split()
    lines, current = [], ""
    for word in words:
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= width:
            current += " " + word
        else:
            lines.append(current)
            if len(lines) >= max_lines:
                lines[-1] = lines[-1][: width - 1] + "…"
                return lines
            current = word
    if current:
        lines.append(current)
    return lines or [""]

class Screen:
    def __init__(self):
        self.lock = threading.Lock()
        self.state = "idle"
        self.status = ""
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

    def _inner_width(self) -> int:
        cols = shutil.get_terminal_size((50, 24)).columns
        return min(max(cols - 4, 38), 76)

    def _hline(self, left, fill, right, w):
        return left + fill * (w + 2) + right

    def _row(self, content, w):
        return "║ " + content[:w].ljust(w) + " ║"

    def _blank(self, w):
        return self._row("", w)

    def _status_line(self, w):
        state = self.state
        emoji, label = _STATE_META.get(state, ("🤖", ""))
        text = self.status if self.status else label
        if state in ("listening", "more"):
            dots = _DOTS[self.frame % len(_DOTS)]
            s = f"{emoji}  {text}  {dots}"
        elif state == "thinking":
            spin = _SPIN[self.frame % len(_SPIN)]
            s = f"{emoji}  {text}  {spin}"
        else:
            s = f"{emoji}  {text}" if text else emoji
        return self._row(s, w)

    def draw_locked(self):
        w = self._inner_width()
        top = self._hline("╔", "═", "╗", w)
        mid = self._hline("╠", "═", "╣", w)
        bot = self._hline("╚", "═", "╝", w)

        title_row = self._row(f"  JARVIS  v{__version__}", w)
        status_row = self._status_line(w)

        q_lines = _wrap(self.question, w - 2) if self.question else ["—"]
        a_lines = _wrap(self.answer, w - 2) if self.answer else ["—"]

        footer_row = self._row("  speak to wake  ·  Ctrl+C to quit", w)

        out = [
            top,
            title_row,
            mid,
            self._blank(w),
            status_row,
            self._blank(w),
            mid,
            self._row("  You", w),
            *[self._row("  " + l, w) for l in q_lines],
            mid,
            self._row("  Jarvis", w),
            *[self._row("  " + l, w) for l in a_lines],
            mid,
            footer_row,
            bot,
            "",
        ]

        if not self._started:
            sys.stdout.write("\033[2J\033[H\033[?25l")
            self._started = True
        else:
            sys.stdout.write("\033[H")

        sys.stdout.write("\n".join(out))
        sys.stdout.flush()


SCREEN = Screen()


def render(face: str, status: str = ""):
    SCREEN.update(state=face, status=status)

# ── Text helpers ────────────────────────────────────────────────────

def speech_text(text: str) -> str:
    """Strip markdown/emoji for clean TTS output."""
    text = re.sub(r"[*_`#>\[\]()]|https?://\S+", " ", text)
    cleaned = []
    for ch in text:
        cat = unicodedata.category(ch)
        if cat in ("So", "Sk") or ch in "\ufe0e\ufe0f":
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
        subprocess.run(
            ["termux-tts-speak", spoken],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except Exception as e:
        log(f"TTS error: {e}")
    return spoken

def wait_for_tts(text: str):
    words = max(1, len(text.split()))
    estimated = words / max(1.0, TTS_WORDS_PER_MINUTE) * 60.0
    delay = min(TTS_MAX_WAIT_SECONDS, max(TTS_SETTLE_SECONDS, estimated + TTS_SETTLE_SECONDS))
    time.sleep(delay)

# ── STT ─────────────────────────────────────────────────────────────

# whisper-cli model — search common Termux locations
_WHISPER_MODEL_SEARCH = [
    "/data/data/com.termux/files/home/whisper.cpp/models/ggml-base.en.bin",
    os.path.expanduser("~/whisper.cpp/models/ggml-base.en.bin"),
    "/data/data/com.termux/files/home/whisper.cpp/models/ggml-medium.en.bin",
    os.path.expanduser("~/whisper.cpp/models/ggml-medium.en.bin"),
    os.path.expanduser("~/models/ggml-base.en.bin"),
    os.path.expanduser("~/models/ggml-small.en.bin"),
    os.path.expanduser("~/models/ggml-tiny.en.bin"),
    "/data/data/com.termux/files/home/models/ggml-base.en.bin",
    "/data/data/com.termux/files/home/models/ggml-tiny.en.bin",
    os.environ.get("WHISPER_MODEL", ""),
]

def _find_whisper_model() -> str:
    for p in _WHISPER_MODEL_SEARCH:
        if p and Path(p).exists():
            return p
    return ""

def _transcribe_wav(wav_path: str) -> str:
    """Transcribe a WAV file using whisper-cli."""
    model = _find_whisper_model()
    if not model:
        log("No whisper model found — set WHISPER_MODEL env var or download to ~/models/")
        SCREEN.update(status="⚠ No whisper model — see README")
        return _termux_stt_fallback()

    try:
        result = subprocess.run(
            [
                "whisper-cli",
                "-m", model,
                "-f", wav_path,
                "-l", "en",
                "--no-prints",          # suppress progress output
                "--no-timestamps",      # clean text output only
            ],
            capture_output=True, text=True, timeout=30,
        )
        # whisper-cli prints transcript to stdout, one line per segment
        lines = [l.strip() for l in result.stdout.splitlines() if l.strip()]
        text = " ".join(lines).strip()
        # Filter Whisper hallucinations on silence
        if text.lower() in ("", "you", "thank you.", "thanks.", ".", "...", "[blank_audio]"):
            return ""
        return text
    except subprocess.TimeoutExpired:
        log("whisper-cli timed out")
        return ""
    except Exception as e:
        log(f"whisper-cli error: {e}")
        return ""

def _termux_stt_fallback() -> str:
    """Last-resort fallback to termux-speech-to-text."""
    try:
        result = subprocess.run(
            ["termux-speech-to-text"],
            capture_output=True, text=True, timeout=15,
        )
        return result.stdout.strip()
    except Exception:
        return ""

def _record_wav(wav_path: str, duration: float) -> bool:
    """Record audio for `duration` seconds and convert to 16kHz mono PCM.

    termux-microphone-record produces a WAV with a varying codec/sample-rate
    depending on the device. whisper-cli requires 16kHz mono 16-bit PCM.
    We record to a raw file then convert in-place with ffmpeg.
    Returns True if the final file has usable content.
    """
    raw_path = wav_path + ".raw.wav"
    for p in (wav_path, raw_path):
        try:
            Path(p).unlink(missing_ok=True)
        except Exception:
            pass
    try:
        proc = subprocess.Popen(
            ["termux-microphone-record", "-f", raw_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        time.sleep(duration)
        subprocess.run(
            ["termux-microphone-record", "-q"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=3,
        )
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.terminate()
    except Exception as e:
        log(f"record error: {e}")
        return False

    if not Path(raw_path).exists() or Path(raw_path).stat().st_size < 512:
        return False

    # Convert to 16kHz mono 16-bit PCM — required by whisper-cli
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", raw_path,
             "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", wav_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=15,
        )
        Path(raw_path).unlink(missing_ok=True)
        return r.returncode == 0 and Path(wav_path).stat().st_size > 512
    except Exception as e:
        log(f"ffmpeg convert error: {e}")
        return False


def listen(timeout: float = QUESTION_TIMEOUT) -> str:
    """Record a single continuous audio clip then transcribe with whisper-cli.

    Records for `duration` seconds in one uninterrupted recording — identical
    to what --test-mic does. Fragmented chunk-based recording loses audio in
    the gaps between process restarts. Whisper's built-in VAD handles silence.
    """
    import tempfile

    wav = str(Path(tempfile.gettempdir()) / "jarvis_listen.wav")

    # Cap at 8s for a natural utterance — enough for any question.
    # For conversation follow-ups timeout=CONVERSATION_IDLE_TIMEOUT (15s),
    # but we still cap at 8s since nobody speaks for longer than that.
    duration = min(timeout, 8.0)

    SCREEN.update(status=f"Listening... ({duration:.0f}s)")
    ok = _record_wav(wav, duration)
    if not ok:
        return ""

    SCREEN.update(status="Transcribing your speech...")
    return _transcribe_wav(wav)

def listen_long(timeout: float = QUESTION_TIMEOUT) -> str:
    return listen(timeout)

# Stub for compatibility — no longer used but kept so nothing breaks
def start_stt_process():
    return None

def collect_stt_process(proc, timeout: float) -> str:
    return listen(timeout)

# ── Wake word ────────────────────────────────────────────────────────

def extract_question(text: str):
    """Return (is_wake, question_after_wake_word)."""
    cleaned = " ".join(text.lower().replace(",", " ").split())
    if not REQUIRE_WAKE_WORD:
        return True, text.strip()
    if WAKE_WORD not in cleaned:
        return False, ""
    idx = cleaned.find(WAKE_WORD)
    question = text[idx + len(WAKE_WORD):].strip(" ,.!?")
    return True, question

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
        return ""
    try:
        result = subprocess.run(
            ["openclaw", "agent",
             "--session-id", OPENCLAW_SESSION_ID,
             "--message", text,
             "--json",
             "--timeout", str(OPENCLAW_TIMEOUT)],
            capture_output=True, text=True, timeout=OPENCLAW_TIMEOUT + 15
        )
    except subprocess.TimeoutExpired:
        log(f"OpenClaw timed out after {OPENCLAW_TIMEOUT}s")
        return ""
    except Exception as e:
        log(f"OpenClaw CLI error: {e}")
        return ""
    if result.returncode != 0:
        log(f"OpenClaw CLI failed: {(result.stderr or result.stdout).strip()[:240]}")
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
                return json.loads(body).get("reply", "")
            log(f"Gateway error: HTTP {resp.status}: {body[:160]}")
    except urlerror.HTTPError as e:
        log(f"Gateway error: HTTP {e.code}: {e.read().decode('utf-8', errors='ignore')[:160]}")
    except Exception as e:
        log(f"Gateway error: {e}")
    return ""

# ── Local response fallback ──────────────────────────────────────────

def get_response(text: str) -> str:
    t = text.lower().strip()
    if any(g in t for g in ["hello", "hi", "hey"]):
        return "Hello! I'm Jarvis. What can I do for you today?"
    elif "how are you" in t:
        return "I'm doing great, thank you for asking!"
    elif any(g in t for g in ["your name", "who are you"]):
        return "I'm Jarvis, your personal AI assistant."
    elif "time" in t:
        from datetime import datetime
        return f"It's {datetime.now().strftime('%I:%M %p')}."
    elif "date" in t:
        from datetime import datetime
        return f"Today is {datetime.now().strftime('%A, %B %d')}."
    elif "weather" in t:
        return "I can check the weather for you. Which location?"
    elif "joke" in t:
        return "Why did the AI cross the road? To get to the other side of the neural network!"
    elif "thank" in t:
        return "You're welcome!"
    elif any(g in t for g in ["bye", "goodbye"]):
        return "Goodbye! I'll be here when you need me."
    else:
        return "I heard you, but I'm not sure how to answer that right now."

# ── VAD Capture Loop ────────────────────────────────────────────────
class VADLoop:
    """
    Continuously records audio in 1s chunks using termux-microphone-record,
    runs WebRTC VAD on each chunk, and fires a callback when speech is detected
    followed by silence. This is the wake trigger — no gaps in listening.
    """

    def __init__(self, sample_rate=SAMPLE_RATE):
        self.sample_rate = sample_rate
        self.frame_size = int(sample_rate * FRAME_DURATION_MS / 1000) * 2
        self.running = False
        self.vad = None
        if HAS_VAD:
            self.vad = webrtcvad.Vad(mode=3)
        else:
            SCREEN.update(status="⚠ No WebRTC VAD — energy fallback")

    def _record_chunk(self, wav_path: str, duration_s: float):
        """Record audio for duration_s seconds, then kill the process."""
        try:
            proc = subprocess.Popen(
                ['termux-microphone-record', '-f', wav_path],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True
            )
            time.sleep(duration_s)
            # Send stop command, then kill the process
            subprocess.run(
                ['termux-microphone-record', '-q'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=2
            )
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.terminate()
        except Exception as e:
            log(f"[vad] record error: {e}")

    def _read_pcm_frames(self, wav_file: Path):
        """Convert WAV to raw 16kHz PCM frames via ffmpeg."""
        try:
            result = subprocess.run([
                'ffmpeg', '-hide_banner', '-loglevel', 'error',
                '-i', str(wav_file),
                '-ar', str(self.sample_rate), '-ac', '1',
                '-f', 's16le', '-acodec', 'pcm_s16le', '-'
            ], capture_output=True, timeout=5, env=MEDIA_ENV)
            if result.returncode != 0:
                return []
            pcm = result.stdout
            usable = len(pcm) - (len(pcm) % self.frame_size)
            return [pcm[i:i + self.frame_size] for i in range(0, usable, self.frame_size)]
        except Exception:
            return []

    def vad_loop(self, on_wake, silence_threshold_frames=5):
        import tempfile as _tempfile
        wav_file = Path(_tempfile.gettempdir()) / "jarvis_vad_chunk.wav"

        speech_frames = 0
        silence_frames = 0
        min_speech = 3
        min_silence = silence_threshold_frames
        is_speaking = False

        while self.running:
            # Remove old file
            if wav_file.exists():
                try:
                    wav_file.unlink()
                except Exception:
                    pass

            # Record 1 second chunk
            self._record_chunk(str(wav_file), 1.0)

            if not wav_file.exists() or wav_file.stat().st_size < 512:
                continue

            frames = self._read_pcm_frames(wav_file)
            if not frames:
                continue

            for chunk_data in frames:
                is_speech = False
                if self.vad:
                    try:
                        is_speech = self.vad.is_speech(chunk_data, self.sample_rate)
                    except Exception:
                        pass
                if compute_rms(chunk_data) > ENERGY_THRESHOLD:
                    is_speech = True

                if is_speech:
                    speech_frames += 1
                    silence_frames = 0
                    if not is_speaking and speech_frames >= min_speech:
                        is_speaking = True
                        render("wake", "Voice detected...")
                elif is_speaking:
                    silence_frames += 1
                    if silence_frames >= min_silence:
                        if speech_frames >= min_speech:
                            try:
                                on_wake()
                            except Exception as e:
                                log_exception("Wake handler error", e)
                        speech_frames = 0
                        silence_frames = 0
                        is_speaking = False
                else:
                    speech_frames = 0
                    silence_frames = 0

    def start(self):
        self.running = True

    def stop(self):
        self.running = False


# ── Main Jarvis TUI ──────────────────────────────────────────────────

class JarvisTUI:
    def __init__(self):
        self.state = "idle"
        self.running = False
        self.pending_intent = None
        self.vad = None

    def _pause_vad(self):
        """Stop VAD and release mic before STT takes over."""
        if self.vad:
            self.vad.stop()
            # Stop any in-flight recording and give it time to release the mic.
            # VAD records 1s chunks — worst case we need to wait for the current
            # chunk to finish + ffmpeg convert + mic release before we can record.
            subprocess.run(
                ['termux-microphone-record', '-q'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=3
            )
            time.sleep(1.5)

    def _resume_vad(self):
        """Restart VAD in a fresh thread after conversation ends."""
        if self.vad:
            self.vad.start()
            threading.Thread(
                target=self.vad.vad_loop, args=(self.on_wake,), daemon=True
            ).start()

    def on_wake(self):
        """Called by VADLoop when speech+silence detected.
        Skips STT wake word check — VAD energy is the trigger."""
        if self.state != "idle":
            return
        self.state = "busy"
        self._pause_vad()
        try:
            render("wake", "Waking up...")
            self._do_conversation()
        except Exception as e:
            log_exception("on_wake error", e)
        finally:
            self.state = "idle"
            render("idle", "Speak to wake me up")
            self._resume_vad()

    def answer_question(self, question: str) -> str:
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

    def _do_conversation(self, question: str = ""):
        """Run a full conversation: beep → listen → think → speak → loop."""
        self.state = "busy"
        self.pending_intent = None

        try:
            if not question:
                # Say "Yes?" in background so mic opens immediately after
                render("listening", "Ask your question — I'm recording for 8s")
                threading.Thread(
                    target=lambda: subprocess.run(
                        ["termux-tts-speak", "Yes?"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        timeout=5,
                    ),
                    daemon=True,
                ).start()
                time.sleep(0.3)
                question = listen(timeout=QUESTION_TIMEOUT)
                SCREEN.update(question=question or "")

            while question:
                SCREEN.update(question=question)
                render("thinking", "Working on it...")
                response = self.answer_question(question)
                SCREEN.update(answer=response)
                render("speaking", "Answering...")
                speak(response)
                render("listening", "Your turn — speak now or stay silent to end")
                question = listen(timeout=CONVERSATION_IDLE_TIMEOUT)
                SCREEN.update(question=question or "")

            # Silence timeout — end conversation
            render("speaking", "Ending conversation...")
            speak("Goodbye!")

        except Exception as e:
            log_exception("conversation error", e)
        finally:
            self.state = "idle"
            render("idle", "Speak to wake me up")

    def run(self):
        missing = [cmd for cmd in ("termux-tts-speak", "whisper-cli",
                                   "termux-microphone-record")
                   if shutil.which(cmd) is None]
        if missing:
            sys.stdout.write("\033[?25h")
            sys.stdout.flush()
            print("❌  Missing required commands:")
            for cmd in missing:
                print(f"    • {cmd}")
            print("    Run: pkg install termux-api whisper.cpp")
            sys.exit(1)

        log(f"Gateway: {GATEWAY_URL}  Session: {SESSION_KEY}")
        model = _find_whisper_model()
        if model:
            log(f"Whisper model: {Path(model).name}")
        else:
            log("⚠ No whisper model found — set WHISPER_MODEL=/path/to/ggml-*.bin")

        self.running = True
        self.vad = VADLoop()

        # Animation ticker
        def _ticker():
            while self.running:
                SCREEN.tick()
                time.sleep(0.25)
        threading.Thread(target=_ticker, daemon=True).start()

        # Start VAD loop
        self.vad.start()
        vad_thread = threading.Thread(
            target=self.vad.vad_loop, args=(self.on_wake,), daemon=True
        )
        vad_thread.start()

        render("idle", "Speak to wake me up")

        try:
            while self.running:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            self.vad.stop()
            self.running = False
            sys.stdout.write("\033[?25h\n")
            sys.stdout.flush()


# ── Test helpers ────────────────────────────────────────────────────

def save_wav(path, audio_data, sample_rate=SAMPLE_RATE):
    with wave.open(str(path), 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(audio_data)

def wait_for_termux_recording(path: Path, timeout_s: float = 5.0):
    deadline = time.time() + timeout_s
    last_size = -1
    while time.time() < deadline:
        size = path.stat().st_size if path.exists() else 0
        if size > 0 and size == last_size:
            return True
        last_size = size
        time.sleep(0.15)
    return path.exists() and path.stat().st_size > 0

def test_mic(duration=5.0, out_path=None):
    """Full pipeline test: record → ffmpeg convert → whisper-cli transcribe."""
    import tempfile, textwrap
    if out_path is None:
        out_path = str(Path(tempfile.gettempdir()) / "jarvis_mic_test.wav")

    model = _find_whisper_model()

    print()
    print("  ┌─────────────────────────────────────┐")
    print("  │  Jarvis STT Pipeline Test           │")
    print("  └─────────────────────────────────────┘")
    print(f"  Model : {Path(model).name if model else '⚠ not found'}")
    print(f"  Duration: {duration}s")
    print()
    print(f"  🎤  Recording {duration}s — speak now...")
    print()

    ok = _record_wav(out_path, duration)
    if not ok:
        print("  ❌  Recording failed — check Termux microphone permission")
        print("      Settings → Apps → Termux → Permissions → Microphone → Allow")
        return False

    size = Path(out_path).stat().st_size
    print(f"  ✓   Recorded and converted  ({size} bytes, 16kHz PCM)")

    if not model:
        print("  ⚠️   No whisper model — recording works but STT is not configured")
        print("      Set WHISPER_MODEL=/path/to/ggml-base.en.bin")
        return False

    print(f"  🧠  Transcribing with {Path(model).name}...")
    print()
    text = _transcribe_wav(out_path)

    w = 37
    print(f"  ┌{'─' * w}┐")
    if text:
        for line in textwrap.wrap(text, w - 2):
            print(f"  │  {line:<{w - 2}}  │")
    else:
        print(f"  │  {'(nothing heard)':<{w - 2}}  │")
    print(f"  └{'─' * w}┘")
    print()

    try:
        Path(out_path).unlink()
    except Exception:
        pass

    if text:
        print("  ✅  Full pipeline working!\n")
        return True
    else:
        print("  ⚠️   Nothing transcribed — try speaking louder/closer\n")
        return False

def test_mic_vad():
    import tempfile, webrtcvad, wave, struct, os, subprocess
    wav_file = Path(tempfile.gettempdir()) / "jarvis_vad_test_chunk.wav"
    if wav_file.exists():
        try:
            wav_file.unlink()
        except Exception:
            pass

    print("  🎤 Recording 3 seconds from microphone...", flush=True)
    print("     (Speak clearly during these 3 seconds)", flush=True)

    with _recording_lock:
        record_proc = subprocess.Popen(
            ['termux-microphone-record', '-f', str(wav_file)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True
        )
        time.sleep(3.0)
        subprocess.run(
            ['termux-microphone-record', '-q'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=3
        )
        try:
            record_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            record_proc.terminate()
        wait_for_termux_recording(wav_file, 5.0)

    if not wav_file.exists():
        return False, "Microphone file not created — is Termux microphone permission granted?"
    size = wav_file.stat().st_size
    if size < 2000:
        return False, f"File too small ({size}B) — check Termux microphone permission."

    print("  🧠 Running WebRTC VAD...", flush=True)
    vad = webrtcvad.Vad(mode=3)

    convert = subprocess.run([
        'ffmpeg', '-hide_banner', '-loglevel', 'error',
        '-i', str(wav_file),
        '-ar', '16000', '-ac', '1',
        '-f', 's16le', '-acodec', 'pcm_s16le', '-'
    ], capture_output=True, timeout=10, env=MEDIA_ENV)
    if convert.returncode != 0:
        return False, f"ffmpeg failed: {convert.stderr.decode(errors='ignore').strip()}"

    audio = convert.stdout
    if not audio:
        return False, "WAV has no audio data"

    frame_size = int(16000 * 30 / 1000) * 2
    n_speech = n_total = 0
    speech_rms_sum = silence_rms_sum = 0
    for i in range(0, len(audio) - frame_size, frame_size):
        chunk = audio[i:i+frame_size]
        n_total += 1
        try:
            is_sp = vad.is_speech(chunk, 16000)
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

    for f in (wav_file,):
        if f.exists():
            try:
                os.remove(f)
            except Exception:
                pass

    print(f"  📊 VAD results:")
    print(f"     Duration: {n_total * 30 / 1000:.1f}s ({n_total} frames)")
    print(f"     Speech frames: {n_speech}/{n_total} ({pct:.1f}%)")
    print(f"     Avg RMS (speech): {avg_speech:.0f}")
    print(f"     Avg RMS (silence): {avg_silence:.0f}")

    if pct < 5:
        return False, f"VAD={pct:.1f}% — microphone may be silent. Check permission."
    elif pct < 20:
        return True, f"VAD OK ({pct:.1f}% speech). Low — speak louder/closer to mic."
    else:
        return True, f"VAD working correctly ({pct:.1f}% speech detected)."

def test_vad_synthetic():
    import webrtcvad, wave, struct, math, os, tempfile
    wav_file = Path(tempfile.gettempdir()) / "jarvis_vad_synthetic_test.wav"
    print("  🎙️  Testing WebRTC VAD on synthetic audio (no microphone)", flush=True)
    SAMPLE_RATE = 16000
    vad = webrtcvad.Vad(mode=3)
    audio = b""
    for i in range(SAMPLE_RATE * 3):
        t = i / SAMPLE_RATE
        if 1.0 <= t < 2.0:
            sample = int(0.8 * 16000 * math.sin(2 * math.pi * 1000 * t))
        else:
            sample = 0
        audio += struct.pack('<h', sample)
    with wave.open(str(wav_file), 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio)
    frame_size = int(SAMPLE_RATE * 30 / 1000) * 2
    n_speech = n_total = 0
    for i in range(0, len(audio) - frame_size, frame_size):
        chunk = audio[i:i+frame_size]
        n_total += 1
        try:
            is_sp = vad.is_speech(chunk, SAMPLE_RATE)
        except Exception:
            is_sp = False
        if is_sp:
            n_speech += 1
    pct = n_speech / n_total * 100 if n_total > 0 else 0
    os.remove(wav_file)
    print(f"  📊 VAD on synthetic 1kHz tone: {n_speech}/{n_total} frames ({pct:.1f}%)", flush=True)
    if pct < 15:
        return False, f"VAD detected only {pct:.1f}% — expected ~33%. VAD may be broken."
    elif pct > 70:
        return False, f"VAD detected {pct:.1f}% — too sensitive."
    else:
        return True, f"VAD working correctly ({pct:.1f}% detected, expected ~33%)"

def _kill_existing_recording():
    subprocess.run(['termux-microphone-record', '-q'],
                   capture_output=True, timeout=5)
    time.sleep(0.5)


__version__ = "1.7.0"

if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] in ("--version", "-v"):
        print(f"jarvis-tui {__version__}")
        sys.exit(0)

    if len(sys.argv) > 1:
        if sys.argv[1] in ("--test-mic", "--mic-test", "-t"):
            import argparse
            parser = argparse.ArgumentParser(description=f"Jarvis Voice TUI v{__version__} — mic + whisper-cli transcription test")
            parser.add_argument("-d", "--duration", type=float, default=5.0)
            parser.add_argument("-f", "--file", default=None)
            args, _ = parser.parse_known_args()
            ok = test_mic(duration=args.duration, out_path=args.file)
            sys.exit(0 if ok else 1)

        elif sys.argv[1] in ("--test-vad", "--vad-test"):
            print(f"Jarvis Voice TUI v{__version__} — VAD pipeline test")
            print("=" * 50)
            print("Press Enter to start recording (or Ctrl+C to cancel)...", end=" ", flush=True)
            input()
            ok, msg = test_mic_vad()
            print()
            print(f"{'✅ PASS' if ok else '❌ FAIL'}: {msg}")
            sys.exit(0 if ok else 1)

        elif sys.argv[1] in ("--test-vad-synthetic", "--vad-synthetic"):
            print(f"Jarvis Voice TUI v{__version__} — VAD synthetic test")
            print("=" * 50)
            ok, msg = test_vad_synthetic()
            print()
            print(f"{'✅ PASS' if ok else '❌ FAIL'}: {msg}")
            sys.exit(0 if ok else 1)

    JarvisTUI().run()
