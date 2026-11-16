#!/usr/bin/env bash
# Make a blank USB stick image (MBR + one FAT32 partition) holding the given files at its
# root - no rekordbox library; browse it with BROWSE > [FOLDER]. macOS (diskutil/hdiutil).
#
#   tools/make-fat32-image.sh [-s SIZE] OUT.img FILE...      SIZE default 256m (mkfile syntax)
#   e.g. tools/make-fat32-image.sh test/usb/wav_samples.img ~/Downloads/*.wav
set -euo pipefail
SIZE=256m
if [ "${1:-}" = -s ]; then SIZE="$2"; shift 2; fi
OUT="${1:?OUT.img}"; shift
[ $# -gt 0 ] || { echo "no files given" >&2; exit 1; }
[ "$(uname)" = Darwin ] || { echo "macOS only (diskutil/hdiutil)" >&2; exit 1; }

DEV=""
cleanup() { [ -n "$DEV" ] && hdiutil detach "$DEV" >/dev/null 2>&1 || true; }
trap cleanup EXIT
rm -f "$OUT"; mkfile -n "$SIZE" "$OUT"
DEV=$(hdiutil attach -imagekey diskimage-class=CRawDiskImage -nomount "$OUT" 2>/dev/null | awk 'NR==1{print $1}')
diskutil partitionDisk "$DEV" MBR FAT32 XDJTEST 100% >/dev/null
VOL=$(diskutil info "${DEV}s1" | awk -F': *' '/Mount Point/{print $2}')
touch "$VOL/.metadata_never_index"
for f in "$@"; do cp -X "$f" "$VOL/"; done
ls -l "$VOL"
echo "$OUT: FAT32, $# file(s) at the root"
