#!/bin/bash
# diagnose_mic.sh - Run these commands in Termux to diagnose mic issues

echo "=== Termux Microphone Diagnostic ==="
echo ""
echo "1. Checking termux-microphone-record availability..."
which termux-microphone-record && echo "OK: termux-microphone-record found" || echo "FAIL: not found"
echo ""

echo "2. Checking microphone permissions..."
# Try to record for 1 second and see what happens
echo "Recording 1 second to test_mic_diag.m4a..."
termux-microphone-record -f test_mic_diag.m4a -l 1
RET=$?
echo "Exit code: $RET"
sleep 1
if [ -f test_mic_diag.m4a ]; then
    SIZE=$(stat -c%s test_mic_diag.m4a 2>/dev/null || stat -f%z test_mic_diag.m4a 2>/dev/null)
    echo "File size: $SIZE bytes"
    if [ "$SIZE" -lt 1000 ]; then
        echo "⚠️  File too small — mic may not have captured audio"
    else
        echo "✅ File created with content"
    fi
else
    echo "❌ File not created at all"
fi
echo ""

echo "3. Checking if Termux has microphone permission..."
# Check Android permissions (this is a best-effort check)
if command -v dumpsys &>/dev/null; then
    echo "Checking via dumpsys..."
    # This would need root or special permission
fi

echo "4. Checking for conflicting processes..."
ps -ef 2>/dev/null | grep -i "microphone\|mediarecorder\|voice" | grep -v grep || echo "No obvious conflicting processes"

echo ""
echo "=== Next Steps ==="
echo "If file not created or too small:"
echo "  1. Open Android Settings > Apps > Termux > Permissions"
echo "  2. Ensure 'Microphone' permission is GRANTED"
echo "  3. Also check 'Files and media' if using scoped storage"
echo "  4. Run: termux-microphone-record -f test2.m4a -l 1"
echo "  5. Check file size with: ls -la test2.m4a"
