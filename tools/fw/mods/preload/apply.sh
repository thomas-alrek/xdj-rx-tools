#!/usr/bin/env bash
# preload mod: load libxdjmod.so into the stock rbp. The base for every rbp feature
# mod (they add their hooks to the library); rbp itself is never modified.
#
#   tools/fw/mods/preload/apply.sh UNPACKED OVERLAY
#
# Adds to the pioneer overlay (mtd7):
#   pioneer/libxdjmod.so          (build.sh)
#   pioneer/apl_start             the stock script with LD_PRELOAD on both rbp launch
#                                 lines (rbp, rbp_solo)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
UNP="$1"; OV="$2"
"$HERE/build.sh" >/dev/null
LIB="$(cd "$HERE/../../../.." && pwd)/build/mods/preload/libxdjmod.so"

mkdir -p "$OV/pioneer/pioneer"
install -m 0755 "$LIB" "$OV/pioneer/pioneer/libxdjmod.so"
SRC="$UNP/pioneer/pioneer/apl_start"
[ -f "$OV/pioneer/pioneer/apl_start" ] && SRC="$OV/pioneer/pioneer/apl_start"
sed -E 's#^([[:space:]]*)(/root/pioneer/rbp(_solo)? &)#\1LD_PRELOAD=/root/pioneer/libxdjmod.so \2#' \
  "$SRC" > "$OV/pioneer/pioneer/apl_start.new"
[ "$(grep -c 'LD_PRELOAD=/root/pioneer/libxdjmod.so' "$OV/pioneer/pioneer/apl_start.new")" = 2 ] \
  || { echo "preload: rbp launch lines not found in apl_start" >&2; exit 1; }
mv "$OV/pioneer/pioneer/apl_start.new" "$OV/pioneer/pioneer/apl_start"
echo "preload: libxdjmod.so + LD_PRELOAD in apl_start"
