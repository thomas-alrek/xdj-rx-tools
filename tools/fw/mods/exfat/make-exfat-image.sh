#!/usr/bin/env bash
# Make an exFAT copy of a rekordbox USB test image (macOS): same size, an MBR with one
# exFAT partition, the same files (no macOS metadata copied). For testing the exFAT
# mod - stock firmware only mounts FAT32 and HFS+.
#
#   tools/fw/mods/exfat/make-exfat-image.sh [SRC.img] [OUT.img]
#     defaults: test/usb/rekordbox_new.img  test/usb/rekordbox_new_exfat.img
set -euo pipefail
cd "$(dirname "$0")/../../../.."
SRC="${1:-test/usb/rekordbox_new.img}"
OUT="${2:-test/usb/rekordbox_new_exfat.img}"
[ "$(uname)" = Darwin ] || { echo "macOS only (diskutil/hdiutil)" >&2; exit 1; }

SIZE=$(stat -f %z "$SRC")
MNT="$(mktemp -d)"; SRC_DEV=""; OUT_DEV=""
cleanup() {
  [ -n "$SRC_DEV" ] && hdiutil detach "$SRC_DEV" >/dev/null 2>&1 || true
  [ -n "$OUT_DEV" ] && hdiutil detach "$OUT_DEV" >/dev/null 2>&1 || true
  rmdir "$MNT" 2>/dev/null || true
}
trap cleanup EXIT

rm -f "$OUT"; mkfile -n "$SIZE" "$OUT"
OUT_DEV=$(hdiutil attach -imagekey diskimage-class=CRawDiskImage -nomount "$OUT" 2>/dev/null | awk 'NR==1{print $1}')
diskutil partitionDisk "$OUT_DEV" MBR ExFAT RBEXFAT 100% >/dev/null
VOL=$(diskutil info "${OUT_DEV}s1" | awk -F': *' '/Mount Point/{print $2}')
touch "$VOL/.metadata_never_index"

SRC_DEV=$(hdiutil attach -readonly -nobrowse -imagekey diskimage-class=CRawDiskImage \
  -mountpoint "$MNT" "$SRC" 2>/dev/null | awk 'NR==1{print $1}')
for d in "$MNT"/*; do cp -RX "$d" "$VOL/"; done
for d in "$MNT"/*; do diff -r "$d" "$VOL/$(basename "$d")"; done
echo "$OUT: exFAT copy of $SRC ($(find "$MNT" -type f | wc -l | tr -d ' ') files, identical)"
