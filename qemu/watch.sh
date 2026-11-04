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
#     Ctrl-A X quits). The app renders the Pioneer DJ / rekordbox splash; it then
#     waits on the (not-yet-emulated) front-panel MCU before the main screen.
#
# Login at "freescale login:" is root (no password) on the test-harness SD.
set -euo pipefail
cd "$(dirname "$0")/.."
Q=build/qemu
QEMU="${QEMU:-$(ls build/qemu-src/*/build/qemu-system-arm 2>/dev/null | head -1 || true)}"
QEMU="${QEMU:-qemu-system-arm}"
SD="${1:-$Q/sd.img}"
[ -f "$SD" ] || { echo "no SD image: $SD  (run qemu/build-sd.sh)"; exit 1; }

exec "$QEMU" -M sabrelite -smp 1 -m 1024 -no-reboot \
  -name "Pioneer XDJ-RX" \
  -display cocoa \
  -device loader,file="$Q/vmlinux.bin",addr=0x10008000,force-raw=on \
  -device loader,file="$Q/atags.bin",addr=0x10000100,force-raw=on \
  -device loader,file="$Q/tramp.bin",addr=0x10000000,cpu-num=0 \
  -drive file="$SD",format=raw,id=mysd,if=none -device sd-card,drive=mysd \
  -serial null -serial mon:stdio
