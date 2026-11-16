#!/usr/bin/env bash
# Test stick for the flac mod (macOS, needs `flac`): a library-less FAT32 stick with the
# wav mod's test tones (1 kHz left / 2 kHz right, plus an ultrasonic tone in the hi-res
# ones) as FLAC, and the 44.1 kHz WAV as a control. Browse with BROWSE > [FOLDER].
#
#   tools/fw/mods/flac/make-flac-test-image.sh [OUT.img]     default: test/usb/flac_test.img
set -euo pipefail
cd "$(dirname "$0")/../../../.."
OUT="${1:-test/usb/flac_test.img}"
command -v flac >/dev/null || { echo "needs the flac encoder (brew install flac)" >&2; exit 1; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
python3 tools/fw/mods/wav/gen_test_wavs.py "$TMP/wav" >/dev/null
mkdir -p "$TMP/stick"
# FLAC has no float: the integer tones only
# Vorbis comments (shown in the browser's track info); "Café" checks the text encoding
TAGS=(--tag=ARTIST="XDJ Test Tones" --tag=ALBUM="FLAC Test Stick" --tag=GENRE="Test"
      --tag=COMMENT="Café - 1 kHz left, 2 kHz right")
for n in t_44k_16 t_48k_24 t_96k_24 t_192k_24x; do
  flac -s -f "${TAGS[@]}" --tag=TITLE="Tone ${n#t_}" -o "$TMP/stick/f${n#t}.flac" "$TMP/wav/$n.wav"
done
# one without a seek table (seeking falls back to a search)
flac -s -f "${TAGS[@]}" --tag=TITLE="Tone 192k no seektable" --no-seektable \
  -o "$TMP/stick/f_192k_24_noseek.flac" "$TMP/wav/t_192k_24x.wav"
cp "$TMP/wav/t_44k_16.wav" "$TMP/stick/w_44k_16_control.wav"
tools/make-fat32-image.sh "$OUT" "$TMP/stick"/* | tail -1
