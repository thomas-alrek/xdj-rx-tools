#!/usr/bin/env bash
# Build a custom firmware update from the official image plus a set of mods
# (tools/fw/mods/<name>/apply.sh). Each mod writes into an overlay generated at build
# time from the unpacked firmware, so no Pioneer-owned file is ever committed.
#
#   tools/fw/build-custom-upd.sh RELEASE MOD[,MOD...] [BASE.UPD|.zip] [OUTDIR]
#     e.g. tools/fw/build-custom-upd.sh 3.00 exfat
#     defaults: build/XDJ-RX_v221.zip  build/custom-upd
#
# Writes OUTDIR/XDJRX.UPD and OUTDIR/stick.img. Install: the app's File > Install
# Firmware Update… (pick XDJRX.UPD). RELEASE must be above the installed one. The release
# itself (3.00, every mod) is tools/fw/build-release.sh.
set -euo pipefail
cd "$(dirname "$0")/../.."
REL="${1:?release number, e.g. 3.00}"
MODS="${2:?mods, e.g. exfat}"
BASE="${3:-build/XDJ-RX_v221.zip}"
OUT="${4:-build/custom-upd}"
XDJ=crates/xdj-fw/target/release/xdj
[ -x "$XDJ" ] || ( cd crates/xdj-fw && cargo build --release -q )

rm -rf "$OUT"; mkdir -p "$OUT/overlay"
"$XDJ" fw unpack "$BASE" "$OUT/unpacked" >/dev/null
for m in ${MODS//,/ }; do
  [ -x "tools/fw/mods/$m/apply.sh" ] || { echo "unknown mod: $m" >&2; exit 1; }
  "tools/fw/mods/$m/apply.sh" "$OUT/unpacked" "$OUT/overlay"
done
"$XDJ" fw build "$BASE" --overlay "$OUT/overlay" --release "$REL" -o "$OUT/XDJRX.UPD"
"$XDJ" fw info "$OUT/XDJRX.UPD"
"$XDJ" fw stick "$OUT/XDJRX.UPD" "$OUT/stick.img"
