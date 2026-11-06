#!/usr/bin/env bash
#
# Build build/qemu/nand.img -- the SD-card image that boots the XDJ-RX firmware
# to its on-screen UI in QEMU.
#
# This is now a thin wrapper around qemu/build-nand.py.  The old Docker recipe is
# gone: the build runs entirely on the host with Python + e2fsprogs (mke2fs +
# debugfs), no container and no root.  See build-nand.py for the full details.
#
#   Needs: python3 (+ pycdlib) and e2fsprogs.
#          macOS:  brew install e2fsprogs
#          Debian: apt-get install e2fsprogs
#
# Usage:  qemu/build-nand.sh        # -> build/qemu/nand.img   (then qemu/watch.sh)
set -euo pipefail
cd "$(dirname "$0")/.."

# Pick a Python that has the firmware-tool deps (pycdlib):
#   $PYTHON override > the repo venv run.sh builds > uv (resolves inline deps) > system.
if [ -n "${PYTHON:-}" ]; then
  exec "$PYTHON" qemu/build-nand.py "$@"
elif [ -x build/.venv/bin/python3 ]; then
  exec build/.venv/bin/python3 qemu/build-nand.py "$@"
elif command -v uv >/dev/null 2>&1; then
  exec uv run qemu/build-nand.py "$@"
else
  exec python3 qemu/build-nand.py "$@"
fi
