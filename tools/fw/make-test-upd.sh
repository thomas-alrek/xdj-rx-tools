#!/usr/bin/env bash
# Build TEST.UPD: the official firmware + console markers in two rebuilt payloads + a new
# release number -- the end-to-end check of the custom-firmware pipeline (`xdj fw build`).
#
#   tools/fw/make-test-upd.sh [BASE.UPD|.zip] [RELEASE] [OUTDIR]
#     defaults: build/XDJ-RX_v221.zip  2.30  build/test-upd
#
# Writes OUTDIR/TEST.UPD and OUTDIR/stick.img (FAT16, XDJRX.UPD). Install it like an
# official update: boot the emulator in firmware-update mode with stick.img on USB1 (in
# the app: File > Enter Firmware Update Mode, power on, USB1 LOAD the stick). Expect,
# after the update + a power cycle:
#   - UTILITY -> VERSION No. shows RELEASE
#   - console: "### xdj-fw TEST: rootfs.cramfs rebuilt (rcS) ###"     (mtd5 payload)
#   - console: "### xdj-fw TEST: pioneer.tar.gz rebuilt (apl_start) ###" (mtd7 payload)
# The overlay is generated from the unpacked firmware at build time, so no
# Pioneer-owned file is ever committed.
set -euo pipefail
cd "$(dirname "$0")/../.."
BASE="${1:-build/XDJ-RX_v221.zip}"
REL="${2:-2.30}"
OUT="${3:-build/test-upd}"
XDJ=crates/xdj-fw/target/release/xdj
[ -x "$XDJ" ] || ( cd crates/xdj-fw && cargo build --release -q )

rm -rf "$OUT"; mkdir -p "$OUT"
"$XDJ" fw unpack "$BASE" "$OUT/unpacked" >/dev/null

OV="$OUT/overlay"
mkdir -p "$OV/rootfs/etc/rc.d" "$OV/pioneer/pioneer"
# rootfs.cramfs: a line at the end of rcS (sysinit)
cp "$OUT/unpacked/rootfs/etc/rc.d/rcS" "$OV/rootfs/etc/rc.d/rcS"
printf '\necho "### xdj-fw TEST: rootfs.cramfs rebuilt (rcS) ###"\n' >> "$OV/rootfs/etc/rc.d/rcS"
# pioneer.tar.gz: a line right after apl_start's "Application Start" banner
awk '{print} /^echo "#### Application Start!! ###"/{print "echo \"### xdj-fw TEST: pioneer.tar.gz rebuilt (apl_start) ###\""}' \
  "$OUT/unpacked/pioneer/pioneer/apl_start" > "$OV/pioneer/pioneer/apl_start"
grep -q "xdj-fw TEST" "$OV/pioneer/pioneer/apl_start" || { echo "apl_start banner not found" >&2; exit 1; }

"$XDJ" fw build "$BASE" --overlay "$OV" --release "$REL" -o "$OUT/TEST.UPD"
"$XDJ" fw info "$OUT/TEST.UPD"
"$XDJ" fw stick "$OUT/TEST.UPD" "$OUT/stick.img"
