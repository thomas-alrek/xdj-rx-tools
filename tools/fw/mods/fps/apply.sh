#!/usr/bin/env bash
# fps mod: UTILITY > UI FRAME RATE (20 (STOCK) / 30 / 60) and SCREEN SYNC (OFF / lead) - xdjmod
# modules fps.c and sync.c.
#   tools/fw/mods/fps/apply.sh UNPACKED OVERLAY
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
"$HERE/../preload/apply.sh" "$1" "$2" "$HERE/fps.c" "$HERE/sync.c"
echo "fps: UI FRAME RATE + SCREEN SYNC rows"
