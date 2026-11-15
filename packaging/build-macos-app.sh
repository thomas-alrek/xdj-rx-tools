#!/usr/bin/env bash
#
# Assemble a self-contained XDJ-RX.app (standalone-app roadmap, Phase 5, macOS).
#
# Bundles everything the app needs to prepare a firmware and run the emulator:
#   - the Tauri shell (Contents/MacOS/XDJ-RX) + the xdj-host orchestrator
#   - the patched qemu-system-arm, with its Homebrew dylibs relocated into the app
#   - the panel pages + assets + vendored noVNC (served by xdj-host's in-process panel)
#
# NOT bundled (by design): firmware, the AES key, the unit's NAND — those are generated
# at runtime from the user's own .UPD into app-data (see roadmap §3 / §5bis-A).
#
# Signing: binaries are **ad-hoc** signed (`codesign -s -`) because Apple Silicon
# refuses to run a binary whose signature dylibbundler invalidated. This is NOT
# Developer-ID signing or notarization (deferred, §5.4) — a notarized build is what a
# user double-clicks without Gatekeeper warnings; this ad-hoc build runs locally.
#
# Prereqs: Rust, a built patched qemu (qemu/build-qemu.sh),
#          dylibbundler (brew install dylibbundler). No Python: everything runs in xdj-host.
#
# Usage:  packaging/build-macos-app.sh        # -> dist/XDJ-RX.app
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
APP="$ROOT/dist/XDJ-RX.app"
RES="$APP/Contents/Resources"
MACOS="$APP/Contents/MacOS"

QEMU="$ROOT/build/qemu-src/qemu-11.1.0/build/qemu-system-arm"

command -v dylibbundler >/dev/null || { echo "need dylibbundler (brew install dylibbundler)"; exit 1; }
[ -x "$QEMU" ] || { echo "need the patched qemu ($QEMU) — run qemu/build-qemu.sh"; exit 1; }

echo "==> building release binaries"
( cd app/host && cargo build --release )
( cd app/shell && cargo build --release )

echo "==> assembling $APP"
rm -rf "$APP"
mkdir -p "$MACOS" "$RES/panel" "$RES/ui"

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

# (firmware prep + the unit's NAND run inside xdj-host: app/host/src/{firmware,nand}.rs)

# panel sidecar + assets + noVNC
# panel pages (the panel server itself is in-process in xdj-host: app/host/src/panel.rs)
cp tools/xdj-panel/panel.html tools/xdj-panel/logs.html "$RES/panel/"
[ -d tools/xdj-panel/assets ] && cp -R tools/xdj-panel/assets "$RES/panel/assets"   # optional (removed in the UI rebuild)
cp -R app/ui/novnc "$RES/ui/novnc"
# Knobs library (npm: audio-knobs). Install if missing, then vendor just dist/.
[ -d app/ui/node_modules/audio-knobs/dist ] || ( cd app/ui && npm install --no-audit --no-fund )
cp -R app/ui/node_modules/audio-knobs/dist "$RES/ui/knobs"

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

# License texts for everything bundled (GPL/LGPL/MPL/MIT...; THIRD_PARTY_NOTICES.md)
echo "==> collecting licenses"
python3 packaging/collect-licenses.py "$RES" "$ROOT/build/qemu-src/qemu-11.1.0"

# Ad-hoc sign inside-out (arm64 won't run binaries with a dylibbundler-broken sig).
echo "==> ad-hoc signing"
find "$RES/libs" -name '*.dylib' -exec codesign --force -s - {} + 2>/dev/null || true
for b in "$RES/qemu-system-arm" "$RES/xdj-host"; do
  codesign --force -s - "$b" 2>/dev/null || true
done
codesign --force --deep -s - "$APP" 2>/dev/null || true

echo "==> done: $APP"
echo "    run (with a prepared firmware):  XDJ_FW_DIR=<app-data dir> \"$MACOS/XDJ-RX\""
echo "    or just open it and drop a .UPD."
