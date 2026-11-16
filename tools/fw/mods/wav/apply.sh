#!/usr/bin/env bash
# wav mod: WAV PCM 16/24/32 + float 32/64, 8-192 kHz, full precision - an xdjmod
# module (wav.c).
#   tools/fw/mods/wav/apply.sh UNPACKED OVERLAY
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
"$HERE/../preload/apply.sh" "$1" "$2" "$HERE/wav.c"
echo "wav: hi-res / float WAV reader"
