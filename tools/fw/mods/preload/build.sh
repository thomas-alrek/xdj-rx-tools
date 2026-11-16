#!/usr/bin/env bash
# Build libxdjmod.so for the XDJ-RX userland (ARM EABI5 soft-float, glibc 2.13): the
# framework core (src/) plus the feature modules' sources, with Debian's armel cross-gcc
# in Docker (image xdjmod-armel-cc, built once). Fails if the library needs a glibc
# symbol newer than the firmware's 2.13.
#
#   tools/fw/mods/preload/build.sh OUTDIR [FEATURE.c ...]
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="${1:?OUTDIR}"; shift
mkdir -p "$OUT"; OUT="$(cd "$OUT" && pwd)"

IMG=xdjmod-armel-cc
docker image inspect "$IMG" >/dev/null 2>&1 || docker build -q -t "$IMG" - >/dev/null <<'EOF'
FROM debian:bookworm
RUN apt-get update -qq && apt-get install -y -qq gcc-arm-linux-gnueabi binutils-arm-linux-gnueabi >/dev/null \
 && rm -rf /var/lib/apt/lists/*
EOF

# feature sources: stage them (Docker sees only mounted dirs)
rm -rf "$OUT/features"; mkdir -p "$OUT/features"
for f in "$@"; do cp "$f" "$OUT/features/"; done

docker run --rm -v "$HERE/src":/src:ro -v "$OUT":/out "$IMG" sh -euc '
  arm-linux-gnueabi-gcc -shared -fPIC -O2 -Wall -Wextra -Wno-unused-parameter \
      -march=armv7-a -mtune=cortex-a9 -I/src -o /out/libxdjmod.so \
      /src/*.c $(ls /out/features/*.c 2>/dev/null) -lpthread
  arm-linux-gnueabi-strip /out/libxdjmod.so
  arm-linux-gnueabi-objdump -T /out/libxdjmod.so | grep -o "GLIBC_[0-9.]*" | sort -uV > /out/glibc-versions.txt'
NEWEST="$(tail -1 "$OUT/glibc-versions.txt")"
printf '%s\n%s\n' "$NEWEST" GLIBC_2.13 | sort -V -C \
  || { echo "libxdjmod.so needs $NEWEST; the firmware has glibc 2.13" >&2; exit 1; }
echo "libxdjmod.so: $(wc -c < "$OUT/libxdjmod.so") bytes, newest glibc symbol $NEWEST, features: ${*:-none}"
