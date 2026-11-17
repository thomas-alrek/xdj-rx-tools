#!/usr/bin/env bash
# beatjump mod: BEAT JUMP on the pads (SHIFT + BEAT LOOP), opt-in - an xdjmod module
# (beatjump.c). UTILITY rows BEAT JUMP (OFF/ON), BEAT JUMP A, BEAT JUMP B.
#   tools/fw/mods/beatjump/apply.sh UNPACKED OVERLAY
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
"$HERE/../preload/apply.sh" "$1" "$2" "$HERE/beatjump.c"
echo "beatjump: BEAT JUMP rows"
