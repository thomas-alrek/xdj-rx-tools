#!/usr/bin/env bash
# keys mod: UTILITY > KEY DISPLAY (CLASSIC / CAMELOT) - an xdjmod module (keys.c).
#   tools/fw/mods/keys/apply.sh UNPACKED OVERLAY
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
"$HERE/../preload/apply.sh" "$1" "$2" "$HERE/keys.c"
echo "keys: KEY DISPLAY row"
