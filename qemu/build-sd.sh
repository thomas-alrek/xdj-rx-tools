#!/usr/bin/env bash
#
# Build an SD-card image that boots the XDJ-RX firmware to userspace in QEMU.
#
# The firmware's embedded /init supports three rootfs modes (nand/sd/nfs),
# chosen from the u-boot env (fw_printenv). QEMU models no NAND but does model
# the uSDHC/SD controller, so we use the firmware's own SD-boot mode:
#
#   SD layout:  env blob at byte 0xC0000 (rootfs=sd, mmcdev=/dev/mmcblk0p1)
#               MBR partition 1 (ext4) with the real rootfs
#
#   rootfs:     rootfs.cramfs (base) + pioneer.tar.gz (app) + gui.tar.gz (assets)
#               built as ext4 with ONLY features kernel 3.0.35 supports, plus a
#               debug serial shell added to /etc/inittab.
#
# The firmware is NOT modified; this is a test-bed rootfs assembled from the
# firmware's own payloads. Needs Docker (Linux: symlinks, device nodes, mke2fs).
#
# Usage:  qemu/build-sd.sh        # -> build/qemu/sd.img   (then ./qemu_run.sh)
set -euo pipefail
cd "$(dirname "$0")/.."
ISO=build/XDJRX_2.21.iso
Q=build/qemu
PY="${PYTHON:-python3}"

[ -f "$ISO" ] || { echo "need $ISO — run: python3 xdjrx_upd.py decrypt XDJRX.UPD -o $ISO" >&2; exit 1; }
command -v docker >/dev/null || { echo "need docker" >&2; exit 1; }

echo "Extracting rootfs payloads from the firmware ISO…"
mkdir -p "$Q/sdbuild"
"$PY" - "$ISO" "$Q/sdbuild" <<'PYEOF'
import sys, pycdlib
iso = pycdlib.PyCdlib(); iso.open(sys.argv[1])
for n in ("rootfs.cramfs", "pioneer.tar.gz", "gui.tar.gz"):
    with open(f"{sys.argv[2]}/{n}", "wb") as f:
        iso.get_file_from_iso_fp(f, rr_path=f"/images/{n}")
iso.close()
PYEOF
cp qemu/cramfs_extract.py "$Q/sdbuild/"

echo "Building ext4 rootfs + env + SD image in a Linux container…"
docker run --rm --privileged -v "$PWD/$Q/sdbuild":/b debian:12 bash -c '
set -e
export DEBIAN_FRONTEND=noninteractive
apt-get -qq update >/dev/null; apt-get -qq install -y e2fsprogs python3 u-boot-tools fdisk >/dev/null
cd /tmp
python3 /b/cramfs_extract.py /b/rootfs.cramfs tree
mkdir -p tree/root
tar xzf /b/pioneer.tar.gz -C tree/root
# NOTE: gui.tar.gz is intentionally NOT included. The firmware /init copies the
# rootfs into a fixed 48 MB tmpfs (SD path) and base+app+gui overflows it; on the
# real device the GUI assets are loaded separately at runtime. Omitting them
# gives a clean boot to a shell (the exFAT/USB test bed). For UI work the assets
# must be provided through the path the app expects, not the root tmpfs.
mkdir -p tree/dev
for n in "console c 5 1" "null c 1 3" "ttymxc1 c 207 17" "ttymxc0 c 207 16" \
         "tty c 5 0" "zero c 1 5" "ptmx c 5 2"; do
  set -- $n; mknod tree/dev/$1 $2 $3 $4 2>/dev/null || true
done
# Clear the root password so the firmware getty (ttymxc1) logs straight into a
# shell — the interactive test-bed console. (Test artifact; firmware untouched.)
sed -i "s|^root:[^:]*:|root::|" tree/etc/shadow 2>/dev/null || true
sed -i "s|^root:[^:]*:|root::|" tree/etc/passwd 2>/dev/null || true
# ext4 with ONLY features Linux 3.0.35 can mount
mke2fs -q -t ext4 -O ^metadata_csum,^64bit,^orphan_file,^metadata_csum_seed \
       -b 1024 -d tree -F rootfs.ext4 500000
printf "rootfs=sd\nmmcdev=/dev/mmcblk0p1\nboot_type=normal\nubifs_formatted=true\nbootdelay=0\n" > env.txt
mkenvimage -s 0x2000 -o env.bin env.txt
dd if=/dev/zero of=sd.img bs=1M count=512 status=none          # 512 MiB (power of 2)
dd if=env.bin of=sd.img bs=1 seek=786432 conv=notrunc status=none   # env @ 0xC0000
printf "label: dos\nstart=4096, type=83\n" | sfdisk sd.img >/dev/null 2>&1
dd if=rootfs.ext4 of=sd.img bs=512 seek=4096 conv=notrunc status=none
cp sd.img /b/sd.img
'
mv "$Q/sdbuild/sd.img" "$Q/sd.img"
rm -rf "$Q/sdbuild"
echo "built $Q/sd.img  ->  ./qemu_run.sh  (boots to a shell)"
