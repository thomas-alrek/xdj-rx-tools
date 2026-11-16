#!/usr/bin/env bash
# Build the custom firmware release (3.00) from the official 2.21 image: every mod, in
# order. Checks the base image's MD5 first and prints the result's; the expected values
# are in docs/firmware-mods.md §1 (the build is reproducible byte for byte).
#
#   tools/fw/build-release.sh [BASE.zip|.UPD] [OUTDIR]
#     defaults: build/XDJ-RX_v221.zip  build/release
set -euo pipefail
cd "$(dirname "$0")/../.."
REL=3.00
MODS=exfat,fps,wav,waveform,flac
BASE="${1:-build/XDJ-RX_v221.zip}"
OUT="${2:-build/release}"
export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-1767225600}"   # 2026-01-01: fixed timestamps
md5of() { command md5 -q "$1" 2>/dev/null || md5sum "$1" | cut -d' ' -f1; }
echo ">> base $BASE md5 $(md5of "$BASE")"
tools/fw/build-custom-upd.sh "$REL" "$MODS" "$BASE" "$OUT"
echo ">> $OUT/XDJRX.UPD (release $REL, mods $MODS) md5 $(md5of "$OUT/XDJRX.UPD")"
