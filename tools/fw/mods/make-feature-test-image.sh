#!/usr/bin/env bash
# One test stick for every mod (macOS; needs baken - brew install M-Igashi/tap/baken): an
# exFAT image holding a real rekordbox export (export.pdb + analysis) of a playlist from a
# rekordbox XML - e.g. a "Test" playlist with an ordinary MP3, the FLAC test tones and the
# hi-res / float WAV samples.
#
#   tools/fw/mods/make-feature-test-image.sh XML [PLAYLIST] [OUT.img] [SRC.img ...]
#     defaults: PLAYLIST "Test", OUT test/usb/features_exfat.img,
#               SRC test/usb/flac_test.img test/usb/wav_samples.img
#
# baken copies each track's audio from its XML Location and its analysis from rekordbox's
# cache. Tracks imported into rekordbox from one of our test sticks point at
# /Volumes/XDJTEST/..., so the SRC images' files are staged on a temporary FAT32 volume
# mounted there (whatever is mounted at /Volumes/XDJTEST is ejected first). Tracks without
# a rekordbox analysis get a generated one (--generate-analysis); no My Settings are written.
set -euo pipefail
cd "$(dirname "$0")/../../.."
XML="${1:?rekordbox XML}"; shift
PLAYLIST="${1:-Test}"; [ $# -gt 0 ] && shift
OUT="${1:-test/usb/features_exfat.img}"; [ $# -gt 0 ] && shift
SRCS=("$@"); [ ${#SRCS[@]} -gt 0 ] || SRCS=(test/usb/flac_test.img test/usb/wav_samples.img)
command -v baken >/dev/null || { echo "needs baken: brew install M-Igashi/tap/baken" >&2; exit 1; }
[ "$(uname)" = Darwin ] || { echo "macOS only" >&2; exit 1; }

TMP="$(mktemp -d)"; DEVS=()
cleanup() { for d in ${DEVS[@]+"${DEVS[@]}"}; do hdiutil detach "$d" >/dev/null 2>&1 || true; done; rm -rf "$TMP"; }
trap cleanup EXIT
attach_raw() { hdiutil attach -imagekey diskimage-class=CRawDiskImage "$@" 2>/dev/null | awk 'NR==1{print $1}'; }

# 1. stage the source files at /Volumes/XDJTEST
if [ -d /Volumes/XDJTEST ]; then
  echo ">> ejecting the volume mounted at /Volumes/XDJTEST"
  diskutil eject "$(diskutil info /Volumes/XDJTEST | awk -F': *' '/Device Node/{print $2}' | sed 's/s[0-9]*$//')" >/dev/null
fi
mkdir -p "$TMP/files"
for img in "${SRCS[@]}"; do
  mkdir -p "$TMP/src"
  d=$(attach_raw -readonly -nobrowse -mountpoint "$TMP/src" "$img"); DEVS+=("$d")
  find "$TMP/src" -maxdepth 1 -type f \( -iname '*.flac' -o -iname '*.wav' -o -iname '*.mp3' \) \
    ! -name '._*' -exec cp -X {} "$TMP/files/" \;
  hdiutil detach "$d" >/dev/null; rmdir "$TMP/src"
done
tools/make-fat32-image.sh "$TMP/staging.img" "$TMP/files"/* | tail -1
d=$(attach_raw -nobrowse "$TMP/staging.img"); DEVS+=("$d")
[ -d /Volumes/XDJTEST ] || { echo "staging volume did not mount at /Volumes/XDJTEST" >&2; exit 1; }

# 2. the exFAT target
echo ">> blank exFAT image: $OUT"
rm -f "$OUT"; mkfile -n 1g "$OUT"
t=$(attach_raw -nomount "$OUT"); DEVS+=("$t")
diskutil partitionDisk "$t" MBR ExFAT XDJFEAT 100% >/dev/null
VOL=$(diskutil info "${t}s1" | awk -F': *' '/Mount Point/{print $2}')
touch "$VOL/.metadata_never_index"

# 3. the export
echo ">> baken expressport \"$PLAYLIST\" -> $VOL"
baken expressport "$XML" --device "$VOL" --playlist "$PLAYLIST" --no-settings --generate-analysis
find "$VOL" -type f ! -path '*/.*' | sed "s|$VOL/||" | sort
echo "$OUT: exFAT + rekordbox export of \"$PLAYLIST\""
