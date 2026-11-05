#!/usr/bin/env bash
#
# Launch the XDJ-RX emulator: a live display window + the serial console.
#
# Boots the unmodified firmware as its real board -- Freescale i.MX6 SABRE-SD
# (MACH_TYPE 3980) -- which registers ECSPI2 (SPI bus 1) so the front-panel
# microcontroller ("Subucom") binds and /dev/subucom_spi1.0 exists.
#
# Board specifics handled on the QEMU side (firmware untouched; all via the
# bootloader-style cmdline/atags and the patched machine, qemu/patches/):
#   - ATAG_REVISION=0x500 (system_rev) so the board registers the subucom + AK4387
#     spi_board_info (see qemu_prepare.py).
#   - console on ttymxc0 (sabresd's early-console UART) -> serial 0 here.
#   - video=mxcfb0:dev=ldb (SABRE-SD panel is LVDS) so /dev/fb0 comes up.
#   - the nand image is attached to uSDHC3 via -drive id=xdjsd (patch 03).
#
# The boot splash renders at 32bpp, then the app switches the framebuffer to
# 16bpp RGB565 and the main deck UI comes up. The QEMU scanout
# (hw/arm/fsl-imx6.c) reads the live IPU display-channel stride and decodes
# whichever depth is active, so both render with no firmware changes.
#
# Single-core by default (fast; XDJSMP=2 for the real dual-core config, slower
# under TCG). The window is resizable (aspect locked 800:480) -- drag a corner.
# SUBUCOM_TRACE=1 logs the panel-MCU SPI frames to stderr (for panel reversing).
set -euo pipefail
cd "$(dirname "$0")/.."
Q=build/qemu
QEMU="${QEMU:-$(ls build/qemu-src/*/build/qemu-system-arm 2>/dev/null | head -1 || true)}"
QEMU="${QEMU:-qemu-system-arm}"
SD="${1:-$Q/nand.img}"
for f in vmlinux.bin tramp.bin atags.bin; do
  [ -f "$Q/$f" ] || { echo "missing $Q/$f — run: python3 qemu_prepare.py" >&2; exit 1; }
done
[ -f "$SD" ] || { echo "no nand image: $SD  (run qemu/build-nand.sh)" >&2; exit 1; }
SMP="${XDJSMP:-1}"            # single-core (fast) by default; XDJSMP=2 for dual-core
DISPLAY_OPT="${XDJDISPLAY:-cocoa,zoom-to-fit=on,zoom-interpolation=on}"
# Network backend attached to the i.MX6 FEC (eth0). The firmware runs a DHCP
# client on eth0 at boot (see qemu/build-nand.sh). Default: user-mode NAT -- it
# always works, needs no root, and the UI window opens as you (eth0 still gets a
# lease, just on a private NAT, not your LAN). Opt into bridged LAN so a rekordbox
# PC on the same LAN can reach the device for library LINK:
#     XDJNIC=bridged sudo qemu/watch.sh    # bridge to your default-route interface
# macOS vmnet needs root for an unsigned qemu (run.sh elevates this for you).
# XDJNIC may also be a literal qemu nic spec (e.g. vmnet-bridged,ifname=en1 for
# Wi-Fi, or a tap into a bridge on Linux). rekordbox LINK is then enabled from the
# app's LINK menu (needs the panel buttons -- feature C).
case "${XDJNIC:-user}" in
  user)    NIC="user" ;;
  bridged)
    IFN="$(route -n get default 2>/dev/null | awk '/interface:/{print $2; exit}')"
    NIC="vmnet-bridged,ifname=${IFN:-en0}"
    echo "net: bridging eth0 to host ${IFN:-en0} (vmnet) — device will be on your LAN" >&2 ;;
  *)       NIC="$XDJNIC" ;;
esac
if [ "$NIC" != "user" ] && [ "$(uname)" = "Darwin" ] && [ "$(id -u)" != "0" ]; then
  echo "net: '$NIC' needs root on macOS (vmnet) — re-run with sudo, or unset XDJNIC for user-mode NAT" >&2
fi

# Optional USB mass-storage (a rekordbox-formatted disk image). Cold-plug only:
# the EHCI hotplug path leaves the port connected-but-not-enabled, so the stick
# must be present at boot to enumerate. Enable with XDJUSB=path/to/image.img.
# NOTE: rbp currently crashes rendering rekordbox USB content (stubbed GPU 2D
# path) -- see docs/emulator-status.md feature D -- so this is for bring-up, not
# yet a working browse.
USB_ARGS=()
if [ -n "${XDJUSB:-}" ]; then
  [ -f "$XDJUSB" ] || { echo "XDJUSB=$XDJUSB not found" >&2; exit 1; }
  USB_ARGS=(-drive if=none,id=usbstick,file="$XDJUSB",format=raw
            -device usb-storage,bus=usb-bus.0,drive=usbstick,id=xdjusb)
  echo "usb: attaching $XDJUSB as usb-storage on usb-bus.0 (cold-plug)" >&2
fi

# sabresd console is ttymxc0 = serial 0 -> wire it to this terminal (Ctrl-A X quits).
exec "$QEMU" -M sabrelite -smp "$SMP" -m 256 -no-reboot \
  -name "Pioneer XDJ-RX" \
  -display "$DISPLAY_OPT" \
  -device loader,file="$Q/vmlinux.bin",addr=0x10008000,force-raw=on \
  -device loader,file="$Q/atags.bin",addr=0x10000100,force-raw=on \
  -device loader,file="$Q/tramp.bin",addr=0x10000000,cpu-num=0 \
  -drive if=none,id=xdjsd,file="$SD",format=raw \
  -nic "$NIC" \
  ${USB_ARGS[@]+"${USB_ARGS[@]}"} \
  -serial mon:stdio -serial null
