#!/usr/bin/env bash
#
# Boot the XDJ-RX firmware as its REAL board: Freescale i.MX6 SABRE-SD
# (MACH_TYPE 3980), the board init the firmware actually uses. Unlike the
# sabrelite path (qemu/watch.sh), this registers ECSPI2 (SPI bus 1), so the
# front-panel microcontroller binds and /dev/subucom_spi1.0 exists.
#
# What this verifies (from the root shell, login: root, no password):
#   - Machine: Freescale i.MX 6Quad/DualLite/Solo Sabre-SD Board
#   - subucom_spi spi1.0: SPI device initialized   (the panel MCU)
#   - ls -l /dev/subucom_spi1.0                     (char dev 153,0)
#   - the SD rootfs on uSDHC3 (Linux mmc0), /root/gui mounted
#
# Board differences handled on the QEMU side (firmware is unmodified, no kernel
# patches - all via the bootloader-style cmdline/atags and the machine):
#   - ATAG_REVISION=0x500 (system_rev) so xdj_spi_device_init registers the
#     subucom + AK4387 board_info (see qemu_prepare.py).
#   - console on ttymxc0 (sabresd's early console UART) -> serial 0 here.
#   - video=mxcfb0:dev=ldb (SABRE-SD wires the panel via LVDS, not parallel
#     "lcd"), so /dev/fb0 comes up and the app can draw.
#   - the SD image is attached to uSDHC3 via -drive if=none,id=xdjsd (the SABRE-SD
#     rootfs controller), by the patched machine (qemu/patches/03-...).
#
# Boots single-core by default (XDJSMP=2 for the real dual-core config). rbp is
# heavily multi-threaded; QEMU emulates two Cortex-A9 cores (with SCU cache
# coherency + atomics) far slower than one, so the UI comes up much faster on a
# single core. apl_start normally runs /root/pioneer/rbp only when it sees a
# second CPU (else rbp_solo, which the firmware does not ship) -- build-sd.sh adds
# rbp_solo as a symlink to rbp so the single-core path launches the app too.
# The console reaches "#### Application Start!! ###" and execs it either way.
#
# Current state: boots to the live UI. The boot splash (Pioneer DJ / rekordbox +
# progress bar) renders at 32bpp, then the app switches the framebuffer to 16bpp
# RGB565 and the main screen (DECK1/DECK2, rekordbox, BEAT EFFECTS, BPM/TEMPO)
# comes up. The QEMU-side scanout (hw/arm/fsl-imx6.c) reads the live line stride
# from the IPU display channel's CPMEM and decodes whichever depth is active, so
# both the splash and the UI render correctly -- no firmware/kernel changes.
#
# DirectFB's GAL (Vivante GPU) graphics core still needs /dev/galcore, which this
# model does not provide yet; the default image here (sd-nogal.img) has that one
# gfxdriver removed so DirectFB falls back to software rendering (the drawing is
# all DirectFB software anyway -- the GPU is only used for an unreached g2d_blit).
# Running the fully stock firmware needs a minimal galcore/Vivante stub (next).
#
# The window is resizable (zoom-to-fit, aspect locked 800:480) -- drag a corner.
# SUBUCOM_TRACE=1 logs the driver's SPI frames to stderr (for panel reversing).
set -euo pipefail
cd "$(dirname "$0")/.."
Q=build/qemu
QEMU="${QEMU:-$(ls build/qemu-src/*/build/qemu-system-arm 2>/dev/null | head -1 || true)}"
QEMU="${QEMU:-qemu-system-arm}"
# Default to the UI-rendering image (sd-nogal.img) if present, else sd.img.
if [ -n "${1:-}" ]; then SD="$1"; elif [ -f "$Q/sd-nogal.img" ]; then SD="$Q/sd-nogal.img"; else SD="$Q/sd.img"; fi
for f in vmlinux.bin tramp-sabresd.bin atags-sabresd.bin; do
  [ -f "$Q/$f" ] || { echo "missing $Q/$f — run: python3 qemu_prepare.py" >&2; exit 1; }
done
[ -f "$SD" ] || { echo "no SD image: $SD  (run qemu/build-sd.sh)"; exit 1; }
SMP="${XDJSMP:-1}"   # single-core (fast) by default; XDJSMP=2 for the real dual-core

# sabresd console is ttymxc0 = serial 0 -> wire it to this terminal (Ctrl-A X quits).
exec "$QEMU" -M sabrelite -smp "$SMP" -m 256 -no-reboot \
  -name "Pioneer XDJ-RX (SABRE-SD)" \
  -display cocoa,zoom-to-fit=on,zoom-interpolation=on \
  -device loader,file="$Q/vmlinux.bin",addr=0x10008000,force-raw=on \
  -device loader,file="$Q/atags-sabresd.bin",addr=0x10000100,force-raw=on \
  -device loader,file="$Q/tramp-sabresd.bin",addr=0x10000000,cpu-num=0 \
  -drive if=none,id=xdjsd,file="$SD",format=raw \
  -serial mon:stdio -serial null
