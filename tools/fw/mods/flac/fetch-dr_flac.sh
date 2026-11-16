#!/usr/bin/env bash
# Fetch dr_flac.h (mackron/dr_libs, public domain / MIT-0) at a pinned commit, checksummed.
#   tools/fw/mods/flac/fetch-dr_flac.sh [OUTDIR]     default: build/mods/flac
set -euo pipefail
cd "$(dirname "$0")/../../../.."
OUT="${1:-build/mods/flac}"
COMMIT=f12e197fd462e4c22873fbd70d11b1d2f2c1dc3c          # dr_flac v0.13.5
SHA=3f010d3142a5b9df80a0bd8b325de54317468b1aff85abb257c86dbfefe9bd2a
mkdir -p "$OUT"
if ! echo "$SHA  $OUT/dr_flac.h" | shasum -a 256 -c - >/dev/null 2>&1; then
  curl -fsSL -o "$OUT/dr_flac.h" "https://raw.githubusercontent.com/mackron/dr_libs/$COMMIT/dr_flac.h"
  echo "$SHA  $OUT/dr_flac.h" | shasum -a 256 -c - >/dev/null || { echo "dr_flac.h: checksum mismatch" >&2; exit 1; }
fi
echo "$OUT/dr_flac.h"
