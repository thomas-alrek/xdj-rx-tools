#!/usr/bin/env bash
# Debug launcher for the XDJ-RX emulator (gdb + monitor/serial sockets + audio).
# Mirrors the proven prior-session config (single-core, snapshot drives, usb-hub
# cold, gdb on :1234). XDJ_AUDIO_DEBUG=1 prints per-channel SDMA peaks.
set -euo pipefail
cd /Users/thomas/Documents/XDJ-RX
Q=build/qemu
QEMU=build/qemu-src/qemu-11.1.0/build/qemu-system-arm
exec env XDJ_AUDIO_DEBUG=1 "$QEMU" \
  -M sabrelite -smp 1 -m 256 -no-reboot \
  -name "Pioneer XDJ-RX (inject/gdb)" \
  -display cocoa,zoom-to-fit=on \
  -audio coreaudio,timer-period=1451 \
  -drive if=none,id=usbstick,file=test/usb/rekordbox_new.img,format=raw,snapshot=on \
  -device usb-hub,bus=usb-bus.1,port=1,id=xdjhub \
  -device loader,file="$Q/vmlinux.bin",addr=0x10008000,force-raw=on \
  -device loader,file="$Q/atags.bin",addr=0x10000100,force-raw=on \
  -device loader,file="$Q/tramp.bin",addr=0x10000000,cpu-num=0 \
  -drive if=none,id=xdjsd,file="$Q/nand.img",format=raw,snapshot=on \
  -nic user \
  -monitor unix:/tmp/d_mon4.sock,server,nowait \
  -serial unix:/tmp/d_con4.sock,server,nowait -serial null \
  -gdb tcp::1234
