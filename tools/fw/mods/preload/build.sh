#!/usr/bin/env bash
# Build libxdjmod.so for the XDJ-RX userland (ARM EABI5 soft-float, glibc 2.13) with
# Debian's armel cross-gcc in Docker, and check it needs no glibc symbol newer than
# the firmware's 2.13.
#
#   tools/fw/mods/preload/build.sh [OUTDIR]     default: build/mods/preload
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE/../../../.."
OUT="${1:-build/mods/preload}"
mkdir -p "$OUT"; OUT="$(cd "$OUT" && pwd)"

docker run --rm -v "$HERE":/src:ro -v "$OUT":/out debian:bookworm sh -euc '
  apt-get update -qq >/dev/null
  apt-get install -y -qq gcc-arm-linux-gnueabi binutils-arm-linux-gnueabi >/dev/null 2>&1
  arm-linux-gnueabi-gcc -shared -fPIC -O2 -Wall -march=armv7-a -mtune=cortex-a9 \
      -o /out/libxdjmod.so /src/*.c
  arm-linux-gnueabi-strip /out/libxdjmod.so
  arm-linux-gnueabi-objdump -T /out/libxdjmod.so | grep -o "GLIBC_[0-9.]*" | sort -uV > /out/glibc-versions.txt'
NEWEST="$(tail -1 "$OUT/glibc-versions.txt")"
printf '%s\n%s\n' "$NEWEST" GLIBC_2.13 | sort -V -C \
  || { echo "libxdjmod.so needs $NEWEST; the firmware has glibc 2.13" >&2; exit 1; }
echo "libxdjmod.so: $(wc -c < "$OUT/libxdjmod.so") bytes, newest glibc symbol $NEWEST"
