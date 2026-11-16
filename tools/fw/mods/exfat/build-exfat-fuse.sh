#!/usr/bin/env bash
# Build mount.exfat-fuse (relan/exfat, GPL-2.0-or-later) for the XDJ-RX: a static
# ARMv7 binary linked against musl + libfuse 2.9, so it needs nothing from the
# firmware's glibc 2.13 rootfs. Built in an Alpine linux/arm/v7 container (Docker,
# runs under emulation on Apple Silicon).
#
#   tools/fw/mods/exfat/build-exfat-fuse.sh [OUTDIR]     default: build/mods/exfat
#
# Writes OUTDIR/mount.exfat-fuse (stripped) and keeps the source tarball there.
set -euo pipefail
cd "$(dirname "$0")/../../../.."
OUT="${1:-build/mods/exfat}"
VER=1.4.0
SHA=a1cfedc55e0e7a12c184605aa0f0bf44b24a3fb272449b20b2c8bbe6edb3001e
TGZ="fuse-exfat-$VER.tar.gz"

mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
[ -f "$OUT/$TGZ" ] || curl -fsSL -o "$OUT/$TGZ" \
  "https://github.com/relan/exfat/releases/download/v$VER/$TGZ"
echo "$SHA  $OUT/$TGZ" | shasum -a 256 -c - >/dev/null || { echo "checksum mismatch: $TGZ" >&2; exit 1; }

docker run --rm --platform linux/arm/v7 -v "$OUT":/w -w /tmp alpine:3.20@sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc sh -euc "
  apk add --no-cache build-base fuse-dev fuse-static linux-headers >/dev/null
  tar xzf /w/$TGZ && cd fuse-exfat-$VER
  ./configure CFLAGS='-O2 -march=armv7-a -mtune=cortex-a9' LDFLAGS=-static >/dev/null
  make -j4 >/dev/null
  install -m 0755 fuse/mount.exfat-fuse /w/mount.exfat-fuse
  strip /w/mount.exfat-fuse"
file "$OUT/mount.exfat-fuse"
