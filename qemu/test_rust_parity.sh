#!/usr/bin/env bash
# Byte-for-byte parity: the Rust firmware/NAND code (crates/xdj-fw, the `xdj` CLI and xdj-host)
# against the Python dev tools it replaces (xdjrx_upd.py, qemu_prepare.py,
# qemu/xdj_update_stick.py, qemu/xdj_nand.py).
#
#   qemu/test_rust_parity.sh <firmware.UPD|.zip> [provisioned-nand.img]
#
# Needs the AES key the usual way (.env / XDJRX_AES_KEY), uv (or python3 with
# pycdlib + pycryptodome), and a built crates/xdj-fw/target/release/xdj.
set -euo pipefail
cd "$(dirname "$0")/.."
UPD="$1"; PROV="${2:-}"
X=crates/xdj-fw/target/release/xdj
[ -x "$X" ] || ( cd crates/xdj-fw && cargo build --release -q )
PY="python3"; command -v uv >/dev/null && PY="uv run"
T=$(mktemp -d /tmp/xdjparity.XXXX); trap 'rm -rf "$T"' EXIT
mkdir -p "$T/py" "$T/rs" "$T/pyb" "$T/rsb"
ok=0; bad=0
same() { if cmp -s "$1" "$2"; then echo "  OK    $3"; ok=$((ok+1)); else echo "  DIFF  $3"; bad=$((bad+1)); fi; }

echo "decrypt"
$PY xdjrx_upd.py decrypt "$UPD" -o "$T/py.iso" >/dev/null
$X fw decrypt "$UPD" -o "$T/rs.iso" >/dev/null
same "$T/py.iso" "$T/rs.iso" fw.iso

echo "boot blobs"
XDJ_OUT="$T/py" $PY qemu_prepare.py "$T/py.iso" >/dev/null
$X prepare "$T/rs.iso" --out "$T/rs" >/dev/null
for f in uImage vmlinux.bin tramp.bin atags.bin; do same "$T/py/$f" "$T/rs/$f" "$f"; done

echo "update stick (build time masked)"
$PY qemu/xdj_update_stick.py "$UPD" "$T/py.stick" >/dev/null
$X fw stick "$UPD" "$T/rs.stick" >/dev/null
python3 - "$T/py.stick" "$T/rs.stick" <<'EOF'
import sys
a, b = (bytearray(open(p, "rb").read()) for p in sys.argv[1:])
def mask(x):
    x[39:43] = b"\0" * 4                       # volume id (= time)
    fat = int.from_bytes(x[22:24], "little")
    root = (1 + 2 * fat) * 512
    for e in (root, root + 32):                # dirent times/dates
        x[e + 14:e + 20] = b"\0" * 6; x[e + 22:e + 26] = b"\0" * 4
    return x
open(sys.argv[1], "wb").write(mask(a)); open(sys.argv[2], "wb").write(mask(b))
EOF
same "$T/py.stick" "$T/rs.stick" update-stick.img

echo "NAND init"
$PY qemu/xdj_nand.py init "$T/py.nand" --iso "$T/py.iso" >/dev/null
$X nand init "$T/rs.nand" --iso "$T/rs.iso" >/dev/null
same "$T/py.nand" "$T/rs.nand" "nand-flash.img (factory)"
$X nand info "$T/rs.nand" | grep -q "installed *false" && { echo "  OK    factory NAND: not installed"; ok=$((ok+1)); } \
  || { echo "  DIFF  factory NAND reads as installed"; bad=$((bad+1)); }

if [ -n "$PROV" ]; then
  echo "NAND boot (provisioned unit)"
  $PY qemu/xdj_nand.py boot "$PROV" "$T/pyb" >/dev/null
  $X nand boot "$PROV" "$T/rsb" 2>/dev/null
  for f in uImage vmlinux.bin tramp.bin atags.bin; do same "$T/pyb/$f" "$T/rsb/$f" "nand-boot/$f"; done
  $X nand info "$PROV" | grep -q "installed *true" && { echo "  OK    provisioned NAND: installed"; ok=$((ok+1)); } \
    || { echo "  DIFF  provisioned NAND reads as not installed"; bad=$((bad+1)); }
fi
echo "$ok OK, $bad different"
[ "$bad" = 0 ]
