#!/usr/bin/env bash
# fps mod: UTILITY > UI FRAME RATE (20 (STOCK) / 30 / 60) - an xdjmod module (fps.c).
#   tools/fw/mods/fps/apply.sh UNPACKED OVERLAY
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
"$HERE/../preload/apply.sh" "$1" "$2" "$HERE/fps.c"
echo "fps: UI FRAME RATE row"
