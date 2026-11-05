#!/usr/bin/env bash
#
# run.sh — one command from nothing to the running XDJ-RX emulator:
#
#   download firmware → resolve the AES key → decrypt → prepare boot blobs →
#   build the patched QEMU → build nand.img → launch.
#
# Idempotent: every step is skipped when its output already exists, so re-running
# just relaunches. Needs: python3, curl, docker (for the nand image), and a C
# toolchain + Homebrew (build-qemu.sh pulls meson/ninja). macOS or Linux.
#
#   ./run.sh                 # do everything, then launch
#   ./run.sh path/to.img     # launch with a specific nand image
#
set -euo pipefail
cd "$(dirname "$0")"

FW_URL="https://downloads.support.alphatheta.com/firmwares/all-in-one-dj-systems/XDJ-RX/XDJ-RX_v221.zip"
FW_ZIP="build/XDJ-RX_v221.zip"
ISO="build/XDJRX.iso"
Q="build/qemu"
mkdir -p build

# --- Python runner: uv (auto-installs the scripts' inline deps) if available,
#     otherwise a self-contained venv under build/ (git-ignored). --------------
if command -v uv >/dev/null 2>&1; then
  PY() { uv run "$@"; }
else
  VENV="build/.venv"
  if [ ! -x "$VENV/bin/python3" ]; then
    echo "==> creating python venv ($VENV) with firmware-tool deps"
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install -q --upgrade pip
    "$VENV/bin/pip" install -q pycdlib pycryptodome inflate64
  fi
  PY() { "$VENV/bin/python3" "$@"; }
fi

# 1. Firmware update package (encrypted .UPD inside a .zip)
[ -f "$FW_ZIP" ] || { echo "==> downloading firmware ($FW_URL)"; curl -fSL -o "$FW_ZIP" "$FW_URL"; }

# 2. AES-256 key -> .env.  Uses ./aes256.key if present, else pulls it from
#    Pioneer's own published GPL source (~280 MB, cached). Nothing secret is committed.
[ -f .env ] || { echo "==> resolving firmware AES key"; PY fetch_key.py; }

# 3. Decrypt the cryptoloop firmware -> ISO9660 image
[ -f "$ISO" ] || { echo "==> decrypting firmware -> $ISO"; PY xdjrx_upd.py decrypt "$FW_ZIP" -o "$ISO"; }

# 4. Extract/decompress the kernel and build the SABRE-SD boot blobs
[ -f "$Q/vmlinux.bin" ] || { echo "==> preparing boot blobs"; PY qemu_prepare.py "$ISO"; }

# 5. Build the patched QEMU (GPU stub + framebuffer scanout + SABRE-SD board)
ls "$Q"/../qemu-src/*/build/qemu-system-arm >/dev/null 2>&1 || { echo "==> building QEMU"; qemu/build-qemu.sh; }

# 6. Build the nand image from the firmware's own rootfs payloads (needs docker)
[ -f "$Q/nand.img" ] || { echo "==> building nand.img"; qemu/build-nand.sh; }

# 7. Launch
echo "==> launching emulator (close the window or Ctrl-A X to quit)"
exec qemu/watch.sh "$@"
