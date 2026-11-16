#!/usr/bin/env bash
#
# run.sh — one command from nothing to the running XDJ-RX emulator:
#
#   download firmware → resolve the AES key → decrypt → prepare boot blobs →
#   build the patched QEMU → build nand.img → launch.
#
# Idempotent: every step is skipped when its output already exists, so re-running
# just relaunches. Needs: python3, curl, e2fsprogs (for the nand image), and a C
# toolchain + Homebrew (build-qemu.sh pulls meson/ninja). macOS or Linux.
#
# Networking: the unit joins your LAN when the optional socket_vmnet bridge is
# installed (tools/net/install-vmnet-bridge.sh, one-time); otherwise it is on QEMU's
# private user-mode NAT. Either way the emulator runs as you (XDJNIC, qemu/watch.sh).
#
#   ./run.sh                 # build + launch the UI window as you (no sudo)
#
set -euo pipefail
cd "$(dirname "$0")"

FW_URL="https://downloads.support.alphatheta.com/firmwares/all-in-one-dj-systems/XDJ-RX/XDJ-RX_v221.zip"
FW_ZIP="build/XDJ-RX_v221.zip"
ISO="build/XDJRX.iso"
Q="build/qemu"

# Run a (build) command as the real user, even under `sudo ./run.sh`, so Homebrew
# works and build/ stays owned by you. Plain ./run.sh just runs it directly.
asuser() {
  if [ "$(id -u)" = 0 ] && [ -n "${SUDO_USER:-}" ]; then sudo -u "$SUDO_USER" -- "$@"; else "$@"; fi
}

asuser mkdir -p build

# --- Python runner: uv (auto-installs the scripts' inline deps) if available,
#     otherwise a self-contained venv under build/ (git-ignored). --------------
if command -v uv >/dev/null 2>&1; then
  PY() { asuser uv run "$@"; }
else
  VENV="build/.venv"
  if [ ! -x "$VENV/bin/python3" ]; then
    echo "==> creating python venv ($VENV) with firmware-tool deps"
    asuser python3 -m venv "$VENV"
    asuser "$VENV/bin/pip" install -q --upgrade pip
    asuser "$VENV/bin/pip" install -q -r tools/requirements.txt
  fi
  PY() { asuser "$VENV/bin/python3" "$@"; }
fi

# The firmware toolkit (`xdj` CLI, crates/xdj-fw): decrypt + boot blobs.
XDJ=crates/xdj-fw/target/release/xdj
[ -x "$XDJ" ] || { echo "==> building the xdj firmware tool"; ( cd crates/xdj-fw && asuser cargo build --release -q ); }

# 1. Firmware update package (encrypted .UPD inside a .zip)
[ -f "$FW_ZIP" ] || { echo "==> downloading firmware ($FW_URL)"; asuser curl -fSL -o "$FW_ZIP" "$FW_URL"; }

# 2. AES-256 key -> .env.  Uses ./aes256.key if present, else pulls it from
#    Pioneer's own published GPL source (~280 MB, cached). Nothing secret is committed.
[ -f .env ] || { echo "==> resolving firmware AES key"; PY tools/fetch_key.py; }

# 3. Decrypt the cryptoloop firmware -> ISO9660 image
[ -f "$ISO" ] || { echo "==> decrypting firmware -> $ISO"; asuser "$XDJ" fw decrypt "$FW_ZIP" -o "$ISO"; }

# 4. Extract/decompress the kernel and build the SABRE-SD boot blobs
[ -f "$Q/vmlinux.bin" ] || { echo "==> preparing boot blobs"; asuser "$XDJ" prepare "$ISO" --out "$Q"; }

# 5. Build the patched QEMU (GPU stub + framebuffer scanout + SABRE-SD board)
ls "$Q"/../qemu-src/*/build/qemu-system-arm >/dev/null 2>&1 || { echo "==> building QEMU"; asuser qemu/build-qemu.sh; }

# 6. Build the nand image from the firmware's own rootfs payloads (pure Python + e2fsprogs)
[ -f "$Q/nand.img" ] || { echo "==> building nand.img"; asuser bash qemu/build-nand.sh; }

# 7. Launch, as you.
echo "==> launching emulator — a window opens; boot to the UI takes ~60–90s"
echo "    (a black window is normal at first). Close the window or Ctrl-A X to quit."
exec qemu/watch.sh "$@"
