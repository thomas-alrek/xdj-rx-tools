#!/usr/bin/env bash
#
# Build a QEMU that boots the UNMODIFIED XDJ-RX firmware all the way to
# userspace. Two small i.MX6 patches are applied to QEMU (not the firmware):
#
#   patches/01-imx6_src-gpu-reset.patch
#       The i.MX6 SRC model clears the CPU/IPU software-reset bits but not the
#       GPU ones. galcore resets the GPU via SRC_SCR bit 1/4 and polls until
#       they self-clear, so it spun forever. Make those bits self-clearing.
#
#   patches/02-fsl-imx6-gpu-stub.patch
#       Three additions to the i.MX6 machine:
#       (a) Vivante GPU register stub at 0x00130000 — reports the GPU idle,
#           echoes the clock-control status bits (16/17) that _ResetGPU() waits
#           for, and reports a GC2000. galcore's reset then completes, its probe
#           fails gracefully (no real GPU), and the kernel boots on to its init.
#       (b) Framebuffer scanout console — mirrors the mxc_sdc_fb memory
#           (800x480 ARGB8888 at 0x18000000) to a QEMU graphic console, so the
#           display is capturable (monitor 'screendump') and showable (-display).
#       (c) IPU1 interrupt stub — delivers the BG display channel's vsync/EOF
#           interrupt (ch 23) so the fb display-enable and DirectFB page-flips
#           complete on a real 60Hz IRQ instead of timing out. Fast boot, no
#           tearing. (IPU_TRACE=1 logs the driver's INT_CTRL writes.)
#       (d) XDJ Subucom SSI device type (front-panel MCU), attached by the
#           machine when booting as SABRE-SD (see patch 03).
#
#   patches/04-imx6-usb-detect-gpio.patch
#       The two USB-"device present" detect lines (GPIO4_IO30 = gpiodrv 126, and
#       GPIO7_IO12 = gpiodrv 204) idle HIGH on real hardware (pull-up, "no USB").
#       QEMU reset every GPIO input to 0, i.e. "USB present", so at UI init rbp's
#       UsbStorageManager pushed an initial USB state to the front-panel MCU
#       BEFORE the panel poll thread was started -- and blocked there forever, so
#       the panel (buttons/jogs) never came up. Reset those two pins high so the
#       unmodified firmware sees "no USB" and the panel initialises normally.
#
#   patches/05-imx6-dvfs-gpt-kick.patch
#       Keeps the DVFS/GPT timer path advancing so the firmware does not stall.
#
#   patches/06-imx6-usb-insert-hook.patch
#       Runtime USB FAULT-flag injection: touching /tmp/usb_insert (USB1) or
#       /tmp/usb2_insert (USB2) drives GPIO4_IO30 / GPIO7_IO12 LOW. These are
#       rbp's active-low USB fault flags (NOT insert-detect lines, as first
#       assumed): LOW on a mounted slot shows "USB Error. Remove the device.".
#       Normal inserts must NOT touch them -- the attach wakes HOST1 (patch 10).
#
#   patches/07-imx6-multifield-holds.patch
#       Extends the Subucom control-injection harness (added in patch 02) to apply
#       MULTIPLE simultaneous A/B holds (all lines of /tmp/subucom_ctrl), not just
#       one. Needed for analog controls that span two complementary frame fields
#       at once -- the tempo fader (0x04 negative + 0x0a positive); see
#       docs/xdj-rx-control-map.md.
#
#   patches/08-usb-highspeed-hub.patch
#       Makes QEMU's usb-hub a USB 2.0 (high-speed) dual-speed hub so it can
#       attach to the high-speed-only EHCI/chipidea HOST buses. The real XDJ-RX
#       wires both front USB slots behind a 2.0 hub on USB HOST1 (guest bus 2),
#       at devpaths 2-1.1 / 2-1.2 -- the kernel's Pioneer driver keys the
#       udev_usbctn slot-connect channels on exactly those paths, so the USB
#       drive must be placed there. See docs/usb-slot-topology.md.
#
#   patches/09-imx6-mainucom-keys.patch
#       The XDJ-RX's source-select buttons (USB1, USB2, rekordbox, MIDI) and
#       DECK/TIME/jack keys are NOT subucom (EUP-UCOM) keys -- the service manual
#       shows they are MAIN-UCOM "direct-input" keys: plain i.MX6 GPIO switches
#       (idle HIGH, pressed LOW) that rbp reads through /dev/gpiodrv. This adds a
#       runtime hook: writing a Linux GPIO number to /tmp/mainkey drives that pin
#       LOW (press); removing the file releases it (HIGH). The 8 key GPIOs idle
#       HIGH from reset (psr-reset, like the detect lines) so there is no boot
#       edge. Confirmed live: GPIO 158 = USB2, 160 = USB1 (press -> the browser
#       opens on that slot), 159 = rekordbox, 161 = MIDI; 119/120 = ch1/ch2
#       Phono-Line, 146/149 = DECK1/2 + TIME MODE. See docs/xdj-rx-control-map.md.
#       (An isolation build confirmed these key GPIOs do NOT affect the separate,
#       flaky rbp USB-library accept.)
#
#   patches/10-imx6-usbnc-wakeup.patch
#       Models the i.MX6 USBNC (USB non-core) wakeup-request block at USBOH3+0x800
#       (previously unmapped). The stock firmware's _is_usbh1_wakeup() reads the
#       controller's USBNC CTRL register bit 31 (OWIR) to validate a USB remote
#       wakeup before resuming a bus-suspended host controller; with it unmapped
#       (reads 0) the guest flooded "host1, _is_usbh1_wakeup, invalid wake up" and
#       never resumed, so a USB stick inserted AFTER boot never enumerated. This
#       derives OWIR live from the matching EHCI root port's Force-Port-Resume bit
#       (FPRES), so a device attaching to a suspended controller is reported as a
#       valid wakeup and Host1 resumes. FPRES self-clears when the guest completes
#       the resume, so OWIR does not latch (an earlier FPRES|CSC version churned:
#       CSC stays set on disconnect, re-asserting OWIR -> resume/re-enumerate loop). This is what makes hardware-accurate post-boot USB insertion work,
#       which in turn fixes the flaky library-accept: insert the stick after rbp is
#       polling and the stock udev mount rule + its blocking "echo > /proc/udev_usb2"
#       run while rbp reads them, so the library registers deterministically. See
#       docs/usb-accept-handover.md.
#
#   patches/12-imx6-otg-vbus-valid.patch
#       The chipidea OTG core's OTGSC register (guest 0x1A4) and the USB PHY
#       status register are both stubbed to read 0, i.e. no VBUS comparators. The
#       i.MX6 OTG driver polls OTGSC for VBUS/session-valid before enumerating and
#       only gave up at its internal timeout -- a multi-second stall at the boot
#       splash printing "wait otg vbus change timeout!" (OTG = usb0 / fsl-ehci.0 /
#       irq 75), paid on every boot. This gives the 0x1A4 block its own read op
#       reporting a settled host state (ID=0, A-VBUS-Valid, A/B-Session-Valid), so
#       the driver skips the wait and enumerates as host immediately. Only status
#       bits; the W1C interrupt bits stay 0 (no spurious OTG IRQ). Verified: the
#       timeout message is gone, OTG + Host1 register together, USB still mounts.
#
#   patches/13-imx6-subucom-autolevel.patch
#       Auto-inits the mixer to an audible default so the device "just works" on
#       boot (no manual gain/routing inject). rbp only polls the panel MCU once its
#       panel thread is up, so the SPI burst that drives the sub-ucom harness every
#       frame is itself the "rbp is running" signal -- no console scraping, no
#       external helper, no persistent /tmp files. A short settle after the first
#       poll, then the harness MERGES an audible default (both channels -> DECK +
#       unity-ish gains, the inj.py "base" values) into the frame per-control: a
#       /tmp/subucom_ctrl hold overrides the baseline for the offsets it sets (so
#       the web panel keeps full runtime control of faders etc.), while an
#       unrelated leftover hold can no longer silence the deck. Disable with
#       XDJ_AUTOLEVEL=0; settle delay XDJ_AUTOLEVEL_DELAY_MS (default 4000).
#       Verified: routing flips to DECK on its own, and stays DECK even with an
#       unrelated hold present; deck plays to the host. QEMU-side only.
#
#   patches/14-imx6-subucom-frame-hook.patch
#       Calls xdj_subucom_frame_start() at the start of every ECSPI burst
#       (imx_spi_flush_txfifo). That call previously existed only as an
#       uncommitted local edit, so a clean build sent all-zero sub-ucom frames and
#       NO panel control reached rbp. Also raises MAXHOLD to 64 (the web panel now
#       sends its full state) and makes auto-level centre both COLOR pots and bring
#       up CH2 as well as CH1.
#
#   patches/15-imx6-subucom-led-backchannel.patch
#       The other direction of the same SPI burst: captures rbp's MOSI SubTxData
#       (LED / level-meter / jog-ring state for the panel MCU), CRC-checks the
#       20-byte payload and publishes each change as "T <hex>\n" -- back up the
#       xdjctrl chardev (socket mode) or to /tmp/subucom_tx (XDJ_TX_PATH; file
#       mode). The web panel lights its buttons from it. Map: docs/subucom-led-map.md.
#
#   patches/16-imx6-audio-paced-sdma.patch
#       Fixes audio stutter. Patch 11 completed SDMA periods from the host audio
#       callback, ~2 periods (the guest's whole 2x64-frame ALSA ring) per call, so
#       rbp hit "ALSA: Resolve xrun" constantly. A pacer thread now completes one
#       period per tick, evenly spaced, feeding a PCM FIFO the host drains; the
#       tick is rate-locked to the FIFO level. 378 -> 2 xruns / 30 s under load.
#       XDJ_AUDIO_LATENCY_MS (FIFO target, default 20), XDJ_AUDIO_STATS=1.
#
#   patches/17-imx6-source-key-leds.patch
#       The rekordbox/MIDI/USB1/USB2 key LEDs are rbp GPIO outputs 162..165
#       (GPIO6_IO02..05, active-high, one-hot). Mirrors them and appends one byte to
#       the patch-15 LED line ("T <40 hex> <2 hex>"); the panel reads it as byte 0x14.
#
#   patches/18-imx6-auth-chip-i2c4.patch
#       Fixes E-7206 "AUTH CHIP ERROR": adds the missing I2C4 controller (0x021F8000,
#       guest /dev/i2c-3) and an MFi auth-coprocessor identity responder at 0x10
#       (rbp's self-check reads reg 0x00 == 0x05, reg 0x01 == 0x01). Also reports
#       the sub-ucom firmware version 0.35 in frame bytes 2-3 (XDJ_SUBUCOM_VER).
#
#   patches/19-imx6-update-mode-strap.patch
#       XDJ_BOOT_UPDATE=1 drives GPIO3_IO30 (the sub-ucom's update request) HIGH
#       from reset, so the initramfs boots into USB firmware-update mode.
#
#   patches/03-sabrelite-sabresd-sd.patch
#       The XDJ-RX firmware's real board is SABRE-SD (MACH_TYPE 3980), which
#       registers ECSPI2 (bus 1) for the panel MCU + audio codecs, on a
#       different uSDHC for the rootfs. This teaches the QEMU sabrelite machine
#       to attach the SD image to uSDHC3 (-drive if=none,id=xdjsd) and the
#       Subucom SSI slave to ECSPI2, so the unmodified firmware boots under its
#       own SABRE-SD init and /dev/subucom_spi1.0 appears.
#
# Result: the kernel boots to userspace in ~8s. As sabrelite (qemu/watch.sh) the
# app renders the real Pioneer DJ / rekordbox boot splash; as SABRE-SD
# qemu/watch.sh boots the firmware to its live UI with the panel MCU bound.
#
# Usage:  qemu/build-qemu.sh      # -> build/qemu-src/qemu-<ver>/build/qemu-system-arm
# Then:   qemu/build-nand.sh && qemu/watch.sh
set -euo pipefail
cd "$(dirname "$0")/.."
# The patches are diffs against this EXACT QEMU release; a different version's
# files would not apply. The version is pinned and the tarball checksum verified
# so the build is reproducible without vendoring the (huge) QEMU source tree.
VER=11.1.0
VER_SHA256=6ee1d1a61f68212476b27108c26da5f449dc09b626d42f8279ba0dc2e08fa858
SRC=build/qemu-src
PATCHES="$PWD/qemu/patches"
export PATH="/opt/homebrew/bin:$PATH"

command -v meson ninja pkg-config >/dev/null 2>&1 || {
  echo "Installing build deps (meson ninja pkg-config)…"; brew install meson ninja pkg-config; }

mkdir -p "$SRC"; cd "$SRC"
[ -f "qemu-$VER.tar.xz" ] || curl -fSL -o "qemu-$VER.tar.xz" "https://download.qemu.org/qemu-$VER.tar.xz"
# Verify the pinned tarball (shasum on macOS, sha256sum on Linux).
if command -v shasum >/dev/null 2>&1; then
  got=$(shasum -a 256 "qemu-$VER.tar.xz" | awk '{print $1}')
else
  got=$(sha256sum "qemu-$VER.tar.xz" | awk '{print $1}')
fi
[ "$got" = "$VER_SHA256" ] || {
  echo "ERROR: qemu-$VER.tar.xz sha256 mismatch" >&2
  echo "  expected $VER_SHA256" >&2
  echo "  got      $got" >&2
  echo "  The patches target QEMU $VER only; refusing to build a different source." >&2
  exit 1; }
[ -d "qemu-$VER" ] || tar xf "qemu-$VER.tar.xz"
cd "qemu-$VER"

# Applied patches are recorded in .xdj-patches so a re-run on an existing tree is
# idempotent. (A reverse dry-run alone is not enough: once a later patch edits the
# context of an earlier one, the earlier patch no longer reverse-applies cleanly and
# would be re-applied on top of itself.) Trees from before the stamp file fall back
# to the reverse dry-run.
STAMP=.xdj-patches
touch "$STAMP"
for p in "$PATCHES"/*.patch; do
  n=$(basename "$p")
  if grep -qxF "$n" "$STAMP"; then
    echo "  already applied: $n"
  elif patch -p1 --dry-run -R -f <"$p" >/dev/null 2>&1; then
    echo "  already applied: $n"; echo "$n" >>"$STAMP"
  else
    echo "  applying: $n"; patch -p1 --no-backup-if-mismatch <"$p"; echo "$n" >>"$STAMP"
  fi
done

mkdir -p build && cd build
# --disable-sdl: we never use the SDL display/audio backend (dev uses cocoa, the
# standalone app uses VNC). On macOS the SDL dep is sdl2-compat, which dlopen's
# libSDL3 at runtime — a dependency bundlers can't follow, so it breaks the .app
# ("Failed loading SDL3 library"). Dropping it removes the problem and shrinks the
# bundle. cocoa/vnc/none all remain.
[ -f build.ninja ] || ../configure --target-list=arm-softmmu --disable-werror --disable-docs --disable-tools --disable-sdl
ninja qemu-system-arm
echo
echo "built: $(pwd)/qemu-system-arm"
echo "run:   qemu/watch.sh"
