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
STT_CONTINUATION_TIMEOUT = float(os.environ.get("JARVIS_STT_CONTINUATION_TIMEOUT", "8.0"))
STT_MAX_PARTS = int(os.environ.get("JARVIS_STT_MAX_PARTS", "3"))
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

# ── TUI ─────────────────────────────────────────────────────────────

# Per-state: (emoji, label)
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

_DOTS   = ["● ○ ○", "● ● ○", "● ● ●", "○ ○ ○"]
_SPIN   = list("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")

# In-memory log ring buffer — log() writes here, never to stdout
_LOG_BUF: list[str] = []
_LOG_MAX = 50

def log(msg: str):
    """Append to in-memory ring buffer; never print to stdout."""
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
    pass  # Banner is now part of the live TUI box; nothing to print on startup.

def _wrap(text: str, width: int, max_lines: int = 2) -> list[str]:
    """Word-wrap text into at most max_lines of the given width."""
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
                # Truncate last line with ellipsis
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

    # ── Internal helpers ──────────────────────────────────────────

    def _inner_width(self) -> int:
        cols = shutil.get_terminal_size((50, 24)).columns
        return min(max(cols - 4, 38), 76)  # 4 = 2 border + 2 padding each side

    def _hline(self, left: str, fill: str, right: str, w: int) -> str:
        return left + fill * (w + 2) + right

    def _row(self, content: str, w: int) -> str:
        """Pad content to exactly w chars, wrapped in border."""
        cell = content[:w].ljust(w)
        return f"║ {cell} ║"

    def _blank(self, w: int) -> str:
        return self._row("", w)

    def _status_line(self, w: int) -> str:
        state = self.state
        emoji, label = _STATE_META.get(state, ("🤖", ""))
        # Override label with explicit status if set
        text = self.status if self.status else label

        if state in ("listening", "more"):
            dots = _DOTS[self.frame % len(_DOTS)]
            status_str = f"{emoji}  {text}  {dots}"
        elif state == "thinking":
            spinner = _SPIN[self.frame % len(_SPIN)]
            status_str = f"{emoji}  {text}  {spinner}"
        else:
            status_str = f"{emoji}  {text}" if text else f"{emoji}"

        return self._row(status_str, w)

    def draw_locked(self):
        w = self._inner_width()
        iw = w  # text inner width (inside the 1-char padding on each side)

        top    = self._hline("╔", "═", "╗", w)
        mid    = self._hline("╠", "═", "╣", w)
        bot    = self._hline("╚", "═", "╝", w)

        # Title row — keep it short so emoji width doesn't clip the version
        title_row = self._row(f"  JARVIS  v{__version__}", w)

        # Status / animation row
        status_row = self._status_line(w)

        # You block
        q_text = self.question or ""
        q_lines = _wrap(q_text, iw - 2) if q_text else ["—"]
        you_label = self._row("  You", w)
        you_rows  = [self._row("  " + l, w) for l in q_lines]

        # Jarvis block
        a_text = self.answer or ""
        a_lines = _wrap(a_text, iw - 2) if a_text else ["—"]
        jar_label = self._row("  Jarvis", w)
        jar_rows  = [self._row("  " + l, w) for l in a_lines]

        # Footer — inside the box, above the bottom border
        wake_hint = f"wake: \"{WAKE_WORD}\"" if REQUIRE_WAKE_WORD else "always listening"
        footer_row = self._row(f"  {wake_hint}  ·  Ctrl+C to quit", w)

        # Assemble — fixed structure, always same number of lines
        out = [
            top,
            title_row,
            mid,
            self._blank(w),
            status_row,
            self._blank(w),
            mid,
            you_label,
            *you_rows,
            mid,
            jar_label,
            *jar_rows,
            mid,
            footer_row,
            bot,
            "",  # trailing newline guard
        ]

        # Move to top of screen; clear screen only on first draw
        if not self._started:
            sys.stdout.write("\033[2J\033[H\033[?25l")  # clear + hide cursor
            self._started = True
        else:
            sys.stdout.write("\033[H")

        sys.stdout.write("\n".join(out))
        sys.stdout.flush()


SCREEN = Screen()


def render(face: str, status: str = ""):
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

def best_transcript(lines):
    """Return the single most complete line from a list of STT partials.

    When all lines come from one STT call, prefer the last line (Android's
    final committed result).  Fall back to the longest if the last line is
    dramatically shorter (handles rare mid-utterance resets).
    """
    if not lines:
        return ""
    last = lines[-1]
    longest = max(lines, key=lambda l: (len(l.split()), len(l)))
    if len(last.split()) < len(longest.split()) // 2:
        return longest
    return last

def _parse_stt_output(stdout: str) -> str:
    """Parse raw termux-speech-to-text stdout into a clean transcript.

    Android STT streams partial results line-by-line.  Two modes exist:
      - Cumulative: lines grow ("where is" → "where is Tring") → last line wins
      - Non-cumulative: independent partials ("where is" / "Tring") → merge all
    """
    lines = [l.strip() for l in stdout.splitlines() if l.strip()]
    if not lines:
        return ""
    if len(lines) == 1:
        return lines[0]

    # Detect cumulative mode: last line already contains all earlier lines
    last_lower = lines[-1].lower()
    if all(l.lower() in last_lower for l in lines[:-1]):
        return lines[-1]

    # Non-cumulative mode: merge all partials into one sentence
    return merge_transcripts(lines)


# Words that cannot grammatically end a sentence — if the transcript ends with
# one of these, Android cut off mid-utterance and we must fire a continuation.
_INCOMPLETE_ENDINGS = {
    # Articles — cannot end a sentence
    "a", "an", "the",
    # Prepositions — cannot end a meaningful sentence on their own
    "in", "on", "at", "to", "of", "for", "from", "with", "by", "about",
    "into", "onto", "upon", "within", "without", "between", "among",
    "near", "over", "under", "through", "across", "along", "beside",
    # Coordinating conjunctions mid-thought
    "and", "or", "but", "nor",
    # Possessive determiners — always precede a noun
    "my", "your", "his", "her", "its", "our", "their",
    # Demonstrative determiners before a noun
    "this", "that", "these", "those",
    # Open question words that need an object
    "what", "where", "when", "why", "how", "which", "who", "whose",
}

def _is_incomplete(text: str) -> bool:
    """Return True if the transcript ends with a word that cannot end a sentence."""
    if not text:
        return False
    last_word = text.rstrip(".,!?").split()[-1].lower()
    return last_word in _INCOMPLETE_ENDINGS


def start_stt_process() -> subprocess.Popen:
    """Launch termux-speech-to-text in the background so it is already
    capturing audio before the user starts speaking.  Call collect_stt_process()
    to read the result."""
    return subprocess.Popen(
        ["termux-speech-to-text"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )


def collect_stt_process(proc: subprocess.Popen, timeout: float) -> str:
    """Wait for a pre-rolled STT process and return the merged transcript.

    Strategy: always launch the *next* STT process the instant the current one
    finishes — before we even check whether the result is complete.  This way
    the continuation is already capturing audio during the tiny gap where the
    user speaks the trailing word ("Tring") before we know we need it.

    If the result turns out to be complete we simply kill the pre-rolled
    continuation and return.  If it's incomplete we collect it instead.
    """
    parts = []
    current_proc = proc
    current_timeout = timeout

    for attempt in range(STT_MAX_PARTS):
        try:
            stdout, _ = current_proc.communicate(timeout=current_timeout)
            result = _parse_stt_output(stdout)
        except subprocess.TimeoutExpired:
            current_proc.kill()
            current_proc.communicate()
            break
        except Exception as e:
            log(f"STT error: {e}")
            break

        if not result:
            break

        # Immediately pre-roll the next process so it's already warm & listening
        # while we do the completeness check and screen update below.
        next_proc = start_stt_process() if attempt + 1 < STT_MAX_PARTS else None

        parts.append(result)
        merged = merge_transcripts(parts)

        if not _is_incomplete(merged):
            # Sentence is complete — kill the pre-rolled process we don't need
            if next_proc:
                next_proc.kill()
                try:
                    next_proc.communicate()
                except Exception:
                    pass
            return merged

        # Incomplete — collect the pre-rolled continuation
        SCREEN.update(status="Listening for more...")
        if next_proc is None:
            return merged
        current_proc = next_proc
        current_timeout = STT_CONTINUATION_TIMEOUT

    return merge_transcripts(parts) if parts else ""


def listen(timeout: float = QUESTION_TIMEOUT) -> str:
    """Start termux-speech-to-text and return the transcript."""
    proc = start_stt_process()
    return collect_stt_process(proc, timeout)

def merge_transcripts(parts):
    """Merge STT partial results into one coherent transcript.

    Handles three cases:
    - Exact duplicate / subset: discard the shorter one
    - Cumulative extension: keep the longer one
    - Genuinely new words: append, stripping any overlapping suffix/prefix
      so we don't get doubled words like "Tell me a a story"
    """
    merged = ""
    for part in (p.strip() for p in parts if p and p.strip()):
        if not merged:
            merged = part
            continue
        lower_merged = merged.lower()
        lower_part = part.lower()
        # Part is already contained in merged — skip
        if lower_part in lower_merged:
            continue
        # Merged is a prefix of part — part is the better cumulative result
        if lower_merged in lower_part:
            merged = part
            continue
        # Try to find an overlapping suffix of merged / prefix of part
        # to avoid doubling words at the join boundary
        merged_words = lower_merged.split()
        part_words = lower_part.split()
        overlap = 0
        for n in range(min(len(merged_words), len(part_words), 4), 0, -1):
            if merged_words[-n:] == part_words[:n]:
                overlap = n
                break
        new_words = part.split()[overlap:]
        if new_words:
            merged = merged + " " + " ".join(new_words)
        # else: full overlap, nothing new to add
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
        else:
            SCREEN.update(status="⚠ No WebRTC VAD — energy fallback")

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
        import tempfile as _tempfile
        work_dir = Path(_tempfile.gettempdir())
        wav_file = work_dir / "jarvis_vad_chunk.wav"

        speech_frames = 0
        silence_frames = 0
        min_speech = 4
        min_silence = silence_threshold_frames
        is_speaking = False
        threshold = ENERGY_THRESHOLD  # local alias, avoids late binding issues in loops

        CHUNK_DURATION = 1.0  # seconds per recording slice

        while self.running:
            # ── S1: Remove old file ─────────────────────────────────
            if wav_file.exists():
                try:
                    os.remove(wav_file)
                except Exception:
                    pass

            # ── S2: Record one chunk via start → sleep → explicit stop
            # The termux-microphone-record -l <n> duration flag is unreliable:
            # the process often ignores it and never exits, so proc.wait()
            # always hits TimeoutExpired and prints the "was terminated" warning.
            # The correct approach: start without -l, sleep the desired duration,
            # then stop cleanly with `-q` (the official quit command).
            with _recording_lock:
                try:
                    proc = subprocess.Popen(
                        ['termux-microphone-record', '-f', str(wav_file)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        start_new_session=True
                    )
                    time.sleep(CHUNK_DURATION)
                    # Explicitly stop the recording — this is the correct termux API
                    subprocess.run(
                        ['termux-microphone-record', '-q'],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        timeout=3
                    )
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    try:
                        proc.terminate()
                        proc.wait(timeout=1)
                    except Exception:
                        pass
                except Exception as e:
                    log(f"[vad] record error: {e}")
                    continue

                if not self._wait_for_recording(wav_file):
                    SCREEN.update(status="⚠ mic: recording not flushed yet")
                    continue

            # ── S4: Check WAV was created ──────────────────────
            if not wav_file.exists():
                SCREEN.update(status="⚠ mic: no file — check permission")
                continue

            file_size = wav_file.stat().st_size
            if file_size < 2000:
                SCREEN.update(status=f"⚠ mic: file too small ({file_size}B)")
                continue

            frames = self._read_pcm_frames(wav_file)
            if not frames:
                SCREEN.update(status="⚠ mic: no PCM data from ffmpeg")
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
                            SCREEN.update(status="Voice detected — waking up...")
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

    def _pause_vad(self):
        """Stop the VAD recording loop so it doesn't compete with termux-speech-to-text.
        Explicitly stops any in-flight recording and waits for the mic to be released."""
        if self.vad:
            self.vad.stop()
            # Stop any in-flight recording and wait for mic to be released
            subprocess.run(['termux-microphone-record', '-q'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
            time.sleep(0.5)  # shorter wait since we explicitly stopped the recording

    def _resume_vad(self):
        """Restart the VAD recording loop in a fresh thread after a conversation ends."""
        if self.vad:
            self.vad.start()
            t = threading.Thread(
                target=self.vad.vad_loop, args=(self.on_wake,), daemon=True
            )
            t.start()

    def on_wake(self):
        """Called when VAD detects end of speech utterance."""
        if self.state != "idle":
            return
        log("Wake detected")
        # Mark busy immediately so re-entrant VAD firings are ignored
        self.state = "busy"
        self.pending_intent = None

        # Stop VAD recording — termux mic can only be used by one process at a time.
        # While we're in conversation, termux-speech-to-text owns the microphone.
        self._pause_vad()

        try:
            question = ""

            # Step 1: optionally verify wake word
            if REQUIRE_WAKE_WORD:
                render("listening", f'Say "{WAKE_WORD}"...')
                text = listen(timeout=QUESTION_TIMEOUT)
                SCREEN.update(question=text or "")
                is_wake, question = extract_question(text)
                if not is_wake:
                    if text:
                        log(f"Ignored speech without wake phrase: {text}")
                    return  # finally block restores state

            # Step 2: if no question yet, speak "Yes?" then listen AFTER TTS finishes
            if not question:
                render("speaking", "")
                speak("Yes?")          # blocks until TTS done
                render("listening", "")
                question = collect_stt_process(start_stt_process(), QUESTION_TIMEOUT)
                SCREEN.update(question=question or "")

            # Step 3: conversation loop — think → speak → listen, never overlapping
            while question:
                SCREEN.update(question=question)
                render("thinking", "")
                response = self.answer_question(question)
                SCREEN.update(answer=response)
                render("speaking", "")
                speak(response)        # blocks until TTS done
                render("listening", "")
                question = collect_stt_process(start_stt_process(), CONVERSATION_IDLE_TIMEOUT)
                SCREEN.update(question=question or "")

            # Step 4: conversation ended by silence
            log(f"Conversation ended (no reply after {CONVERSATION_IDLE_TIMEOUT:.0f}s)")
            render("speaking", "")
            speak("Goodbye!")

        finally:
            # Always restore state and restart VAD, even on exceptions
            self.state = "idle"
            render("idle", "")
            self._resume_vad()

    def run(self):
        # Verify termux-api before starting the TUI
        missing = [cmd for cmd in ("termux-tts-speak", "termux-speech-to-text",
                                   "termux-microphone-record", "ffmpeg")
                   if shutil.which(cmd) is None]
        if missing:
            sys.stdout.write("\033[?25h")  # restore cursor before error output
            sys.stdout.flush()
            print("❌  termux-api not found! Run: pkg install termux-api")
            print(f"    Missing: {', '.join(missing)}")
            sys.exit(1)

        log(f"Gateway: {GATEWAY_URL}  Session: {SESSION_KEY}")
        log(f"VAD: {'WebRTC' if HAS_VAD else 'energy-only'}")

        self.running = True
        self.vad = VADLoop()

        vad_thread = threading.Thread(target=self.vad.vad_loop, args=(self.on_wake,), daemon=True)
        self.vad.start()
        vad_thread.start()
        render("idle")

        try:
            while self.running:
                SCREEN.tick()
                time.sleep(0.25)
        except KeyboardInterrupt:
            pass
        finally:
            self.vad.stop()
            self.running = False
            sys.stdout.write("\033[?25h\n")  # restore cursor
            sys.stdout.flush()




def test_vad_synthetic():
    """Test VAD on generated synthetic audio (no microphone needed). Returns (ok, message)."""
    import webrtcvad, wave, struct, math, os, tempfile
    from pathlib import Path

    wav_file = Path(tempfile.gettempdir()) / "jarvis_vad_synthetic_test.wav"

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
            is_sp = vad.is_speech(chunk, SAMPLE_RATE)
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

    wav_file = Path(tempfile.gettempdir()) / "jarvis_vad_test_chunk.wav"

    # Remove existing file (termux-mic refuses to overwrite)
    if wav_file.exists():
        try:
            wav_file.unlink()
        except Exception:
            pass

    # Step 1: Record 3 seconds directly to WAV
    # Use start→sleep→stop(-q) instead of -l 3, because the -l duration flag
    # is unreliable on many Termux versions and causes the process to hang.
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
        try:
            is_sp = vad.is_speech(chunk, rate)
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

def test_mic(duration=3.0, out_path=None):
    """Quick mic test — records `duration` seconds, checks file, plays it back."""
    import tempfile
    if out_path is None:
        out_path = str(Path(tempfile.gettempdir()) / "jarvis_mic_test.wav")
    wav_file = Path(out_path)
    # Remove existing file (termux-mic refuses to overwrite)
    if wav_file.exists():
        try:
            wav_file.unlink()
        except Exception:
            pass

    print(f"🎤 Recording {duration}s... (speak clearly)")

    # Use start→sleep→stop(-q): the -l duration flag is unreliable and hangs.
    with _recording_lock:
        proc = subprocess.Popen(
            ['termux-microphone-record', '-f', str(wav_file)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True
        )
        time.sleep(duration)
        subprocess.run(
            ['termux-microphone-record', '-q'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=3
        )
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.terminate()
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
