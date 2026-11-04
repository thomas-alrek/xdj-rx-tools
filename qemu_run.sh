#!/usr/bin/env bash
# Boot the UNMODIFIED XDJ-RX firmware kernel in QEMU.
#
# Prereqs (once):
#   python3 xdjrx_upd.py decrypt XDJRX.UPD -o build/XDJRX_2.21.iso
#   python3 qemu_prepare.py
#
# The trampoline supplies machine-id 3769 + ATAGs so the Freescale 3.0.35
# ATAG kernel accepts QEMU's 'sabrelite' board. Console is kept on the early
# polled console (ttymxc1 / UART2) because that's the UART QEMU exposes and the
# full imx-uart driver's TX doesn't reach QEMU's chardev. Nothing in the
# firmware is modified — the kernel image is loaded byte-for-byte.
#
# Usage:
#   ./qemu_run.sh                 # interactive console on your terminal (Ctrl-A X to quit)
#   ./qemu_run.sh | tee boot.log  # capture the boot log
set -euo pipefail
cd "$(dirname "$0")"
Q=build/qemu
for f in vmlinux.bin atags.bin tramp.bin; do
  [ -f "$Q/$f" ] || { echo "missing $Q/$f — run: python3 qemu_prepare.py" >&2; exit 1; }
done

# Use the patched QEMU (GPU-reset fix, see qemu/README.md) if present, else the
# system one. Override with:  QEMU=/path/to/qemu-system-arm ./qemu_run.sh
QEMU="${QEMU:-$(ls build/qemu-src/*/build/qemu-system-arm 2>/dev/null | head -1 || true)}"
QEMU="${QEMU:-qemu-system-arm}"

# Attach the SD rootfs if built (qemu/build-sd.sh). Without it the kernel boots
# to its embedded /init and stops at the root mount; with it, you get a shell.
SD=()
if [ -f "$Q/sd.img" ]; then
  SD=(-drive file="$Q/sd.img",format=raw,id=mysd,if=none -device sd-card,drive=mysd)
fi

# Console is on the 2nd serial (ttymxc1); wire it to your terminal. Ctrl-A X quits.
exec "$QEMU" -M sabrelite -smp 1 -m 1024 -no-reboot -name "Pioneer XDJ-RX" -display none \
  -device loader,file="$Q/vmlinux.bin",addr=0x10008000,force-raw=on \
  -device loader,file="$Q/atags.bin",addr=0x10000100,force-raw=on \
  -device loader,file="$Q/tramp.bin",addr=0x10000000,cpu-num=0 \
  "${SD[@]}" \
  -serial null -serial mon:stdio "$@"
