#!/usr/bin/env bash
# preload mod: the xdjmod framework (libxdjmod.so) loaded into the stock rbp. Feature
# mods build on it: their apply.sh calls `apply.sh UNPACKED OVERLAY their.c`, which
# adds the source to this build's module list and rebuilds the library with every
# module listed so far. rbp itself is never modified.
#
#   tools/fw/mods/preload/apply.sh UNPACKED OVERLAY [FEATURE.c ...]
#
# Adds to the pioneer overlay (mtd7):
#   pioneer/libxdjmod.so          src/ (core) + the listed feature modules
#   pioneer/apl_start             the stock script with LD_PRELOAD on both rbp launch
#                                 lines (rbp, rbp_solo)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
UNP="$1"; OV="$2"; shift 2
WORK="$(cd "$OV/.." && pwd)/xdjmod"           # build state next to the overlay
mkdir -p "$WORK"; touch "$WORK/features.txt"
for f in "$@"; do
  f="$(cd "$(dirname "$f")" && pwd)/$(basename "$f")"
  grep -qxF "$f" "$WORK/features.txt" || echo "$f" >> "$WORK/features.txt"
done
FEATURES=()
while IFS= read -r f; do [ -n "$f" ] && FEATURES+=("$f"); done < "$WORK/features.txt"
"$HERE/build.sh" "$WORK" "$UNP/rootfs/lib" ${FEATURES[@]+"${FEATURES[@]}"} | tail -1

mkdir -p "$OV/pioneer/pioneer"
install -m 0755 "$WORK/libxdjmod.so" "$OV/pioneer/pioneer/libxdjmod.so"
SRC="$UNP/pioneer/pioneer/apl_start"
[ -f "$OV/pioneer/pioneer/apl_start" ] && SRC="$OV/pioneer/pioneer/apl_start"
sed -E 's#^([[:space:]]*)(/root/pioneer/rbp(_solo)? &)#\1LD_PRELOAD=/root/pioneer/libxdjmod.so \2#' \
  "$SRC" > "$OV/pioneer/pioneer/apl_start.new"
[ "$(grep -c 'LD_PRELOAD=/root/pioneer/libxdjmod.so' "$OV/pioneer/pioneer/apl_start.new")" = 2 ] \
  || { echo "preload: rbp launch lines not found in apl_start" >&2; exit 1; }
mv "$OV/pioneer/pioneer/apl_start.new" "$OV/pioneer/pioneer/apl_start"
echo "preload: libxdjmod.so + LD_PRELOAD in apl_start"
