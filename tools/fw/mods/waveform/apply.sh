#!/usr/bin/env bash
# waveform mod: UTILITY > WAVEFORM COLOR (BLUE (STOCK) / RGB / 3-BAND) - an xdjmod
# module (waveform.c), with smooth scrolling (scroll.c, UTILITY > WAVEFORM SCROLL).
#   tools/fw/mods/waveform/apply.sh UNPACKED OVERLAY
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
"$HERE/../preload/apply.sh" "$1" "$2" "$HERE/waveform.c" "$HERE/scroll.c"
echo "waveform: WAVEFORM COLOR row"
