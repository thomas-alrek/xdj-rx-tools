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
#     spi_board_info (see `xdj prepare`, crates/xdj-fw/src/firmware.rs).
#   - console on ttymxc0 (sabresd's early-console UART) -> serial 0 here.
#   - video=mxcfb0:dev=ldb (SABRE-SD panel is LVDS) so /dev/fb0 comes up.
#   - the nand image is attached to uSDHC3 via -drive id=xdjsd (03-sabresd-board).
#
# The boot splash renders at 32bpp, then the app switches the framebuffer to
# 16bpp RGB565 and the main deck UI comes up. The QEMU scanout
# (hw/arm/fsl-imx6.c) reads the live IPU display-channel stride and decodes
# whichever depth is active, so both render with no firmware changes.
#
# Dual-core by default = the real XDJ-RX config (boots reliably now, and faster);
# XDJSMP=1 forces single-core. NOTE: the init script launches /root/pioneer/rbp on
# dual-core but the rbp_solo symlink (same binary) on single-core, so process
# listings show "rbp" vs "rbp_solo" depending on XDJSMP -- `pidof` both to be safe.
# The window is resizable (aspect locked 800:480) -- drag a corner.
# SUBUCOM_TRACE=1 logs the panel-MCU SPI frames to stderr (for panel reversing).
set -euo pipefail
cd "$(dirname "$0")/.."
Q=build/qemu
QEMU="${QEMU:-$(ls build/qemu-src/*/build/qemu-system-arm 2>/dev/null | head -1 || true)}"
QEMU="${QEMU:-qemu-system-arm}"
SD="${1:-$Q/nand.img}"
for f in vmlinux.bin tramp.bin atags.bin; do
  [ -f "$Q/$f" ] || { echo "missing $Q/$f — run: crates/xdj-fw/target/release/xdj prepare build/XDJRX.iso" >&2; exit 1; }
done
[ -f "$SD" ] || { echo "no nand image: $SD  (run qemu/build-nand.sh)" >&2; exit 1; }
SMP="${XDJSMP:-2}"            # dual-core by default = real XDJ-RX config (boots fine now,
                             # and faster); the init script launches /root/pioneer/rbp.
                             # XDJSMP=1 boots single-core (launches the rbp_solo symlink ->
                             # SAME binary; a bit slower under TCG).
DISPLAY_OPT="${XDJDISPLAY:-cocoa,zoom-to-fit=on,zoom-interpolation=on}"
# Network backend attached to the i.MX6 FEC (eth0); the firmware runs a DHCP client
# on eth0 at boot. XDJNIC:
#   auto (default) -- the LAN bridge if its socket_vmnet daemon is installed
#                     (tools/net/install-vmnet-bridge.sh, one-time, optional), else the
#                     Mac-only network (`… mac`), else NAT
#   user           -- QEMU user-mode NAT: private 10.0.2.0/24, invisible to the LAN
#   /path/to/sock  -- a socket_vmnet socket
# QEMU runs as you in every mode (the root daemon owns the vmnet interface).
XDJNIC="${XDJNIC:-auto}"
if [ "$XDJNIC" = auto ]; then
  IFN="$(route -n get default 2>/dev/null | awk '/interface:/{print $2; exit}')"
  XDJNIC=user
  for d in /var/run /opt/homebrew/var/run /usr/local/var/run; do
    [ -S "$d/socket_vmnet.bridged.$IFN" ] && { XDJNIC="$d/socket_vmnet.bridged.$IFN"; break; }
  done
  if [ "$XDJNIC" = user ]; then
    for d in /var/run /opt/homebrew/var/run /usr/local/var/run; do
      [ -S "$d/socket_vmnet" ] && { XDJNIC="$d/socket_vmnet"; break; }
    done
  fi
fi
case "$XDJNIC" in
  user) NET_ARGS=(-nic user)
        echo "net: user-mode NAT (not on the LAN; optional bridge: tools/net/install-vmnet-bridge.sh)" >&2 ;;
  /*)   NET_ARGS=(-netdev "stream,id=xdjnet,server=off,reconnect-ms=1000,addr.type=unix,addr.path=$XDJNIC"
                  -net nic,netdev=xdjnet)
        echo "net: vmnet via $XDJNIC" >&2 ;;
  *)    echo "XDJNIC=$XDJNIC: expected auto, user or a socket path" >&2; exit 1 ;;
esac

# Optional USB mass-storage at boot (a rekordbox-formatted disk image), cold-plugged on the
# OTG bus -- for quick checks only. The front slots USB1/USB2 sit behind the hub and must be
# inserted after rbp polls (tools/xdj-panel/debug/relaunch.sh + device_add, docs/usb.md):
if [ -n "${XDJUSB:-}" ]; then
  [ -f "$XDJUSB" ] || { echo "XDJUSB=$XDJUSB not found" >&2; exit 1; }
  USB_ARGS=(-drive if=none,id=usbstick,file="$XDJUSB",format=raw
            -device usb-storage,bus=usb-bus.0,drive=usbstick,id=xdjusb)
  echo "usb: attaching $XDJUSB as usb-storage on usb-bus.0 (cold-plug)" >&2
fi

# Audio: route the deck's master output to a host sink so you actually hear it.
# The deck playhead (and the PCM) is slaved to the audio clock (xdj-audio.c), and the
# guest's ALSA ring is tiny (~2.9ms). QEMU's audio mixer polls voices on a timer
# whose default period (10ms) is far too coarse to pace that ring -- the guest
# then refills at ~27% of real time and the stream starves (very slow / silent).
# timer-period=1451 (µs, ~689Hz = the 64-frame period rate) paces it at real time.
# Override the backend for your OS with XDJAUDIO (e.g. pa / pipewire / sdl), keep
# the timer-period; XDJAUDIO=none disables host audio (deck still plays, silent).
case "$(uname)" in
  Darwin) AUDIO_DEF="coreaudio,timer-period=1451" ;;
  *)      AUDIO_DEF="pa,timer-period=1451" ;;
esac
AUDIO_OPT="${XDJAUDIO:-$AUDIO_DEF}"
AUDIO_ARGS=()
[ "$AUDIO_OPT" != "none" ] && AUDIO_ARGS=(-audio "$AUDIO_OPT")

# Control sockets (opt-in, per-instance). Set XDJ_RUNTIME_DIR to push panel
# controls over the QEMU chardevs (id=xdjctrl = sub-ucom snapshot, id=xdjkey =
# mainkey GPIO) instead of the global /tmp/subucom_ctrl + /tmp/mainkey files. UNSET =
# no chardevs, so the harness falls back to the /tmp files (or
# XDJ_CTRL_PATH/XDJ_KEY_PATH), and existing panel/inject workflows are unchanged.
CTRL_ARGS=()
if [ -n "${XDJ_RUNTIME_DIR:-}" ]; then
  mkdir -p "$XDJ_RUNTIME_DIR"
  CTRL_ARGS=(-chardev socket,id=xdjctrl,path="$XDJ_RUNTIME_DIR/ctrl.sock",server=on,wait=off
             -chardev socket,id=xdjkey,path="$XDJ_RUNTIME_DIR/key.sock",server=on,wait=off)
  echo "ctrl: xdjctrl=$XDJ_RUNTIME_DIR/ctrl.sock  xdjkey=$XDJ_RUNTIME_DIR/key.sock" >&2
fi

# sabresd console is ttymxc0 = serial 0 -> wire it to this terminal (Ctrl-A X quits).
exec "$QEMU" -M sabrelite -smp "$SMP" -m 256 -no-reboot \
  -name "Pioneer XDJ-RX" \
  -display "$DISPLAY_OPT" \
  ${AUDIO_ARGS[@]+"${AUDIO_ARGS[@]}"} \
  -device loader,file="$Q/vmlinux.bin",addr=0x10008000,force-raw=on \
  -device loader,file="$Q/atags.bin",addr=0x10000100,force-raw=on \
  -device loader,file="$Q/tramp.bin",addr=0x10000000,cpu-num=0 \
  -drive if=none,id=xdjsd,file="$SD",format=raw \
  "${NET_ARGS[@]}" \
  ${USB_ARGS[@]+"${USB_ARGS[@]}"} \
  ${CTRL_ARGS[@]+"${CTRL_ARGS[@]}"} \
  -serial mon:stdio -serial null
