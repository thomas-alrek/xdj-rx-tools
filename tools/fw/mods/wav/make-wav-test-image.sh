#!/usr/bin/env bash
# Test stick for the wav mod (macOS): a copy of a rekordbox test image plus a folder
# "XDJ WAV TEST" of generated test tones in formats stock firmware does and doesn't play.
# Browse them with BROWSE > [FOLDER]. Each file: 1 kHz left, 2 kHz right at -6 dBFS; the
# hi-res files also carry an ultrasonic tone (30 kHz, or 60 kHz at 192 kHz) at -12 dBFS
# that must not alias into the audible band.
#
#   tools/fw/mods/wav/make-wav-test-image.sh [SRC.img] [OUT.img]
#     defaults: test/usb/rekordbox_new.img  test/usb/wav_test.img
set -euo pipefail
cd "$(dirname "$0")/../../../.."
SRC="${1:-test/usb/rekordbox_new.img}"
OUT="${2:-test/usb/wav_test.img}"
[ "$(uname)" = Darwin ] || { echo "macOS only (hdiutil)" >&2; exit 1; }

TMP="$(mktemp -d)"; DEV=""
cleanup() { [ -n "$DEV" ] && hdiutil detach "$DEV" >/dev/null 2>&1 || true; rm -rf "$TMP"; }
trap cleanup EXIT
python3 "$(dirname "$0")/gen_test_wavs.py" "$TMP/XDJ WAV TEST"

cp "$SRC" "$OUT"
DEV=$(hdiutil attach -imagekey diskimage-class=CRawDiskImage -nobrowse -mountpoint "$TMP/mnt" \
  "$OUT" 2>/dev/null | awk 'NR==1{print $1}')
cp -RX "$TMP/XDJ WAV TEST" "$TMP/mnt/"
ls -l "$TMP/mnt/XDJ WAV TEST"
echo "$OUT: $SRC + XDJ WAV TEST"
