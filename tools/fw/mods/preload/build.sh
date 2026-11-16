#!/usr/bin/env bash
# Build libxdjmod.so for the XDJ-RX userland: the framework core (src/) plus the feature
# modules' sources, compiled with Debian's armel cross-gcc in Docker (image
# xdjmod-armel-cc, built once) and LINKED AGAINST THE FIRMWARE'S OWN LIBRARIES (FWLIB =
# the unpacked rootfs's lib/: glibc 2.13's libc/libdl/libpthread/libm, libstdc++), with
# -nostdlib and --no-undefined. So every symbol binds to the library and version the
# unit really provides, and anything it lacks fails the build instead of rbp's start
# ("relocation error ... not defined in file libc.so.6"). Hardware floating point inside
# the library (VFPv3; the Cortex-A9 has it) with the soft-float calling convention rbp
# uses.
#
#   tools/fw/mods/preload/build.sh OUTDIR FWLIB [FEATURE.c ...]
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="${1:?OUTDIR}"; FWLIB="${2:?FWLIB (unpacked rootfs/lib)}"; shift 2
mkdir -p "$OUT"; OUT="$(cd "$OUT" && pwd)"; FWLIB="$(cd "$FWLIB" && pwd)"
for l in libc.so.6 libdl.so.2 libpthread.so.0 libm.so.6 libstdc++.so.6; do
  [ -e "$FWLIB/$l" ] || { echo "build.sh: $FWLIB/$l missing (not an unpacked rootfs/lib?)" >&2; exit 1; }
done

IMG=xdjmod-armel-cc
docker image inspect "$IMG" >/dev/null 2>&1 || docker build -q -t "$IMG" - >/dev/null <<'DOCKERFILE'
FROM debian:bookworm
RUN apt-get update -qq && apt-get install -y -qq gcc-arm-linux-gnueabi binutils-arm-linux-gnueabi >/dev/null \
 && rm -rf /var/lib/apt/lists/*
DOCKERFILE

# feature sources: stage them (Docker sees only mounted dirs)
rm -rf "$OUT/features" "$OUT/obj"; mkdir -p "$OUT/features" "$OUT/obj"
for f in "$@"; do cp "$f" "$OUT/features/"; done

docker run --rm -v "$HERE/src":/src:ro -v "$OUT":/out -v "$FWLIB":/fwlib:ro "$IMG" sh -euc '
  CC="arm-linux-gnueabi-gcc -fPIC -O2 -Wall -Wextra -Wno-unused-parameter -march=armv7-a -mtune=cortex-a9 -mfloat-abi=softfp -mfpu=vfpv3-d16 -I/src -I/out/features"
  for f in /src/*.c $(ls /out/features/*.c 2>/dev/null); do
    $CC -c -o /out/obj/$(basename "$f" .c).o "$f"
  done
  arm-linux-gnueabi-gcc -shared -nostdlib -Wl,--no-undefined -Wl,-soname,libxdjmod.so \
      -Wl,-rpath-link,/fwlib -o /out/libxdjmod.so /out/obj/*.o \
      /fwlib/libc.so.6 /fwlib/libdl.so.2 /fwlib/libpthread.so.0 /fwlib/libm.so.6 \
      /fwlib/libstdc++.so.6 -lgcc
  arm-linux-gnueabi-strip /out/libxdjmod.so
  arm-linux-gnueabi-readelf -d /out/libxdjmod.so | grep NEEDED | sed "s/.*\[//; s/\]//" > /out/needed.txt
  arm-linux-gnueabi-objdump -T /out/libxdjmod.so | grep -o "GLIBC_[0-9.]*" | sort -uV > /out/glibc-versions.txt'
NEWEST="$(tail -1 "$OUT/glibc-versions.txt")"
printf '%s\n%s\n' "$NEWEST" GLIBC_2.13 | sort -V -C \
  || { echo "libxdjmod.so needs $NEWEST; the firmware has glibc 2.13" >&2; exit 1; }
echo "libxdjmod.so: $(wc -c < "$OUT/libxdjmod.so") bytes, needs $(tr '\n' ' ' < "$OUT/needed.txt")(newest glibc symbol $NEWEST), features: ${*:-none}"
