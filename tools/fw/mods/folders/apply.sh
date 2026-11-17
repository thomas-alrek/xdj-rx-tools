#!/usr/bin/env bash
# folders mod: [FOLDER] lists the Contents folder of a rekordbox stick - an xdjmod
# module (folders.c).
#   tools/fw/mods/folders/apply.sh UNPACKED OVERLAY
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
"$HERE/../preload/apply.sh" "$1" "$2" "$HERE/folders.c"
echo "folders: Contents in [FOLDER]"
