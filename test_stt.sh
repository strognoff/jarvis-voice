#!/bin/bash
# test_stt.sh — Quick end-to-end microphone + Whisper STT test
#
# Usage:
#   bash test_stt.sh            # records 5 seconds
#   bash test_stt.sh 8          # records 8 seconds

DURATION=${1:-5}
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WAV="$SCRIPT_DIR/jarvis_stt_test.wav"
MODEL=/data/data/com.termux/files/home/whisper.cpp/models/ggml-base.en.bin

echo ""
echo "  ┌─────────────────────────────────┐"
echo "  │   Jarvis STT Test               │"
echo "  └─────────────────────────────────┘"
echo ""

# ── Check dependencies ──────────────────────────────────────────────
if ! command -v termux-microphone-record &>/dev/null; then
    echo "  ❌  termux-microphone-record not found"
    echo "      Run: pkg install termux-api"
    exit 1
fi

if ! command -v whisper-cli &>/dev/null; then
    echo "  ❌  whisper-cli not found"
    echo "      Run: pkg install whisper.cpp"
    exit 1
fi

if [ ! -f "$MODEL" ]; then
    echo "  ❌  Model not found: $MODEL"
    echo "      Download with:"
    echo "      curl -L -o $MODEL "
    echo "        https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-medium.en.bin"
    exit 1
fi

echo "  ✓  termux-microphone-record found"
echo "  ✓  whisper-cli found"
echo "  ✓  Model: $(basename $MODEL)"
echo ""

# ── Record ──────────────────────────────────────────────────────────
rm -f "$WAV"

echo "  🎤  Recording for ${DURATION}s — speak now..."
echo ""

termux-microphone-record -f "$WAV" &>/dev/null &
REC_PID=$!
sleep "$DURATION"
termux-microphone-record -q &>/dev/null
wait $REC_PID 2>/dev/null

sleep 0.5   # let the file flush

if [ ! -f "$WAV" ]; then
    echo "  ❌  No file created — check Termux microphone permission"
    echo "      Settings → Apps → Termux → Permissions → Microphone → Allow"
    exit 1
fi

SIZE=$(stat -c%s "$WAV" 2>/dev/null || stat -f%z "$WAV" 2>/dev/null)
if [ "$SIZE" -lt 512 ]; then
    echo "  ❌  File too small (${SIZE} bytes) — microphone may be blocked"
    exit 1
fi

echo "  ✓  Recorded ${SIZE} bytes"

# Show what format the mic produced
echo "  ℹ️   Raw format:"
ffprobe -v quiet -show_streams -select_streams a "$WAV" 2>&1 \
    | grep -E "codec_name|sample_rate|channels|bit_rate" \
    | sed 's/^/       /'
echo ""

# ── Convert to 16kHz mono 16-bit PCM (required by whisper-cli) ──────
WAV_16K="${WAV%.wav}_16k.wav"
echo "  🔄  Converting to 16kHz PCM..."
ffmpeg -y -i "$WAV" -ar 16000 -ac 1 -c:a pcm_s16le "$WAV_16K" -loglevel error
if [ $? -ne 0 ] || [ ! -f "$WAV_16K" ]; then
    echo "  ❌  ffmpeg conversion failed"
    exit 1
fi
echo "  ✓  Converted: $(basename $WAV_16K)"
echo ""

# ── Transcribe ──────────────────────────────────────────────────────
echo "  🧠  Transcribing..."
echo ""

TRANSCRIPT=$(whisper-cli \
    -m "$MODEL" \
    -f "$WAV_16K" \
    -l en \
    --no-prints \
    --no-timestamps \
    2>/dev/null)

echo "  ┌─────────────────────────────────┐"
if [ -z "$TRANSCRIPT" ]; then
    echo "  │  (no speech detected)           │"
else
    # Word-wrap at ~33 chars for the box
    echo "$TRANSCRIPT" | fold -s -w 33 | while IFS= read -r line; do
        printf "  │  %-33s│
" "$line"
    done
fi
echo "  └─────────────────────────────────┘"
echo ""

if [ -z "$TRANSCRIPT" ]; then
    echo "  ⚠️   Nothing transcribed."
    echo "      Try speaking louder or closer to the mic."
    exit 1
else
    echo "  ✅  STT is working!"
fi

rm -f "$WAV" "$WAV_16K"
echo ""
