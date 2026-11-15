#!/usr/bin/env bash
#
# Build the QEMU that runs the UNMODIFIED Pioneer XDJ-RX firmware (QEMU is changed,
# never the firmware).
#
# Layout:
#   qemu/xdj/          the XDJ-RX device models as real source files -- edit these.
#                      Synced into hw/arm/xdj/ of the QEMU tree on every run:
#     xdj-board.c      GPIO reset levels/captures, realize dispatcher
#     xdj-display.c    Vivante GPU / IPU / thermal stubs, DVFS kick, framebuffer scanout
#     xdj-audio.c      SDMA engine + SSI/AUDMUX, paced period clock -> host audio
#     xdj-panel.c      front-panel MCU ("sub-ucom") SPI slave: control frames in,
#                      LED frames out, ctrl/key chardevs, MAIN-UCOM keys, USB faults
#     xdj-mfi.c        MFi auth coprocessor on I2C4 (fixes E-7206)
#     xdj-usbnc.c      USBNC wakeup registers
#   qemu/patches/      small edits to stock QEMU files:
#     01-imx6-soc-xdj-hooks       fsl-imx6.c calls into hw/arm/xdj/ + meson subdir
#     02-imx6-peripherals         SRC GPU reset, GPT/CCM DVFS kick, GPIO psr-reset,
#                                 ECSPI burst hook for the panel MCU
#     03-sabresd-board            SABRE-SD wiring: SD rootfs on uSDHC3, panel on ECSPI2
#     04-usb-chipidea-hub         high-speed hub, OTG VBUS, ChipIdea PSPD/SRI/uframe polling
#     05-macos-submillisecond-poll  sub-ms main-loop timers on macOS (no ppoll)
# Feature status: docs/emulator-status.md; details: the qemu-imx6-patches skill.
# Patches target QEMU $VER exactly (checksum-pinned).
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
SRC="${XDJ_QEMU_SRC:-build/qemu-src}"   # override to build elsewhere (e.g. a clean test tree)
PATCHES="$PWD/qemu/patches"
XDJ_SRC="$PWD/qemu/xdj"
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
# idempotent. A tree whose stamp names a patch that is no longer in qemu/patches/
# was built from a different patch set (e.g. the pre-consolidation 01..21 stack):
# re-extract it rather than stacking patches on top of each other.
STAMP=.xdj-patches
if [ -f "$STAMP" ]; then
  while read -r n; do
    [ -z "$n" ] || [ -f "$PATCHES/$n" ] && continue
    echo "  source tree was patched with a different patch set ($n) -- re-extracting qemu-$VER"
    cd ..; rm -rf "qemu-$VER"; tar xf "qemu-$VER.tar.xz"; cd "qemu-$VER"
    break
  done < "$STAMP"
fi
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

# The XDJ-RX device models: copy only changed files, so ninja rebuilds just those.
mkdir -p hw/arm/xdj
for f in "$XDJ_SRC"/*; do
  t="hw/arm/xdj/$(basename "$f")"
  cmp -s "$f" "$t" || { cp "$f" "$t"; echo "  synced: $t"; }
done

mkdir -p build && cd build
# --disable-sdl: we never use the SDL display/audio backend (dev uses cocoa, the
# standalone app uses VNC). On macOS the SDL dep is sdl2-compat, which dlopen's
# libSDL3 at runtime — a dependency bundlers can't follow, so it breaks the .app
# ("Failed loading SDL3 library"). Dropping it removes the problem and shrinks the
# bundle. cocoa/vnc/none all remain.
# --disable-libssh: the SSH block driver is never used, and libssh pulls in OpenSSL's
# libcrypto (Apache-2.0), which the FSF considers incompatible with GPLv2-only code like
# QEMU -- so the redistributed .app must not bundle it. (An existing build dir is switched
# over with `meson configure` below.)
[ -f build.ninja ] || ../configure --target-list=arm-softmmu --disable-werror --disable-docs --disable-tools --disable-sdl --disable-libssh
pyvenv/bin/meson configure -Dlibssh=disabled >/dev/null
ninja qemu-system-arm
echo
echo "built: $(pwd)/qemu-system-arm"
echo "run:   qemu/watch.sh"
