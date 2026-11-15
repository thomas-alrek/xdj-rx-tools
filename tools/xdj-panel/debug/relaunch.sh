#!/usr/bin/env bash
# Debug launcher for the XDJ-RX emulator (gdb + monitor/serial sockets + audio).
# Mirrors the proven prior-session config (single-core, snapshot drives, usb-hub
# cold, gdb on :1234). XDJ_AUDIO_DEBUG=1 prints per-channel SDMA peaks.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."   # repo root
Q=build/qemu
QEMU=build/qemu-src/qemu-11.1.0/build/qemu-system-arm
# Boot blobs (vmlinux/atags/tramp): XDJ_BOOT_DIR overrides build/qemu -- e.g. the output of
# `qemu/xdj_nand.py boot <nand.img> <dir>` for a NAND boot (docs/update-path-plan.md).
B="${XDJ_BOOT_DIR:-$Q}"
# SD card: build/qemu/nand.img (legacy SD test bed) unless XDJ_SD=none (a NAND unit has none).
SD_ARGS=(-drive if=none,id=xdjsd,file="$Q/nand.img",format=raw,snapshot=on)
[ "${XDJ_SD:-}" = "none" ] && SD_ARGS=()

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

# The serial console stays on /tmp/d_con4.sock (ser.py) and is also logged to
# /tmp/xdj_console.log, which the panel's View > System log tails.

# USB stick backend (id=usbstick) for the post-boot device_add -- only when the image exists.
USBIMG="${XDJUSB:-test/usb/rekordbox_new.img}"
USB_ARGS=()
if [ -f "$USBIMG" ]; then
  USB_ARGS=(-drive if=none,id=usbstick,file="$USBIMG",format=raw,snapshot=on)
else
  echo "relaunch: no USB image at $USBIMG -- booting without the usbstick backend" >&2
fi

exec env XDJ_AUDIO_DEBUG=1 "$QEMU" \
  -M sabrelite -smp "${XDJ_SMP:-1}" -m 256 -no-reboot \
  -name "Pioneer XDJ-RX (inject/gdb)" \
  -display cocoa,zoom-to-fit=on \
  -audio coreaudio,timer-period=1451 \
  ${USB_ARGS[@]+"${USB_ARGS[@]}"} \
  -device usb-hub,bus=usb-bus.1,port=1,id=xdjhub \
  -device loader,file="$B/vmlinux.bin",addr=0x10008000,force-raw=on \
  -device loader,file="$B/atags.bin",addr=0x10000100,force-raw=on \
  -device loader,file="$B/tramp.bin",addr=0x10000000,cpu-num=0 \
  ${SD_ARGS[@]+"${SD_ARGS[@]}"} \
  -nic user \
  ${CTRL_ARGS[@]+"${CTRL_ARGS[@]}"} \
  -monitor unix:/tmp/d_mon4.sock,server,nowait \
  -chardev socket,id=con0,path=/tmp/d_con4.sock,server=on,wait=off,logfile=/tmp/xdj_console.log \
  -serial chardev:con0 -serial null \
  -gdb tcp::1234
