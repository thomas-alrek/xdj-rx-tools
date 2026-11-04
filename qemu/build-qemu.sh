#!/usr/bin/env bash
#
# Build a QEMU that boots the UNMODIFIED XDJ-RX firmware all the way to
# userspace. Two small i.MX6 patches are applied to QEMU (not the firmware):
#
#   patches/01-imx6_src-gpu-reset.patch
#       The i.MX6 SRC model clears the CPU/IPU software-reset bits but not the
#       GPU ones. galcore resets the GPU via SRC_SCR bit 1/4 and polls until
#       they self-clear, so it spun forever. Make those bits self-clearing.
#
#   patches/02-fsl-imx6-gpu-stub.patch
#       Two additions to the i.MX6 machine:
#       (a) Vivante GPU register stub at 0x00130000 — reports the GPU idle,
#           echoes the clock-control status bits (16/17) that _ResetGPU() waits
#           for, and reports a GC2000. galcore's reset then completes, its probe
#           fails gracefully (no real GPU), and the kernel boots on to its init.
#       (b) Framebuffer scanout console — mirrors the mxc_sdc_fb memory
#           (480x800 ARGB8888 at 0x18000000) to a QEMU graphic console, so the
#           display is capturable (monitor 'screendump') and showable (-display).
#
# Result: the kernel reaches "Freeing init memory" and runs the firmware's
# embedded initramfs /init (see build/qemu/boot-userspace-2.21.log).
#
# Usage:  qemu/build-qemu.sh      # -> build/qemu-src/qemu-<ver>/build/qemu-system-arm
# Then:   ./qemu_run.sh           # auto-detects the patched binary
set -euo pipefail
cd "$(dirname "$0")/.."
VER=11.1.0
SRC=build/qemu-src
PATCHES="$PWD/qemu/patches"
export PATH="/opt/homebrew/bin:$PATH"

command -v meson ninja pkg-config >/dev/null 2>&1 || {
  echo "Installing build deps (meson ninja pkg-config)…"; brew install meson ninja pkg-config; }

mkdir -p "$SRC"; cd "$SRC"
[ -f "qemu-$VER.tar.xz" ] || curl -fSL -o "qemu-$VER.tar.xz" "https://download.qemu.org/qemu-$VER.tar.xz"
[ -d "qemu-$VER" ] || tar xf "qemu-$VER.tar.xz"
cd "qemu-$VER"

for p in "$PATCHES"/*.patch; do
  if patch -p1 --dry-run -R -f <"$p" >/dev/null 2>&1; then
    echo "  already applied: $(basename "$p")"
  else
    echo "  applying: $(basename "$p")"; patch -p1 <"$p"
  fi
done

mkdir -p build && cd build
[ -f build.ninja ] || ../configure --target-list=arm-softmmu --disable-werror --disable-docs --disable-tools
ninja qemu-system-arm
echo
echo "built: $(pwd)/qemu-system-arm"
echo "run:   ./qemu_run.sh"
