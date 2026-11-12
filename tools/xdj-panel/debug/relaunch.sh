#!/usr/bin/env bash
# Debug launcher for the XDJ-RX emulator (gdb + monitor/serial sockets + audio).
# Mirrors the proven prior-session config (single-core, snapshot drives, usb-hub
# cold, gdb on :1234). XDJ_AUDIO_DEBUG=1 prints per-channel SDMA peaks.
set -euo pipefail
cd /Users/thomas/Documents/XDJ-RX
Q=build/qemu
QEMU=build/qemu-src/qemu-11.1.0/build/qemu-system-arm

# Phase 1 control sockets (opt-in). Set XDJ_RUNTIME_DIR to push panel controls over
# the QEMU chardevs (id=xdjctrl = sub-ucom snapshot, id=xdjkey = mainkey GPIO) per
# instance, instead of the global /tmp/subucom_ctrl + /tmp/mainkey files. UNSET =
# today's /tmp-file behavior (inj.py / sweep_*.py and the rest of the file-based
# debug suite keep working unchanged -- the chardev's presence is what switches the
# harness into socket mode, so we add it only when asked). The monitor/serial stay
# on their /tmp paths so mon.py/rsp.py and the panel screendump are unaffected.
CTRL_ARGS=()
if [ -n "${XDJ_RUNTIME_DIR:-}" ]; then
  mkdir -p "$XDJ_RUNTIME_DIR"
  CTRL_ARGS=(-chardev socket,id=xdjctrl,path="$XDJ_RUNTIME_DIR/ctrl.sock",server=on,wait=off
             -chardev socket,id=xdjkey,path="$XDJ_RUNTIME_DIR/key.sock",server=on,wait=off)
  echo "ctrl: xdjctrl=$XDJ_RUNTIME_DIR/ctrl.sock  xdjkey=$XDJ_RUNTIME_DIR/key.sock" >&2
fi

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
  ${CTRL_ARGS[@]+"${CTRL_ARGS[@]}"} \
  -monitor unix:/tmp/d_mon4.sock,server,nowait \
  -serial unix:/tmp/d_con4.sock,server,nowait -serial null \
  -gdb tcp::1234
