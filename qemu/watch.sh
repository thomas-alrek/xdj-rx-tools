#!/usr/bin/env bash
#
# Watch the XDJ-RX emulator LIVE: a 480x800 display window + the serial console
# in this terminal. Run this in your OWN terminal — QEMU runs natively on macOS
# (Docker is only used to build the SD image), so it opens a real window here.
#
#   qemu/watch.sh                     # build/qemu/sd.img (built by build-sd.sh)
#   qemu/watch.sh path/to/other.img   # a specific SD image
#
# In the window/terminal:
#   - the Cocoa window shows /dev/fb0 (the firmware's framebuffer)
#   - this terminal is the serial console AND the QEMU monitor (Ctrl-A C toggles;
#     Ctrl-A X quits).
#
# Boots single-core by default (XDJSMP=2 for the real dual-core config). rbp is
# heavily multi-threaded and QEMU emulates two Cortex-A9 cores (SCU cache
# coherency + atomics) far slower than one, so the UI comes up much faster on a
# single core. apl_start picks the app binary by core count -- /root/pioneer/rbp
# on >1 core, else /root/pioneer/rbp_solo (which the firmware does NOT ship);
# build-sd.sh adds rbp_solo as a symlink to rbp so the single-core path launches
# the app too. apl_start then reaches "#### Application Start!! ###" and execs it.
#
# This is the sabrelite path (parallel-LCD / dev=lcd). For the firmware's real
# board (LVDS panel + front-panel MCU) use qemu/watch-sabresd.sh, which boots to
# the live UI. The framebuffer scanout renders both the 32bpp boot splash and the
# 16bpp RGB565 UI (hw/arm/fsl-imx6.c picks the depth from the IPU display stride).
# With the fully stock image here, DirectFB's GAL (Vivante) graphics core still
# aborts init because it needs /dev/galcore, which this model does not provide
# yet; a minimal galcore/Vivante stub is the remaining piece for stock firmware.
#
# Login at "freescale login:" is root (no password) on the test-harness SD.
set -euo pipefail
cd "$(dirname "$0")/.."
Q=build/qemu
QEMU="${QEMU:-$(ls build/qemu-src/*/build/qemu-system-arm 2>/dev/null | head -1 || true)}"
QEMU="${QEMU:-qemu-system-arm}"
SD="${1:-$Q/sd.img}"
[ -f "$SD" ] || { echo "no SD image: $SD  (run qemu/build-sd.sh)"; exit 1; }

SMP="${XDJSMP:-1}"   # single-core (fast) by default; XDJSMP=2 for the real dual-core
exec "$QEMU" -M sabrelite -smp "$SMP" -m 1024 -no-reboot \
  -name "Pioneer XDJ-RX" \
  -display cocoa \
  -device loader,file="$Q/vmlinux.bin",addr=0x10008000,force-raw=on \
  -device loader,file="$Q/atags.bin",addr=0x10000100,force-raw=on \
  -device loader,file="$Q/tramp.bin",addr=0x10000000,cpu-num=0 \
  -drive file="$SD",format=raw,id=mysd,if=none -device sd-card,drive=mysd \
  -serial null -serial mon:stdio
