#!/bin/zsh
# Build a rekordbox USB *disk image* (FAT32/MBR) from a rekordbox Export
# Collection XML, using `baken expressport`, for testing in the QEMU XDJ-RX.
#
# Why: the XDJ-RX firmware (rbp) reads a genuine rekordbox device export
# (PIONEER/rekordbox/export.pdb + USBANLZ analysis). baken writes that format
# from a collection.xml WITHOUT opening rekordbox to write the stick. We target
# a raw image file (not a physical stick) so QEMU can use it directly.
#
# Prereqs:
#   - baken:   brew install M-Igashi/tap/baken
#   - XML:     in rekordbox, File -> Export Collection (xml)
#   - ffmpeg:  only for --cdjsafe (already present)
#
# Usage:
#   tools/build-rb-usb.sh <collection.xml> [playlist] [image.img] [size]
# Example:
#   tools/build-rb-usb.sh ~/Music/rekordbox/collection.xml "Sets/Test" \
#       test/usb/rekordbox_new.img 2g
#
# Extra baken flags can be passed via BAKEN_FLAGS, e.g.:
#   BAKEN_FLAGS="--cdjsafe --generate-analysis" tools/build-rb-usb.sh ...
set -euo pipefail
cd "$(dirname "$0")/.."

XML="${1:?collection.xml required}"
PLAYLIST="${2:-}"
IMG="${3:-test/usb/rekordbox_new.img}"
SIZE="${4:-2g}"
VOL="XDJTEST"

command -v baken >/dev/null || { echo "baken not found: brew install M-Igashi/tap/baken" >&2; exit 1; }
[ -f "$XML" ] || { echo "no such XML: $XML" >&2; exit 1; }

# convert e.g. 2g/512m -> MB for the sparse seek
case "$SIZE" in
  *g|*G) MB=$(( ${SIZE%[gG]} * 1024 ));;
  *m|*M) MB=${SIZE%[mM]};;
  *)     MB=$SIZE;;
esac

echo ">> creating blank $SIZE image: $IMG"
rm -f "$IMG"
dd if=/dev/zero of="$IMG" bs=1 count=0 seek=${MB}m 2>/dev/null

echo ">> attaching + formatting FAT32/MBR ($VOL)"
DEV=$(hdiutil attach -imagekey diskimage-class=CRawDiskImage -nomount "$IMG" | head -1 | awk '{print $1}')
trap 'diskutil eject "$DEV" >/dev/null 2>&1 || true' EXIT
diskutil eraseDisk FAT32 "$VOL" MBRFormat "$DEV" >/dev/null
[ -d "/Volumes/$VOL" ] || { echo "mount failed" >&2; exit 1; }

echo ">> baken expressport -> /Volumes/$VOL"
PL_ARGS=(); [ -n "$PLAYLIST" ] && PL_ARGS=(--playlist "$PLAYLIST")
# shellcheck disable=SC2086
baken expressport "$XML" --device "/Volumes/$VOL" "${PL_ARGS[@]}" ${BAKEN_FLAGS:-}

echo ">> contents written:"
find "/Volumes/$VOL/PIONEER" -maxdepth 3 -type f 2>/dev/null | sed "s|/Volumes/$VOL/||" | head -20

echo ">> ejecting (flushes image)"
diskutil eject "$DEV" >/dev/null; trap - EXIT
echo ">> done: $IMG  (point QEMU at it with XDJUSB=$IMG)"
