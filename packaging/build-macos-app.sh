#!/usr/bin/env bash
#
# Assemble a self-contained XDJ-RX.app (standalone-app roadmap, Phase 5, macOS).
#
# Bundles everything the app needs to prepare a firmware and run the emulator:
#   - the Tauri shell (Contents/MacOS/XDJ-RX) + the xdj-host orchestrator
#   - the patched qemu-system-arm, with its Homebrew dylibs relocated into the app
#   - the prep scripts (xdjrx_upd / qemu_prepare / build-nand / cramfs_extract)
#   - the panel sidecar (server.py) + assets + vendored noVNC
#   - e2fsprogs (mke2fs/debugfs) with its dylibs relocated
#   - a standalone Python with the prep deps (pycdlib / pycryptodome / inflate64)
#
# NOT bundled (by design): firmware, the AES key, nand.img — those are generated at
# runtime from the user's own .UPD into app-data (see roadmap §3 / §5bis-A).
#
# Signing: binaries are **ad-hoc** signed (`codesign -s -`) because Apple Silicon
# refuses to run a binary whose signature dylibbundler invalidated. This is NOT
# Developer-ID signing or notarization (deferred, §5.4) — a notarized build is what a
# user double-clicks without Gatekeeper warnings; this ad-hoc build runs locally.
#
# Prereqs: Rust, a built patched qemu (qemu/build-qemu.sh), Homebrew e2fsprogs,
#          dylibbundler (brew install dylibbundler). Needs network for pip + python.
#
# Usage:  packaging/build-macos-app.sh        # -> dist/XDJ-RX.app
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
APP="$ROOT/dist/XDJ-RX.app"
RES="$APP/Contents/Resources"
MACOS="$APP/Contents/MacOS"

QEMU="$ROOT/build/qemu-src/qemu-11.1.0/build/qemu-system-arm"
E2FS_SBIN="$(brew --prefix e2fsprogs 2>/dev/null)/sbin"
# python-build-standalone (relocatable CPython); cached under build/.
PYSTD="${XDJ_PYSTD:-$ROOT/build/python-standalone.tar.gz}"
PYSTD_URL="https://github.com/astral-sh/python-build-standalone/releases/download/20250612/cpython-3.12.11+20250612-aarch64-apple-darwin-install_only.tar.gz"

command -v dylibbundler >/dev/null || { echo "need dylibbundler (brew install dylibbundler)"; exit 1; }
[ -x "$QEMU" ] || { echo "need the patched qemu ($QEMU) — run qemu/build-qemu.sh"; exit 1; }
[ -x "$E2FS_SBIN/mke2fs" ] || { echo "need Homebrew e2fsprogs (brew install e2fsprogs)"; exit 1; }

echo "==> building release binaries"
( cd app/host && cargo build --release )
( cd app/shell && cargo build --release )

echo "==> assembling $APP"
rm -rf "$APP"
mkdir -p "$MACOS" "$RES/scripts/qemu" "$RES/panel" "$RES/ui" "$RES/e2fsprogs"

cp app/shell/target/release/xdj-rx-shell "$MACOS/XDJ-RX"
cp app/host/target/release/xdj-host "$RES/xdj-host"

# Strip all LC_RPATHs dylibbundler leaves on a binary (it rewrites each existing
# rpath to the -p value, producing duplicates dyld rejects; our deps are absolute
# @executable_path paths, so no rpath is needed).
strip_rpaths() {
  while install_name_tool -delete_rpath "@executable_path/libs/" "$1" 2>/dev/null; do :; done
}

# qemu + relocate its dylibs to Resources/libs. Prefix @executable_path (not
# @loader_path): qemu runs as its own process, so @executable_path = Resources/ for
# the binary AND every dylib it loads — correct for inter-library deps too.
cp "$QEMU" "$RES/qemu-system-arm"; chmod u+w "$RES/qemu-system-arm"
dylibbundler -cd -of -b -x "$RES/qemu-system-arm" -d "$RES/libs" -p "@executable_path/libs/"
strip_rpaths "$RES/qemu-system-arm"

# qemu data dir (keymaps) — qemu is relocated away from its build tree, so -L needs it
mkdir -p "$RES/qemu-bios"
cp -R "$ROOT/build/qemu-src/qemu-11.1.0/pc-bios/keymaps" "$RES/qemu-bios/keymaps"

# prep scripts (mirror the repo layout the scripts resolve relative to themselves)
cp xdjrx_upd.py qemu_prepare.py "$RES/scripts/"
cp qemu/build-nand.py qemu/cramfs_extract.py "$RES/scripts/qemu/"

# panel sidecar + assets + noVNC
cp tools/xdj-panel/server.py tools/xdj-panel/panel.html "$RES/panel/"
cp -R tools/xdj-panel/assets "$RES/panel/assets"
cp -R app/ui/novnc "$RES/ui/novnc"

# e2fsprogs + relocate its dylibs to Resources/e2fsprogs/libs
cp "$E2FS_SBIN/mke2fs" "$E2FS_SBIN/debugfs" "$RES/e2fsprogs/"
chmod u+w "$RES/e2fsprogs/mke2fs" "$RES/e2fsprogs/debugfs"
dylibbundler -cd -of -b -x "$RES/e2fsprogs/mke2fs" -x "$RES/e2fsprogs/debugfs" \
  -d "$RES/e2fsprogs/libs" -p "@executable_path/libs/"
strip_rpaths "$RES/e2fsprogs/mke2fs"
strip_rpaths "$RES/e2fsprogs/debugfs"

# standalone Python + prep deps
[ -f "$PYSTD" ] || { echo "==> downloading python-build-standalone"; curl -fSL -o "$PYSTD" "$PYSTD_URL"; }
echo "==> bundling python + prep deps"
rm -rf "$RES/python"
tar -xzf "$PYSTD" -C "$RES"            # creates $RES/python
"$RES/python/bin/python3" -m pip install --quiet --disable-pip-version-check \
  pycdlib pycryptodome inflate64

# Info.plist
cp app/shell/icons/icon.png "$RES/icon.png" 2>/dev/null || true
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>XDJ-RX</string>
  <key>CFBundleDisplayName</key><string>Pioneer XDJ-RX</string>
  <key>CFBundleIdentifier</key><string>no.apriil.xdjrx</string>
  <key>CFBundleExecutable</key><string>XDJ-RX</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.1.0</string>
  <key>CFBundleVersion</key><string>0.1.0</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST

# Ad-hoc sign inside-out (arm64 won't run binaries with a dylibbundler-broken sig).
echo "==> ad-hoc signing"
find "$RES/libs" "$RES/e2fsprogs/libs" -name '*.dylib' -exec codesign --force -s - {} + 2>/dev/null || true
for b in "$RES/qemu-system-arm" "$RES/e2fsprogs/mke2fs" "$RES/e2fsprogs/debugfs" "$RES/xdj-host"; do
  codesign --force -s - "$b" 2>/dev/null || true
done
codesign --force --deep -s - "$APP" 2>/dev/null || true

echo "==> done: $APP"
echo "    run (with a prepared firmware):  XDJ_FW_DIR=<app-data dir> \"$MACOS/XDJ-RX\""
echo "    or just open it and drop a .UPD."
