#!/usr/bin/env bash
# flac mod: FLAC playback (16/24-bit, up to 192 kHz) through the wav mod's reader - xdjmod
# modules wav.c + flac.c, with dr_flac (fetched at a pinned commit).
#   tools/fw/mods/flac/apply.sh UNPACKED OVERLAY
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
HDR="$("$HERE/fetch-dr_flac.sh")"
HDR="$(cd "$(dirname "$HDR")" && pwd)/$(basename "$HDR")"
"$HERE/../preload/apply.sh" "$1" "$2" "$HERE/../wav/wav.c" "$HERE/flac.c" "$HDR"
echo "flac: FLAC via the wav reader"
