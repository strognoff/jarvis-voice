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

# ── Config loader ───────────────────────────────────────────────────
def _load_conf(path: Path) -> dict:
    """Parse a simple KEY = value config file. Returns a dict of strings."""
    conf = {}
    if not path.exists():
        return conf
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            conf[key.strip()] = val.strip()
    return conf

def _cfg(conf: dict, key: str, default: str) -> str:
    """Resolve a setting: env var > conf file > default."""
    env_key = f"JARVIS_{key}"
    return os.environ.get(env_key, conf.get(key, default))

_CONF = _load_conf(APP_DIR / "jarvis.conf")

# ── Config ─────────────────────────────────────────────────────────
GATEWAY_URL          = _cfg(_CONF, "GATEWAY_URL",          "http://localhost:18789")
AUTH_TOKEN           = _cfg(_CONF, "AUTH_TOKEN",           "")
SESSION_KEY          = _cfg(_CONF, "SESSION_KEY",          "agent:main:subagent:jarvis-tui")
OPENCLAW_SESSION_ID  = _cfg(_CONF, "OPENCLAW_SESSION_ID",  "jarvis-tui")
OPENCLAW_TIMEOUT     = int(_cfg(_CONF, "OPENCLAW_TIMEOUT", "90"))

# Audio / recording
SAMPLE_RATE       = 16000
FRAME_DURATION_MS = 30
# ENERGY_THRESHOLD  — VAD wake sensitivity: how loud the room must be to trigger a
#                     wake event. Set this high enough to ignore background noise.
# STT_ENERGY_THRESHOLD — pre-Whisper silence gate: recordings below this RMS are
#                     discarded as pure silence (no speech at all). Must be much
#                     LOWER than ENERGY_THRESHOLD — speech that was loud enough to
#                     wake the VAD may still measure below ENERGY_THRESHOLD when
#                     averaged across the full 8s recording (quiet start/end pads).
#                     True silence is RMS ~0-50; quiet speech is ~300-800.
ENERGY_THRESHOLD      = int(_cfg(_CONF, "ENERGY_THRESHOLD",     "800"))
STT_ENERGY_THRESHOLD  = int(_cfg(_CONF, "STT_ENERGY_THRESHOLD", "150"))
RECORD_DURATION   = float(_cfg(_CONF, "RECORD_DURATION", "8.0"))
MIC_DELAY         = float(_cfg(_CONF, "MIC_DELAY",       "1.0"))

# Wake word
WAKE_WORD         = _cfg(_CONF, "WAKE_WORD",         "hello").strip().lower()
REQUIRE_WAKE_WORD = _cfg(_CONF, "REQUIRE_WAKE_WORD", "0").lower() not in ("0", "false", "no")
WAKE_WORD_TIMEOUT = float(_cfg(_CONF, "WAKE_WORD_TIMEOUT", "5.0"))

# Timeouts
QUESTION_TIMEOUT          = float(_cfg(_CONF, "QUESTION_TIMEOUT",          "45"))
CONVERSATION_IDLE_TIMEOUT = float(_cfg(_CONF, "CONVERSATION_IDLE_TIMEOUT", "15"))

# TTS
TTS_SETTLE_SECONDS   = float(_cfg(_CONF, "TTS_SETTLE_SECONDS",   "0.8"))
TTS_WORDS_PER_MINUTE = float(_cfg(_CONF, "TTS_WORDS_PER_MINUTE", "155"))
TTS_MAX_WAIT_SECONDS = float(_cfg(_CONF, "TTS_MAX_WAIT_SECONDS", "18"))

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
    "idle":      ("😴", ""),
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

# ── Color palette (bot_avatar aesthetic) ────────────────────────────
# Derived from images/bot_avatar.jpg:
#   deep navy-black bg, silver metallic tones, electric blue glows, teal accents
def _build_colors() -> dict:
    """Return ANSI 24-bit true-color escape strings.

    Falls back to empty strings when NO_COLOR env var is set or when the
    terminal declares itself dumb (TERM=dumb).
    """
    no_color = bool(os.environ.get("NO_COLOR")) or os.environ.get("TERM") == "dumb"
    if no_color:
        return {k: "" for k in (
            "border", "title", "label", "status_active", "status_dim",
            "you_label", "you_text", "jarvis_label", "jarvis_text",
            "footer", "face", "reset",
        )}
    def fg(r, g, b):
        return f"\033[38;2;{r};{g};{b}m"
    return {
        "border":        fg(26,  99, 238),   # #1a63ee electric blue
        "title":         fg(91, 170, 255),   # #5baaff bright sky-blue
        "label":         fg(178, 183, 186),  # #b2b7ba silver-grey
        "status_active": fg(43,  212, 160),  # #2bd4a0 teal glow
        "status_dim":    fg(109, 169, 133),  # #6da985 muted teal
        "you_label":     fg(238, 239, 243),  # #eeeff3 near-white
        "you_text":      fg(200, 210, 230),  # soft white-blue
        "jarvis_label":  fg(91,  170, 255),  # #5baaff
        "jarvis_text":   fg(145, 208, 255),  # #91d0ff light blue
        "footer":        fg(66,   90, 118),  # #425a76 dim blue-grey
        "face":          fg(26,   99, 238),  # #1a63ee — face borders
        "reset":         "\033[0m",
    }

_COLORS = _build_colors()

def _c(name: str) -> str:
    """Return the ANSI color escape for a palette key, or '' if colors disabled."""
    return _COLORS.get(name, "")

import re as _re
_ANSI_ESCAPE = _re.compile(r"\033\[[^m]*m")

def _visible_len(s: str) -> int:
    """Length of string after stripping ANSI escape sequences."""
    return len(_ANSI_ESCAPE.sub("", s))

# ── Multi-frame animated robot face art ──────────────────────────────
# dict[state] -> list of frames, each frame = list of 7 strings (17 chars each)
# Accessed as: _FACE_FRAMES[state][frame % len(frames)][line_index]

def _ff(eyes: str, mouth: str) -> list[str]:
    """Build a 7-line face frame. eyes and mouth must each be exactly 7 visible chars."""
    return [
        "  ╔═══════════╗  ",
        "  ║ ▓▓▓▓▓▓▓▓▓ ║  ",
        f"  ║  {eyes}  ║  ",
        "  ║  ▔▔▔▔▔▔▔  ║  ",
        f"  ║  {mouth}  ║  ",
        "  ║▄▄▄▄▄▄▄▄▄▄▄║  ",
        "  ╚═══════════╝  ",
    ]

_FACE_FRAMES: dict[str, list[list[str]]] = {
    # idle: slow blink every 8 frames — eyes open (6 frames) then closed (2 frames)
    "idle": [
        _ff("◈     ◈", "╾─────╼"),
        _ff("◈     ◈", "╾─────╼"),
        _ff("◈     ◈", "╾─────╼"),
        _ff("◈     ◈", "╾─────╼"),
        _ff("◈     ◈", "╾─────╼"),
        _ff("◈     ◈", "╾─────╼"),
        _ff("─     ─", "╾─────╼"),   # blink closed
        _ff("◈     ◈", "╾─────╼"),
    ],
    # start: dots eyes, neutral mouth
    "start": [
        _ff("·     ·", "───────"),
        _ff("·     ·", "───────"),
        _ff("·     ·", "───────"),
        _ff("·     ·", "───────"),
    ],
    # wake: eyes snap wide, pulse ◉ ↔ ◎
    "wake": [
        _ff("◉     ◉", "╾──●──╼"),
        _ff("◎     ◎", "╾──●──╼"),
        _ff("◉     ◉", "╾─────╼"),
        _ff("◎     ◎", "╾──●──╼"),
    ],
    # listening: mouth dot bounces L→R→L across 5 interior positions
    "listening": [
        _ff("◉     ◉", "╾●────╼"),
        _ff("◉     ◉", "╾─●───╼"),
        _ff("◉     ◉", "╾──●──╼"),
        _ff("◉     ◉", "╾───●─╼"),
        _ff("◉     ◉", "╾────●╼"),
        _ff("◉     ◉", "╾───●─╼"),
        _ff("◉     ◉", "╾──●──╼"),
        _ff("◉     ◉", "╾─●───╼"),
    ],
    # more: same as listening
    "more": [
        _ff("◉     ◉", "╾●────╼"),
        _ff("◉     ◉", "╾─●───╼"),
        _ff("◉     ◉", "╾──●──╼"),
        _ff("◉     ◉", "╾───●─╼"),
        _ff("◉     ◉", "╾────●╼"),
        _ff("◉     ◉", "╾───●─╼"),
        _ff("◉     ◉", "╾──●──╼"),
        _ff("◉     ◉", "╾─●───╼"),
    ],
    # thinking: eyes rotate ◑◐ → ◐◑ → ◔◕ → ◕◔
    "thinking": [
        _ff("◑     ◐", "· · · ·"),
        _ff("◐     ◑", "· · · ·"),
        _ff("◔     ◕", "· · · ·"),
        _ff("◕     ◔", "· · · ·"),
        _ff("◑     ◐", "· · · ·"),
        _ff("◐     ◑", "· · · ·"),
        _ff("◔     ◕", "· · · ·"),
        _ff("◕     ◔", "· · · ·"),
    ],
    # busy: same as thinking
    "busy": [
        _ff("◑     ◐", "· · · ·"),
        _ff("◐     ◑", "· · · ·"),
        _ff("◔     ◕", "· · · ·"),
        _ff("◕     ◔", "· · · ·"),
        _ff("◑     ◐", "· · · ·"),
        _ff("◐     ◑", "· · · ·"),
        _ff("◔     ◕", "· · · ·"),
        _ff("◕     ◔", "· · · ·"),
    ],
    # speaking: ◆ dot traverses mouth bar left to right and back
    "speaking": [
        _ff("◈     ◈", "◄◆════►"),
        _ff("◈     ◈", "◄═◆═══►"),
        _ff("◈     ◈", "◄══◆══►"),
        _ff("◈     ◈", "◄═══◆═►"),
        _ff("◈     ◈", "◄════◆►"),
        _ff("◈     ◈", "◄═══◆═►"),
        _ff("◈     ◈", "◄══◆══►"),
        _ff("◈     ◈", "◄═◆═══►"),
    ],
    # error: eyes flicker × ↔ ✕
    "error": [
        _ff("×     ×", "───────"),
        _ff("✕     ✕", "───────"),
        _ff("×     ×", "───────"),
        _ff("✕     ✕", "───────"),
    ],
}
# Fallback for any unknown state
_FACE_FRAMES["__default__"] = _FACE_FRAMES["idle"]

_LOG_BUF: list[str] = []
_LOG_MAX = 50

# ── Structured file logger ────────────────────────────────────────────
#
# Writes to jarvis.log in the app directory.
# Format:  2026-05-07 14:23:01.123 [LEVEL] message
# Levels:  INFO  WARN  ERROR
#
# jarvis.log is never truncated on startup — each run appends a
# SESSION START / SESSION END banner so you can tell runs apart.
# Keep the last 50 messages in _LOG_BUF for the TUI display.

LOG_FILE = APP_DIR / "jarvis.log"
_log_lock = threading.Lock()

def _ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S") + f".{int(time.time() * 1000) % 1000:03d}"

def _write_log_file(level: str, msg: str):
    """Append one line to jarvis.log. Never raises."""
    try:
        with _log_lock:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"{_ts()} [{level:5s}] {msg}\n")
    except Exception:
        pass

def log(msg: str, level: str = "INFO"):
    """Log to TUI display buffer and jarvis.log."""
    entry = f"[{time.strftime('%H:%M:%S')}] {msg}"
    _LOG_BUF.append(entry)
    if len(_LOG_BUF) > _LOG_MAX:
        _LOG_BUF.pop(0)
    _write_log_file(level, msg)

def log_warn(msg: str):
    log(msg, level="WARN")

def log_error(msg: str):
    log(msg, level="ERROR")

def log_exception(context: str, exc: BaseException):
    """Log an exception with full traceback to jarvis.log."""
    trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    summary = f"{context}: {type(exc).__name__}: {exc}"
    log(summary, level="ERROR")
    try:
        with _log_lock:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"{_ts()} [ERROR] --- traceback ---\n")
                for line in trace.splitlines():
                    f.write(f"{_ts()} [ERROR] {line}\n")
                f.write(f"{_ts()} [ERROR] --- end traceback ---\n")
    except Exception:
        pass

def _log_session_banner(kind: str):
    """Write a visible session START/END separator to jarvis.log."""
    sep = "=" * 60
    try:
        with _log_lock:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"\n{sep}\n")
                f.write(f"{_ts()} [INFO ] SESSION {kind}  pid={os.getpid()}  v{__version__}\n")
                f.write(f"{sep}\n")
    except Exception:
        pass

def _log_thread_dump():
    """Dump all live thread names/states to jarvis.log — useful for hang diagnosis."""
    try:
        import threading as _th
        lines = [f"  Thread '{t.name}' daemon={t.daemon} alive={t.is_alive()}"
                 for t in _th.enumerate()]
        with _log_lock:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"{_ts()} [INFO ] --- thread dump ({len(lines)} threads) ---\n")
                for l in lines:
                    f.write(f"{_ts()} [INFO ] {l}\n")
                f.write(f"{_ts()} [INFO ] --- end thread dump ---\n")
    except Exception:
        pass

def _log_system_info():
    """Log platform, Python version, key env vars at session start."""
    import platform
    info = {
        "python": platform.python_version(),
        "platform": platform.system(),
        "machine": platform.machine(),
        "HAS_VAD": str(HAS_VAD),
        "GATEWAY_URL": GATEWAY_URL,
        "SESSION_KEY": SESSION_KEY,
        "REQUIRE_WAKE_WORD": str(REQUIRE_WAKE_WORD),
        "WAKE_WORD": f"{WAKE_WORD} (listen {WAKE_WORD_TIMEOUT}s)" if REQUIRE_WAKE_WORD else "(disabled)",
        "ENERGY_THRESHOLD": str(ENERGY_THRESHOLD),
        "STT_ENERGY_THRESHOLD": str(STT_ENERGY_THRESHOLD),
        "RECORD_DURATION": str(RECORD_DURATION),
    }
    for k, v in info.items():
        _write_log_file("INFO ", f"  {k} = {v}")

def _watched_thread(name: str, target, *args, daemon: bool = True, **kwargs) -> threading.Thread:
    """Wrap a thread target so any unhandled exception is logged to jarvis.log."""
    def _wrapper():
        log(f"Thread '{name}' started", level="INFO ")
        try:
            target(*args, **kwargs)
            log(f"Thread '{name}' exited normally", level="INFO ")
        except Exception as exc:
            log_exception(f"Thread '{name}' crashed", exc)
    t = threading.Thread(target=_wrapper, name=name, daemon=daemon)
    return t

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
        self._last_line_count = 0   # lines drawn in the previous frame
        self._last_width = 0        # terminal width used in the previous frame
        self._force_clear = False   # set True by SIGWINCH to trigger full 2J clear
        # ── Animation state ─────────────────────────────────────────
        self._anim_state_frame = 0    # resets on every state change
        self._prev_state = ""         # previous state (for transition effects)
        self._target_q = ""           # full question text (typewriter target)
        self._target_a = ""           # full answer text  (typewriter target)
        self._displayed_q = ""        # currently shown question (typewriter buffer)
        self._displayed_a = ""        # currently shown answer   (typewriter buffer)
        self._typewriter_speed = 8    # chars revealed per tick

    def update(self, state=None, status=None, question=None, answer=None):
        with self.lock:
            if state is not None and state != self.state:
                self._prev_state = self.state
                self.state = state
                self._anim_state_frame = 0
            elif state is not None:
                self.state = state
            if status is not None:
                self.status = status
            if question is not None:
                self._target_q = question
                self._displayed_q = ""          # restart typewriter
            if answer is not None:
                self._target_a = answer
                self._displayed_a = ""          # restart typewriter
            self.draw_locked()

    def tick(self):
        with self.lock:
            self.frame += 1
            self._anim_state_frame += 1
            # Advance typewriter buffers
            if self._displayed_q != self._target_q:
                end = len(self._displayed_q) + self._typewriter_speed
                self._displayed_q = self._target_q[:end]
            if self._displayed_a != self._target_a:
                end = len(self._displayed_a) + self._typewriter_speed
                self._displayed_a = self._target_a[:end]
            self.draw_locked()

    # ── Dimensions ──────────────────────────────────────────────────
    def _inner_width(self) -> int:
        cols = shutil.get_terminal_size((60, 24)).columns
        # Reserve space for face column (18 chars) + a comfortable text area
        return min(max(cols - 4, 52), 72)

    # ── Color helpers ───────────────────────────────────────────────
    @staticmethod
    def _c(name: str) -> str:
        return _c(name)

    def _B(self, s: str) -> str:
        """Wrap a box-drawing string in border color + reset."""
        return _c("border") + s + _c("reset")

    # ── Box primitives ───────────────────────────────────────────────
    def _hline(self, left: str, fill: str, right: str, w: int) -> str:
        return self._B(left + fill * (w + 2) + right)

    def _row(self, content: str, w: int, color: str = "") -> str:
        """A single bordered row.  content is the *visible* text (no ANSI).
        color optionally wraps the inner content in a color."""
        inner = content[:w].ljust(w)
        if color:
            inner = color + inner + _c("reset")
        return self._B("║") + " " + inner + " " + self._B("║")

    def _colored_row(self, colored_content: str, visible_len: int, w: int) -> str:
        """Like _row but content already contains ANSI escapes; visible_len is
        its display width so we can pad correctly."""
        padding = " " * max(0, w - visible_len)
        return self._B("║") + " " + colored_content + padding + " " + self._B("║")

    def _blank(self, w: int) -> str:
        return self._row("", w)

    # ── Animated face ────────────────────────────────────────────────
    def _animated_face(self) -> list[str]:
        """Return the current animation frame's face lines."""
        frames = _FACE_FRAMES.get(self.state, _FACE_FRAMES["__default__"])
        return frames[self.frame % len(frames)]

    # ── Title glyph (heartbeat pulse) ───────────────────────────────
    def _title_glyph(self) -> str:
        """Cycle ◈ → ◇ → ◆ → ◈ for a heartbeat glow effect."""
        glyphs = ["◈", "◇", "◆", "◇"]
        return glyphs[self.frame % len(glyphs)]

    # ── Border color pulse ───────────────────────────────────────────
    def _border_color(self) -> str:
        """Cycle through 3 blue shades for border glow pulse."""
        no_color = bool(os.environ.get("NO_COLOR")) or os.environ.get("TERM") == "dumb"
        if no_color:
            return ""
        # deep blue → electric blue → bright cyan → back
        shades = [
            "\033[38;2;20;70;180m",   # dim blue
            "\033[38;2;26;99;238m",   # electric blue (normal)
            "\033[38;2;60;140;255m",  # bright blue
            "\033[38;2;26;99;238m",   # electric blue
        ]
        return shades[self.frame % len(shades)]

    # ── Wake flash border color ──────────────────────────────────────
    def _effective_border_color(self) -> str:
        """On wake state within first 8 anim frames: flash cyan-white."""
        no_color = bool(os.environ.get("NO_COLOR")) or os.environ.get("TERM") == "dumb"
        if no_color:
            return ""
        if self.state == "wake" and self._anim_state_frame < 8:
            flash = [
                "\033[38;2;100;200;255m",
                "\033[38;2;255;255;255m",
                "\033[38;2;100;200;255m",
                "\033[38;2;60;140;255m",
            ]
            return flash[self._anim_state_frame % len(flash)]
        return self._border_color()

    # ── Scan line border ─────────────────────────────────────────────
    def _scan_line_border(self, w: int) -> str:
        """Top border with a bright traveling ◈ character (HUD radar sweep)."""
        no_color = bool(os.environ.get("NO_COLOR")) or os.environ.get("TERM") == "dumb"
        bc = self._effective_border_color()
        R = _c("reset")
        fill = w + 2  # total inner chars including the 2 padding spaces

        if no_color:
            return bc + "╔" + "═" * fill + "╗" + R

        # Scan dot travels across fill positions every 60 frames (ping-pong)
        period = 60
        pos = (self.frame * 2) % (fill * 2)   # ping-pong
        if pos >= fill:
            pos = fill * 2 - 1 - pos
        pos = max(0, min(fill - 1, pos))

        chars = ["═"] * fill
        chars[pos] = "◈"

        bright = "\033[38;2;150;220;255m"
        line = ""
        for i, ch in enumerate(chars):
            if i == pos:
                line += R + bright + ch + R + bc
            else:
                line += ch

        return bc + "╔" + line + "╗" + R

    # ── Waveform visualizer (listening / speaking) ────────────────────
    def _waveform(self, width: int) -> str:
        """Animated ASCII waveform bar — purely cosmetic, driven by frame."""
        import math
        wave_chars = " ▁▂▃▄▅▆▇█"
        result = []
        for i in range(width):
            # Multiple overlapping sine waves for organic feel
            t = self.frame * 0.4 + i * 0.6
            v = (math.sin(t) * 0.5 +
                 math.sin(t * 1.7 + 1.2) * 0.3 +
                 math.sin(t * 2.9 + 0.7) * 0.2)
            v = (v + 1.0) / 2.0  # normalize to 0..1
            idx = int(v * (len(wave_chars) - 1))
            result.append(wave_chars[idx])
        return "".join(result)

    # ── Thinking particle trail ───────────────────────────────────────
    def _thinking_trail(self, width: int) -> str:
        """Scrolling bright dot across a field of dim dots."""
        trail = list("· " * ((width // 2) + 1))
        trail = trail[:width]
        # Bright dot position: travels across width over 20 frames
        pos = (self.frame * 2) % (width * 2)
        if pos >= width:
            pos = width * 2 - 1 - pos
        pos = max(0, min(width - 1, pos))
        trail[pos] = "●"
        return "".join(trail)[:width]

    # ── Idle particle field ──────────────────────────────────────────
    def _particle_row(self, width: int, row_seed: int) -> str:
        """Sparse drifting particles for idle screensaver feel."""
        import math
        result = []
        for i in range(width):
            # Each cell has a unique phase
            phase = (self.frame + row_seed * 7 + i * 13) % 20
            if phase == 0:
                result.append("✦")
            elif phase == 5:
                result.append("◦")
            elif phase == 10:
                result.append("·")
            else:
                result.append(" ")
        return "".join(result)[:width]

    # ── Speaking sound wave ──────────────────────────────────────────
    def _sound_wave(self, width: int) -> str:
        """Shifting block-character wave for speaking state."""
        wave = "▁▂▃▄▅▆▇█▇▆▅▄▃▂▁▁"
        offset = self.frame % len(wave)
        result = []
        for i in range(width):
            result.append(wave[(i + offset) % len(wave)])
        return "".join(result)[:width]

    # ── Status ──────────────────────────────────────────────────────
    def _status_text(self) -> str:
        """Build the animated status string (no borders)."""
        state = self.state
        emoji, label = _STATE_META.get(state, ("🤖", ""))
        text = self.status if self.status else label
        if state in ("listening", "more"):
            dots = _DOTS[self.frame % len(_DOTS)]
            return f"{emoji}  {text}  {dots}"
        elif state in ("thinking", "busy"):
            spin = _SPIN[self.frame % len(_SPIN)]
            return f"{emoji}  {text}  {spin}"
        else:
            return f"{emoji}  {text}" if text else emoji

    # ── Full redraw ──────────────────────────────────────────────────
    def draw_locked(self):
        w = self._inner_width()
        R = _c("reset")
        bc = self._effective_border_color()   # pulsing/flashing border color

        def B(s: str) -> str:
            return bc + s + R

        # ── Box lines ───────────���────────────────────────────────────
        top = self._scan_line_border(w)            # animated scan line top border
        mid = B("╠" + "═" * (w + 2) + "╣")
        bot = B("╚" + "═" * (w + 2) + "╝")

        def crow(colored_content: str, vis: int) -> str:
            pad = " " * max(0, w - vis)
            return B("║") + " " + colored_content + pad + " " + B("║")

        def plain_row(text: str, color: str = "") -> str:
            inner = text[:w].ljust(w)
            if color:
                inner = color + inner + R
            return B("║") + " " + inner + " " + B("║")

        def blank() -> str:
            return plain_row("")

        # ── Title bar — pulsing glyph + color ────────────────────────
        g = self._title_glyph()
        title_str = f"  {g}  J A R V I S  {g}   v{__version__}"
        tc = _c("title")
        title_row = crow(tc + title_str + R, len(title_str))

        # ── Animated face block + right-side status visuals ──────────
        FACE_W = 17
        face_lines = self._animated_face()
        rw = w - FACE_W - 2          # right-column width

        # State-dependent status color
        active_states = {"listening", "more", "thinking", "busy", "wake", "speaking"}
        s_color = _c("status_active") if self.state in active_states else _c("status_dim")

        # Right-side content per row (7 rows = face height).
        # Each entry must be exactly rw visible chars wide (pre-padded), then ANSI-wrapped.
        def _rcell(text: str, color: str) -> str:
            """Pad text to exactly rw visible chars, then wrap in color."""
            padded = text[:rw].ljust(rw)
            return color + padded + R if color else padded

        status_text = self._status_text()
        blank_rc = " " * rw

        right_col = [blank_rc] * 7

        # Row 2: status text (animated)
        right_col[2] = _rcell(status_text, s_color)

        # Rows 3-4: state-specific visualizer (functions already return exactly rw chars)
        if self.state in ("listening", "more"):
            right_col[3] = _rcell(self._waveform(rw), _c("status_active"))
            right_col[4] = _rcell(self._waveform(rw), _c("status_dim"))
        elif self.state in ("thinking", "busy"):
            right_col[3] = _rcell(self._thinking_trail(rw), _c("status_active"))
            right_col[4] = _rcell(self._thinking_trail(rw), _c("status_dim"))
        elif self.state == "speaking":
            right_col[3] = _rcell(self._sound_wave(rw), _c("status_active"))
            right_col[4] = _rcell(self._sound_wave(rw), _c("status_dim"))
        elif self.state == "idle":
            right_col[3] = _rcell(self._particle_row(rw, 0), _c("status_dim"))
            right_col[4] = _rcell(self._particle_row(rw, 3), _c("status_dim"))
        elif self.state == "wake":
            right_col[3] = _rcell(self._waveform(rw), _c("status_active"))
            right_col[4] = _rcell(self._thinking_trail(rw), _c("status_active"))

        # Row 5: secondary status label (non-active states only)
        if self.status and self.state not in ("listening", "more", "thinking", "busy", "speaking", "wake"):
            right_col[5] = _rcell(self.status, _c("label"))

        face_status_rows = []
        for i, face_line in enumerate(face_lines):
            # face_line is exactly 17 visible chars; right_col[i] is exactly rw visible chars
            fp = _c("face") + face_line + R
            rc = right_col[i]               # exactly rw visible chars (may have ANSI codes)
            combined = fp + "  " + rc       # ANSI-wrapped; visible = 17 + 2 + rw = FACE_W+2+rw
            face_status_rows.append(crow(combined, FACE_W + 2 + rw))

        # ── You / question section (typewriter) ──────────────────────
        q_text = self._displayed_q or self._target_q
        has_q = bool(q_text)
        q_lines = _wrap(q_text, w - 4) if has_q else []
        you_label_row = crow(_c("you_label") + "  ◂ You" + R, 6)
        you_rows = [
            crow(_c("you_text") + "    " + l + R, 4 + len(l))
            for l in q_lines
        ]

        # ── Jarvis / answer section (typewriter) ─────────────────────
        a_text = self._displayed_a or self._target_a
        has_a = bool(a_text)
        a_lines = _wrap(a_text, w - 4) if has_a else []
        jarvis_label_row = crow(_c("jarvis_label") + "  ◈ Jarvis" + R, 9)
        jarvis_rows = [
            crow(_c("jarvis_text") + "    " + l + R, 4 + len(l))
            for l in a_lines
        ]

        # ── Footer ───────────────────────────────────────────────────
        footer_str = "  speak to wake  ·  Ctrl+C to quit"
        footer_row = crow(_c("footer") + footer_str + R, len(footer_str))

        # ── Assemble ─────────────────────────────────────────────────
        out = [
            top,
            title_row,
            mid,
            blank(),
            *face_status_rows,
            blank(),
            mid,
        ]
        if has_q:
            out += [you_label_row, *you_rows, mid]
        if has_a:
            out += [jarvis_label_row, *jarvis_rows, mid]
        out += [
            footer_row,
            bot,
            "",
        ]

        new_line_count = len(out)
        resize = (w != self._last_width) or self._force_clear

        buf = []
        if not self._started:
            # First draw: clear entire screen + scrollback, hide cursor, go home
            buf.append("\033[2J\033[H\033[?25l")
            self._started = True
        elif resize:
            # Terminal resized (keyboard shown/hidden): full clear then home
            buf.append("\033[2J\033[H")
            self._force_clear = False
        else:
            # Move cursor UP by exactly how many lines we drew last frame.
            # \033[{n}A is a *relative* move — safe even if terminal has scrolled,
            # unlike \033[H which jumps to the scrollback-buffer top on Termux.
            if self._last_line_count > 0:
                buf.append(f"\033[{self._last_line_count}A")
            buf.append("\033[1G")  # move to column 1 of current line

        # Write each line: erase whole line first (\033[2K erases left+right of
        # cursor), then content, then \r\n.  Leftover chars from a previously
        # wider frame are fully cleared before we overwrite.
        for line in out:
            buf.append("\033[2K")
            buf.append(line)
            buf.append("\r\n")

        # Erase any extra lines that existed last frame but not this one
        extra = self._last_line_count - new_line_count
        for _ in range(max(0, extra)):
            buf.append("\033[2K\r\n")

        self._last_line_count = new_line_count
        self._last_width = w

        sys.stdout.write("".join(buf))
        sys.stdout.flush()


SCREEN = Screen()

# Handle terminal resize (SIGWINCH) — fires when the Android soft keyboard
# is shown or hidden, changing the terminal dimensions.  We set _force_clear
# so the next draw_locked() call does a full \033[2J wipe before redrawing,
# preventing the old wider/taller layout from bleeding through.
try:
    import signal as _signal
    def _on_sigwinch(signum, frame):
        SCREEN._force_clear = True
    _signal.signal(_signal.SIGWINCH, _on_sigwinch)
except (AttributeError, OSError):
    pass   # SIGWINCH not available on all platforms


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

def _split_sentences(text: str, max_chars: int = 200) -> list[str]:
    """Split text into speakable chunks at sentence boundaries.

    Splits on '.', '!', '?', ':' followed by whitespace, keeping each chunk
    under max_chars. Falls back to splitting on commas, then hard-truncating
    if no good boundary is found.
    """
    import re as _re
    # Split on sentence-ending punctuation followed by space/end
    raw = _re.split(r'(?<=[.!?:])\s+', text.strip())
    chunks: list[str] = []
    current = ""
    for sentence in raw:
        if not sentence:
            continue
        if not current:
            current = sentence
        elif len(current) + 1 + len(sentence) <= max_chars:
            current += " " + sentence
        else:
            chunks.append(current)
            current = sentence
    if current:
        chunks.append(current)

    # If any chunk is still too long, split on commas
    final: list[str] = []
    for chunk in chunks:
        if len(chunk) <= max_chars:
            final.append(chunk)
        else:
            parts = _re.split(r'(?<=,)\s+', chunk)
            buf = ""
            for part in parts:
                if not buf:
                    buf = part
                elif len(buf) + 1 + len(part) <= max_chars:
                    buf += " " + part
                else:
                    final.append(buf)
                    buf = part
            if buf:
                final.append(buf)

    return [c.strip() for c in final if c.strip()]


_BEEP_WAKE = APP_DIR / "beep_wake.wav"   # high tone — "I'm listening"
_BEEP_END  = APP_DIR / "beep_end.wav"    # low tone  — "conversation over"


def _ensure_beeps():
    """Generate beep WAV files via ffmpeg if they don't exist yet."""
    specs = [
        (_BEEP_WAKE, "880", "0.18"),   # 880 Hz, 0.18s
        (_BEEP_END,  "440", "0.35"),   # 440 Hz, 0.35s
    ]
    for path, freq, dur in specs:
        if not path.exists():
            try:
                subprocess.run(
                    ["ffmpeg", "-y", "-f", "lavfi",
                     "-i", f"sine=frequency={freq}:duration={dur}",
                     "-ar", "44100", str(path)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=10,
                )
                log(f"Generated beep: {path.name}")
            except Exception as e:
                log_warn(f"Could not generate {path.name}: {e}")


def _beep(kind: str = "wake"):
    """Play a short beep sound in a background thread.

    kind="wake" → high tone (listening),  kind="end" → low tone (done).
    Tries termux-media-player first, falls back to ffplay.
    """
    path = str(_BEEP_WAKE if kind == "wake" else _BEEP_END)

    def _play():
        # Try termux-media-player (built into Termux:API)
        if shutil.which("termux-media-player"):
            try:
                subprocess.run(
                    ["termux-media-player", "play", path],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=5,
                )
                return
            except Exception:
                pass
        # Fallback: ffplay (part of ffmpeg suite)
        if shutil.which("ffplay"):
            try:
                subprocess.run(
                    ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", path],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=5,
                )
                return
            except Exception:
                pass
        log_warn("_beep: no audio player available (need termux-media-player or ffplay)")

    threading.Thread(target=_play, daemon=True).start()


def speak(text: str) -> str:
    """Speak text via termux-tts-speak, chunked into short sentences.

    Long responses used to time out as a single TTS call. We now split at
    sentence boundaries (max 200 chars per chunk) so each call completes
    well within the per-chunk timeout.
    """
    spoken = speech_text(text)
    if not spoken:
        return spoken

    chunks = _split_sentences(spoken, max_chars=200)
    if not chunks:
        return spoken

    log(f"TTS: {len(chunks)} chunk(s), total {len(spoken)} chars")
    for i, chunk in enumerate(chunks):
        try:
            subprocess.run(
                ["termux-tts-speak", chunk],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=15,
            )
        except subprocess.TimeoutExpired:
            log_error(f"TTS chunk {i+1}/{len(chunks)} timed out — skipping rest: {chunk[:60]!r}")
            break
        except Exception as e:
            log_error(f"TTS chunk {i+1}/{len(chunks)} error: {e}")
            break

    return spoken

def wait_for_tts(text: str):
    words = max(1, len(text.split()))
    estimated = words / max(1.0, TTS_WORDS_PER_MINUTE) * 60.0
    delay = min(TTS_MAX_WAIT_SECONDS, max(TTS_SETTLE_SECONDS, estimated + TTS_SETTLE_SECONDS))
    time.sleep(delay)

# ── STT ─────────────────────────────────────────────────────────────

# whisper-cli model — search common Termux locations
_WHISPER_MODEL_SEARCH = [
    _cfg(_CONF, "WHISPER_MODEL", ""),  # explicit conf/env path takes priority
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

# Whisper commonly hallucinates these on silence or background noise
_WHISPER_HALLUCINATIONS = {
    "", ".", "..", "...", "…",
    "you", "you.", "you!", "you?",
    "thank you", "thank you.", "thank you!",
    "thanks", "thanks.", "thanks!",
    "bye", "bye.", "goodbye", "goodbye.",
    "okay", "okay.", "ok", "ok.",
    "um", "uh", "hmm", "hm",
    "[blank_audio]", "[silence]", "[noise]", "[music]", "(silence)",
    "subtitles by", "subtitles:", "translated by",
}

def _wav_rms(wav_path: str) -> float:
    """Return the RMS energy of a 16kHz PCM WAV file."""
    try:
        import wave as _wave
        with _wave.open(wav_path, "rb") as wf:
            raw = wf.readframes(wf.getnframes())
        return compute_rms(raw)
    except Exception:
        return 0.0

def _transcribe_wav(wav_path: str) -> str:
    """Transcribe a WAV file using whisper-cli.

    Checks RMS energy first — if the recording is silent, returns ""
    immediately without running Whisper (saves ~8s and prevents false triggers).
    """
    # Energy gate — skip Whisper entirely on truly silent recordings.
    # Uses STT_ENERGY_THRESHOLD (not ENERGY_THRESHOLD) — see config comment.
    rms = _wav_rms(wav_path)
    log(f"STT energy check: RMS={rms:.0f}  gate={STT_ENERGY_THRESHOLD}  vad_threshold={ENERGY_THRESHOLD}")
    if rms < STT_ENERGY_THRESHOLD:
        log(f"Recording silent (RMS={rms:.0f} < STT gate {STT_ENERGY_THRESHOLD}) — skipping Whisper")
        return ""

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
                "--no-prints",
                "--no-timestamps",
            ],
            capture_output=True, text=True, timeout=30,
        )
        lines = [l.strip() for l in result.stdout.splitlines() if l.strip()]
        text = " ".join(lines).strip()
        # Filter hallucinations
        if text.lower().rstrip(".,!? ") in _WHISPER_HALLUCINATIONS or \
           text.lower().strip() in _WHISPER_HALLUCINATIONS:
            log(f"Whisper hallucination filtered: '{text}'")
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

def _force_kill_proc(proc: subprocess.Popen, context: str):
    """Escalate: SIGTERM → SIGKILL → killpg. Logs each step to jarvis.log."""
    try:
        proc.terminate()
        proc.wait(timeout=1)
        return
    except subprocess.TimeoutExpired:
        log_warn(f"{context}: SIGTERM ignored, sending SIGKILL")
    except Exception:
        pass
    try:
        proc.kill()
        proc.wait(timeout=1)
        return
    except subprocess.TimeoutExpired:
        log_warn(f"{context}: SIGKILL ignored, trying killpg")
    except Exception:
        pass
    try:
        import signal
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        log_warn(f"{context}: sent SIGKILL to process group")
    except Exception as e:
        log_error(f"{context}: could not kill process group: {e}")


def _record_wav(wav_path: str, duration: float) -> bool:
    """Record audio for `duration` seconds and convert to 16kHz mono PCM.

    termux-microphone-record produces a WAV with a varying codec/sample-rate
    depending on the device. whisper-cli requires 16kHz mono 16-bit PCM.
    We record to a raw file then convert in-place with ffmpeg.
    Returns True if the final file has usable content.

    The raw.wav file is ALWAYS deleted in the finally block regardless of
    which failure path is taken — this prevents jarvis_listen.wav.raw.wav
    from being left behind when the recorder or ffmpeg hangs.
    """
    raw_path = wav_path + ".raw.wav"

    # Clean up any stale files from a previous crashed run
    for p in (wav_path, raw_path):
        try:
            Path(p).unlink(missing_ok=True)
        except Exception as e:
            log_warn(f"_record_wav: could not remove stale {p}: {e}")

    proc = None
    try:
        proc = subprocess.Popen(
            ["termux-microphone-record", "-f", raw_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        time.sleep(duration)

        # Graceful stop via the control command
        try:
            subprocess.run(
                ["termux-microphone-record", "-q"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=3,
            )
        except Exception as e:
            log_warn(f"_record_wav: stop command failed: {e}")

        # Wait for the recorder process to exit; escalate if it doesn't
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            log_warn("_record_wav: recorder did not exit after stop — force killing")
            _force_kill_proc(proc, "_record_wav recorder")
            proc = None

    except Exception as e:
        log_exception("_record_wav: recorder failed to start", e)
        return False

    raw_p = Path(raw_path)
    if not raw_p.exists():
        log_warn(f"_record_wav: raw file missing after recording: {raw_path}")
        return False
    raw_size = raw_p.stat().st_size
    if raw_size < 512:
        log_warn(f"_record_wav: raw file too small ({raw_size}B) — mic may be silent or blocked")
        return False

    # Convert to 16kHz mono 16-bit PCM — required by whisper-cli.
    # raw_path is deleted in the finally block regardless of ffmpeg outcome.
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", raw_path,
             "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", wav_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=15,
        )
        if r.returncode != 0:
            log_error(f"_record_wav: ffmpeg exited {r.returncode} converting {raw_path}")
            return False
        out_size = Path(wav_path).stat().st_size if Path(wav_path).exists() else 0
        if out_size < 512:
            log_error(f"_record_wav: converted WAV too small ({out_size}B)")
            return False
        return True
    except subprocess.TimeoutExpired:
        log_error(f"_record_wav: ffmpeg timed out converting {raw_path} — file may be corrupt/held open")
        return False
    except Exception as e:
        log_exception("_record_wav: ffmpeg error", e)
        return False
    finally:
        # ALWAYS remove raw.wav — this is the line that prevents the hang-file
        try:
            Path(raw_path).unlink(missing_ok=True)
        except Exception as e:
            log_error(f"_record_wav: could not delete raw file {raw_path}: {e}")


def _wait_for_silence(max_wait: float = 5.0):
    """Wait until room audio drops below ENERGY_THRESHOLD after TTS.

    Records short 0.5s probe chunks and waits until the RMS energy falls
    below threshold — handles speaker bleed/reverb adaptively regardless of
    volume. Gives up after max_wait seconds.
    """
    import tempfile
    probe = str(Path(tempfile.gettempdir()) / "jarvis_probe.wav")
    PROBE_DURATION = 1.0   # 0.5s was too short — mic needs ~0.8s to open on Termux
    TAIL_BUFFER    = 0.3   # extra quiet time after silence detected
    deadline = time.time() + max_wait

    SCREEN.update(status="Waiting for speaker to finish...")

    while time.time() < deadline:
        _record_wav(probe, PROBE_DURATION)
        rms = _wav_rms(probe)
        log(f"[silence probe] RMS={rms:.0f} threshold={ENERGY_THRESHOLD}")
        if rms < ENERGY_THRESHOLD:
            time.sleep(TAIL_BUFFER)  # let the last reverb die out
            return
        # Still noisy — wait a moment before probing again
        time.sleep(0.1)

    log("_wait_for_silence: gave up waiting — proceeding anyway")


def _countdown_bar(duration: float, stop_event: threading.Event):
    """Animate a smooth countdown bar using sub-character block precision.
    Runs in a background thread — stops when stop_event is set."""
    bar_width = 14  # characters wide
    start = time.time()
    # Block chars from full to empty: █▉▊▋▌▍▎▏
    BLOCKS = "█▉▊▋▌▍▎▏ "
    while not stop_event.is_set():
        elapsed = time.time() - start
        remaining = max(0.0, duration - elapsed)
        ratio = remaining / duration   # 1.0 → 0.0

        # Full blocks
        full = ratio * bar_width
        n_full = int(full)
        frac = full - n_full   # fractional part → pick sub-block char

        if frac > 0:
            sub_idx = int((1.0 - frac) * (len(BLOCKS) - 1))
            sub_char = BLOCKS[sub_idx]
        else:
            sub_char = ""

        bar = "█" * n_full + sub_char
        bar = bar.ljust(bar_width)[:bar_width]

        secs = int(remaining) + 1
        SCREEN.update(status=f"🎙 [{bar}] {secs}s")
        time.sleep(0.12)   # ~8fps for smooth sub-block animation


def listen(timeout: float = QUESTION_TIMEOUT) -> str:
    """Record a single continuous audio clip then transcribe with whisper-cli."""
    import tempfile

    wav = str(Path(tempfile.gettempdir()) / "jarvis_listen.wav")
    duration = min(timeout, RECORD_DURATION)

    # Wait until the room goes quiet after TTS — adaptively handles speaker
    # bleed and reverb regardless of volume or response length.
    _wait_for_silence(max_wait=MIC_DELAY + 4.0)

    # Start countdown animation in background.
    # stop_countdown is set in finally — guaranteed even if _record_wav hangs/raises.
    stop_countdown = threading.Event()
    t = threading.Thread(target=_countdown_bar, args=(duration, stop_countdown), daemon=True)
    t.start()

    ok = False
    try:
        ok = _record_wav(wav, duration)
    finally:
        stop_countdown.set()
        t.join(timeout=duration + 2.0)   # should be instant; 2s safety margin
        if t.is_alive():
            log_warn("listen: countdown bar thread did not exit — abandoning it")

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

    # Consecutive stop-command failures before we declare the mic service dead
    _MAX_MIC_FAILURES = 3

    def _record_chunk(self, wav_path: str, duration_s: float) -> bool:
        """Record audio for duration_s seconds, kill the process, return True on success.

        Returns False if the stop command failed (caller tracks consecutive failures).
        Always kills by PID directly — never relies solely on -q when we know it may hang.
        """
        proc = None
        stop_ok = True
        try:
            proc = subprocess.Popen(
                ['termux-microphone-record', '-f', wav_path],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True
            )
            time.sleep(duration_s)

            # Try graceful stop, but don't wait long — we'll kill by PID regardless
            try:
                subprocess.run(
                    ['termux-microphone-record', '-q'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=2
                )
            except subprocess.TimeoutExpired:
                # Mic service unresponsive — likely screen lock revoked mic access.
                # Block for up to 180s waiting for Android to restore mic (screen unlock).
                log_warn("[vad] stop command timed out — mic likely locked, waiting up to 180s for recovery")
                SCREEN.update(status="⏳ Mic locked — waiting for screen unlock…")
                try:
                    subprocess.run(
                        ['termux-microphone-record', '-q'],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        timeout=180
                    )
                    log("[vad] mic service recovered after extended wait")
                    SCREEN.update(status="🎙 Listening…")
                except Exception as e:
                    log_warn(f"[vad] stop command failed after 180s wait: {e}")
                    stop_ok = False
            except Exception as e:
                log_warn(f"[vad] stop command failed: {e}")
                stop_ok = False

            # Always kill the recorder proc by PID — don't trust -q alone
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                _force_kill_proc(proc, "vad _record_chunk")

        except Exception as e:
            log_exception("[vad] _record_chunk failed", e)
            stop_ok = False
            if proc is not None:
                _force_kill_proc(proc, "vad _record_chunk (exception path)")

        return stop_ok

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
        consecutive_failures = 0
        backoff = 5.0      # seconds to wait after mic service declared dead
        chunks_recorded = 0
        MIC_REST_INTERVAL = 30   # release mic every ~30s to prevent Android audio lock-up
        MIC_REST_SECONDS  = 2.0  # how long to pause before resuming

        while self.running:
            # Remove old file
            if wav_file.exists():
                try:
                    wav_file.unlink()
                except Exception:
                    pass

            # Record 1 second chunk — returns False if stop command failed
            stop_ok = self._record_chunk(str(wav_file), 1.0)

            if not stop_ok:
                consecutive_failures += 1
                if consecutive_failures >= self._MAX_MIC_FAILURES:
                    log_error(
                        f"[vad] mic service unresponsive after {consecutive_failures} "
                        f"consecutive failures — backing off {backoff:.0f}s then restarting"
                    )
                    SCREEN.update(status=f"⚠ Mic stuck — retrying in {backoff:.0f}s")
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)   # cap at 30s
                    consecutive_failures = 0
                    # Exit loop — watchdog or _resume_vad will start a fresh VADLoop
                    self.running = False
                    break
                continue
            else:
                if consecutive_failures > 0:
                    log(f"[vad] mic service recovered after {consecutive_failures} failure(s)")
                consecutive_failures = 0
                chunks_recorded += 1

                # Periodic mic rest — release Android audio hardware briefly to
                # prevent the Termux API service from locking up after ~5min idle
                if chunks_recorded % MIC_REST_INTERVAL == 0:
                    log(f"[vad] periodic mic rest ({MIC_REST_SECONDS}s) at chunk {chunks_recorded}")
                    time.sleep(MIC_REST_SECONDS)
                    backoff = 5.0   # reset backoff after a successful rest cycle

            if not wav_file.exists() or wav_file.stat().st_size < 512:
                continue

            frames = self._read_pcm_frames(wav_file)
            if not frames:
                continue

            for chunk_data in frames:
                is_speech = False
                rms = compute_rms(chunk_data)
                if self.vad:
                    try:
                        vad_says_speech = self.vad.is_speech(chunk_data, self.sample_rate)
                    except Exception:
                        vad_says_speech = False
                    # Require BOTH WebRTC VAD and a minimum energy floor.
                    # WebRTC alone fires on quiet background noise (RMS=20-50).
                    # The floor is 1/4 of ENERGY_THRESHOLD — low enough to catch
                    # real speech but high enough to reject room hiss and TV noise.
                    if vad_says_speech and rms > ENERGY_THRESHOLD // 4:
                        is_speech = True
                else:
                    # No WebRTC VAD — fall back to pure energy threshold
                    if rms > ENERGY_THRESHOLD:
                        is_speech = True

                if is_speech:
                    speech_frames += 1
                    silence_frames = 0
                    if not is_speaking and speech_frames >= min_speech:
                        is_speaking = True
                        log(f"[vad] wake trigger: RMS={rms:.0f} threshold={ENERGY_THRESHOLD//4} (floor)")
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
        self._vad_thread = None          # updated by run() and _resume_vad(); watched by watchdog
        self._vad_resume_lock = threading.Lock()  # prevents double-resume races

    def _pause_vad(self):
        """Stop VAD and release mic before STT takes over."""
        if self.vad:
            log("Pausing VAD — releasing mic for STT")
            self.vad.stop()
            # Stop any in-flight recording and give it time to release the mic.
            # VAD records 1s chunks — worst case we need to wait for the current
            # chunk to finish + ffmpeg convert + mic release before we can record.
            try:
                subprocess.run(
                    ['termux-microphone-record', '-q'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=3
                )
            except Exception as e:
                log_warn(f"_pause_vad: stop command failed: {e}")
            time.sleep(1.5)
            log("VAD paused — mic released")

    def _resume_vad(self):
        """Restart VAD using a FRESH VADLoop instance to avoid running-flag races.

        Serialised by _vad_resume_lock so simultaneous calls from the watchdog
        and on_wake's finally block don't create two competing VADLoops.
        """
        with self._vad_resume_lock:
            # Idempotent: if a live thread already exists, don't start another
            if self._vad_thread is not None and self._vad_thread.is_alive():
                log("_resume_vad: VAD thread already alive — skipping duplicate resume")
                return
            log("Resuming VAD — creating fresh VADLoop")
            self.vad = VADLoop()
            self.vad.start()
            self._vad_thread = _watched_thread("vad-loop-resume", self.vad.vad_loop, self.on_wake)
            self._vad_thread.start()
            log("VAD resumed — listening for wake")

    def on_wake(self):
        """Called by VADLoop when speech+silence detected."""
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
            render("idle", "")
            self._resume_vad()

    def answer_question(self, question: str) -> str:
        # Wake word gate — only applies on first-word check, not mid-conversation
        if REQUIRE_WAKE_WORD:
            words = question.strip().split()
            # Strip punctuation from the first word so "Jarvis," matches "jarvis"
            first_word_clean = words[0].strip(".,!?;:\"'").lower() if words else ""
            if first_word_clean != WAKE_WORD:
                log(f"Wake word '{WAKE_WORD}' not found — first word was '{first_word_clean}' in: {question!r}")
                SCREEN.update(status=f"Heard: '{first_word_clean}' — expected '{WAKE_WORD}'")
                return None   # sentinel: caller handles display
            # Wake word matched — show it on screen and strip it from the question
            SCREEN.update(status=f"◈ Wake word '{WAKE_WORD}' ✓")
            question = " ".join(words[1:]).strip(".,!?;: ")
            log(f"Wake word '{WAKE_WORD}' detected — sending: {question!r}")

        SCREEN.update(status="Sending to OpenClaw...")
        response = send_message(SESSION_KEY, question)
        if response:
            log(f"OpenClaw replied ({len(response)} chars)")
            return response

        # OpenClaw not available — use local fallback
        SCREEN.update(status="OpenClaw unavailable — using local fallback")
        log("OpenClaw returned empty — using local fallback")

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
        """Run a full conversation: say Yes? → listen → think → speak → loop."""
        self.state = "busy"
        self.pending_intent = None

        try:
            if not question:
                render("listening", "Ask your question — I'm recording for 8s")
                _beep()
                question = listen(timeout=QUESTION_TIMEOUT)
                SCREEN.update(question=question or "")

            while question:
                SCREEN.update(question=question)
                render("thinking", "Working on it...")
                response = self.answer_question(question)

                if response is None:
                    # Wake word required but not spoken as first word.
                    # status was already set to "Heard: 'X' — expected 'Y'" by answer_question.
                    SCREEN.update(
                        state="error",
                        answer=f"⚠  Start with '{WAKE_WORD}' to send to OpenClaw",
                    )
                    time.sleep(2.5)   # hold on screen so user can read it
                    break

                SCREEN.update(answer=response)
                render("speaking", "Answering...")
                speak(response)
                render("listening", "Your turn — speak now or stay silent to end")
                question = listen(timeout=CONVERSATION_IDLE_TIMEOUT)
                SCREEN.update(question=question or "")

            else:
                # Silence timeout — end conversation normally
                render("idle", "")

        except Exception as e:
            log_exception("conversation error", e)
        finally:
            self.state = "idle"
            render("idle", "")

    def run(self):
        _log_session_banner("START")
        _log_system_info()
        _ensure_beeps()

        # Clean up any stale temp files left by a previous crash
        import tempfile, glob as _glob
        stale = set(_glob.glob(str(Path(tempfile.gettempdir()) / "jarvis_*.wav")))
        for f in stale:
            try:
                Path(f).unlink(missing_ok=True)
                log_warn(f"Deleted stale temp file from previous crash: {f}")
            except Exception as e:
                log_error(f"Could not delete stale file {f}: {e}")

        missing = [cmd for cmd in ("termux-tts-speak", "whisper-cli",
                                   "termux-microphone-record")
                   if shutil.which(cmd) is None]
        if missing:
            for cmd in missing:
                log_error(f"Missing required command: {cmd}")
            sys.stdout.write("\033[?25h")
            sys.stdout.flush()
            print("❌  Missing required commands:")
            for cmd in missing:
                print(f"    • {cmd}")
            print("    Run: pkg install termux-api whisper.cpp")
            _log_session_banner("END (missing commands)")
            sys.exit(1)

        log(f"Gateway: {GATEWAY_URL}  Session: {SESSION_KEY}")
        model = _find_whisper_model()
        if model:
            log(f"Whisper model: {Path(model).name}")
        else:
            log_warn("No whisper model found — set WHISPER_MODEL=/path/to/ggml-*.bin")

        self.running = True
        self.vad = VADLoop()

        # Animation ticker — wrapped so crashes write to jarvis.log
        def _ticker_fn():
            while self.running:
                try:
                    SCREEN.tick()
                except Exception as exc:
                    log_exception("ticker crash", exc)
                time.sleep(0.25)

        _watched_thread("ticker", _ticker_fn).start()

        # VAD loop — wrapped so crashes write to jarvis.log.
        # self._vad_thread always points to the current active VAD thread so the
        # watchdog can check the right one (updated by _resume_vad on each resume).
        self.vad.start()
        self._vad_thread = _watched_thread("vad-loop", self.vad.vad_loop, self.on_wake)
        self._vad_thread.start()

        # Watchdog — polls every 5s for a dead VAD thread (fast recovery after
        # mic-stuck backoff), and dumps threads every 60s for diagnostics.
        # Only restarts VAD when state is "idle" — during conversations VAD is
        # intentionally paused so the thread being dead is normal.
        def _watchdog_fn():
            last_dump = time.time()
            while self.running:
                time.sleep(5)
                if not self.running:
                    break

                # Thread dump every 60s
                if time.time() - last_dump >= 60:
                    _log_thread_dump()
                    last_dump = time.time()

                # Restart VAD if it died while idle
                if not self._vad_thread.is_alive() and self.state == "idle":
                    log_error("VAD thread not alive while idle — restarting")
                    try:
                        self._resume_vad()
                    except Exception as exc:
                        log_exception("Watchdog VAD restart failed", exc)

        _watched_thread("watchdog", _watchdog_fn).start()

        render("idle", "")
        log("Ready — waiting for voice activity")

        exit_reason = "normal"
        try:
            while self.running:
                time.sleep(0.5)
        except KeyboardInterrupt:
            exit_reason = "KeyboardInterrupt"
            log("Interrupted by user (Ctrl+C)")
        except Exception as exc:
            exit_reason = f"{type(exc).__name__}"
            log_exception("Main loop crashed", exc)
        finally:
            log(f"Shutting down — reason: {exit_reason}")
            self.vad.stop()
            self.running = False
            _log_thread_dump()
            _log_session_banner(f"END ({exit_reason})")
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
