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
# The Pioneer DJ / rekordbox splash renders here. Past the splash the app needs
# the Subucom panel-MCU handshake (ui::panel_protocol StaticInfomation); the SSI
# slave stub currently answers 0xff, so that handshake is still being reversed.
#
# SUBUCOM_TRACE=1 logs the driver's SPI frames to stderr (for that reversing).
set -euo pipefail
cd "$(dirname "$0")/.."
Q=build/qemu
QEMU="${QEMU:-$(ls build/qemu-src/*/build/qemu-system-arm 2>/dev/null | head -1 || true)}"
QEMU="${QEMU:-qemu-system-arm}"
SD="${1:-$Q/sd.img}"
for f in vmlinux.bin tramp-sabresd.bin atags-sabresd.bin; do
  [ -f "$Q/$f" ] || { echo "missing $Q/$f — run: python3 qemu_prepare.py" >&2; exit 1; }
done
[ -f "$SD" ] || { echo "no SD image: $SD  (run qemu/build-sd.sh)"; exit 1; }

# sabresd console is ttymxc0 = serial 0 -> wire it to this terminal (Ctrl-A X quits).
exec "$QEMU" -M sabrelite -smp 1 -m 1024 -no-reboot \
  -name "Pioneer XDJ-RX (SABRE-SD)" \
  -display cocoa \
  -device loader,file="$Q/vmlinux.bin",addr=0x10008000,force-raw=on \
  -device loader,file="$Q/atags-sabresd.bin",addr=0x10000100,force-raw=on \
  -device loader,file="$Q/tramp-sabresd.bin",addr=0x10000000,cpu-num=0 \
  -drive if=none,id=xdjsd,file="$SD",format=raw \
  -serial mon:stdio -serial null
