#!/usr/bin/env bash
# Build xdjrx-patcher for macOS, Linux and Windows -> dist/patcher/.
#
#   tools/fw/build-patcher.sh [mac] [linux] [windows]     (default: all)
#
#   xdjrx-patcher-macos            universal (Apple Silicon + Intel), built natively (macOS host)
#   xdjrx-patcher-linux-x86_64     static (musl), built in Docker
#   xdjrx-patcher-linux-arm64      static (musl), built in Docker
#   xdjrx-patcher-windows-x86_64.exe   built in Docker (MinGW)
#
# Needs Rust (rustup) on macOS and Docker (with linux/amd64 emulation on Apple Silicon).
set -euo pipefail
cd "$(dirname "$0")/../.."
ROOT="$PWD"
OUT="$ROOT/dist/patcher"
mkdir -p "$OUT"
WANT="${*:-mac linux windows}"
CRATE=crates/xdjrx-patcher
VOL="xdjrx-patcher-cargo"          # Docker volume: the cargo registry cache

if [[ " $WANT " == *" mac "* ]]; then
  [ "$(uname)" = Darwin ] || { echo "mac: needs a macOS host" >&2; exit 1; }
  rustup target add aarch64-apple-darwin x86_64-apple-darwin >/dev/null
  for t in aarch64-apple-darwin x86_64-apple-darwin; do
    cargo build --release --manifest-path "$CRATE/Cargo.toml" --target "$t" -q
  done
  lipo -create -output "$OUT/xdjrx-patcher-macos" \
    "$CRATE/target/aarch64-apple-darwin/release/xdjrx-patcher" \
    "$CRATE/target/x86_64-apple-darwin/release/xdjrx-patcher"
  echo "built $OUT/xdjrx-patcher-macos"
fi

if [[ " $WANT " == *" linux "* ]]; then
  for arch in x86_64 arm64; do
    plat=linux/amd64; t=x86_64-unknown-linux-musl
    [ "$arch" = arm64 ] && { plat=linux/arm64; t=aarch64-unknown-linux-musl; }
    docker run --rm --platform "$plat" -v "$ROOT/crates":/src -v "$VOL-$arch":/usr/local/cargo/registry \
      -w /src/xdjrx-patcher rust:1-alpine sh -euc "
        apk add --no-cache musl-dev >/dev/null
        CARGO_TARGET_DIR=/src/xdjrx-patcher/target/docker-$arch cargo build --release -q --target $t
        cp /src/xdjrx-patcher/target/docker-$arch/$t/release/xdjrx-patcher /src/xdjrx-patcher/target/xdjrx-patcher-linux-$arch"
    mv "$CRATE/target/xdjrx-patcher-linux-$arch" "$OUT/"
    echo "built $OUT/xdjrx-patcher-linux-$arch"
  done
fi

if [[ " $WANT " == *" windows "* ]]; then
  docker run --rm --platform linux/amd64 -v "$ROOT/crates":/src -v "$VOL-win":/usr/local/cargo/registry \
    -w /src/xdjrx-patcher rust:1 sh -euc "
      apt-get update -qq >/dev/null && apt-get install -y -qq gcc-mingw-w64-x86-64 >/dev/null
      rustup target add x86_64-pc-windows-gnu >/dev/null
      CARGO_TARGET_DIR=/src/xdjrx-patcher/target/docker-win cargo build --release -q --target x86_64-pc-windows-gnu
      cp /src/xdjrx-patcher/target/docker-win/x86_64-pc-windows-gnu/release/xdjrx-patcher.exe /src/xdjrx-patcher/target/xdjrx-patcher-windows-x86_64.exe"
  mv "$CRATE/target/xdjrx-patcher-windows-x86_64.exe" "$OUT/"
  echo "built $OUT/xdjrx-patcher-windows-x86_64.exe"
fi
ls -l "$OUT"
