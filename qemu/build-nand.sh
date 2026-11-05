#!/usr/bin/env bash
#
# Build an SD-card image that boots the XDJ-RX firmware to its on-screen UI in
# QEMU.
#
# The firmware's embedded /init supports three rootfs modes (nand/sd/nfs),
# chosen from the u-boot env (fw_printenv). QEMU models no NAND but does model
# the uSDHC/SD controller, so we use the firmware's own SD-boot mode:
#
#   SD layout:  env blob at byte 0xC0000 (rootfs=sd, mmcdev=/dev/mmcblk0p1)
#               MBR partition 1 (ext4) = rootfs + app
#               MBR partition 2 (ext4) = GUI assets (fonts + imagedata)
#
#   rootfs:     rootfs.cramfs (base) + pioneer.tar.gz (app), built as ext4 with
#               ONLY features kernel 3.0.35 supports.
#   gui:        gui.tar.gz (pset/ fonts + imagedata, pioneer/ fontdata), mounted
#               at /root/gui where the app (rbp) reads it.
#
# Two test-bed adaptations are made to the *rootfs* (the firmware binaries are
# untouched — this is assembled from the firmware's own payloads):
#
#   1. The GUI partition is mounted at /root/gui from apl_start.  On the device
#      that mount is `mount_ubifs gui 10 /root/gui` from NAND, gated on $BOOTMODE
#      (NAND/MMC).  BOOTMODE comes from the i.MX6 boot-mode straps, which QEMU
#      does not emulate, so neither branch runs — we add an unconditional ext4
#      mount of partition 2 instead.  (The "pure" unmodified path is to model the
#      NAND GUI volume; deferred.)
#   2. The root password is cleared so the firmware getty on ttymxc1 logs
#      straight into a shell for poking at the running system.
#
# Needs Docker (Linux: symlinks, device nodes, mke2fs).
#
# Usage:  qemu/build-nand.sh        # -> build/qemu/nand.img   (then qemu/watch.sh)
set -euo pipefail
cd "$(dirname "$0")/.."
ISO=build/XDJRX.iso
Q=build/qemu
# Host python for the ISO extraction (needs pycdlib). Prefer the repo venv that
# run.sh / the other tools use, so deps are available without polluting the system.
if [ -n "${PYTHON:-}" ]; then PY="$PYTHON"
elif [ -x build/.venv/bin/python3 ]; then PY="build/.venv/bin/python3"
else PY="python3"; fi

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

echo "Building ext4 rootfs + GUI partition + env + SD image in a Linux container…"
docker run --rm --privileged -v "$PWD/$Q/sdbuild":/b debian:12 bash -c '
set -e
export DEBIAN_FRONTEND=noninteractive
apt-get -qq update >/dev/null; apt-get -qq install -y e2fsprogs python3 u-boot-tools fdisk >/dev/null
cd /tmp

# ---- partition 1: rootfs + app ----------------------------------------------
python3 /b/cramfs_extract.py /b/rootfs.cramfs tree
mkdir -p tree/root tree/dev
tar xzf /b/pioneer.tar.gz -C tree/root
# The firmware is dual-core: apl_start runs /root/pioneer/rbp only when it sees a
# second CPU, otherwise /root/pioneer/rbp_solo (which the firmware does NOT ship).
# QEMU emulates two Cortex-A9 cores much slower than one, so for a fast test bed
# we run single-core -- add rbp_solo as a symlink to rbp so apl_start still
# launches the app on one core. (Test artifact; the shipped firmware is untouched
# and runs dual-core on the real hardware.)
ln -sf rbp tree/root/pioneer/rbp_solo
for n in "console c 5 1" "null c 1 3" "ttymxc1 c 207 17" "ttymxc0 c 207 16" \
         "tty c 5 0" "zero c 1 5" "ptmx c 5 2" "fb0 c 29 0" \
         "mmcblk0 b 179 0" "mmcblk0p1 b 179 1" "mmcblk0p2 b 179 2"; do
  set -- $n; mknod tree/dev/$1 $2 $3 $4 2>/dev/null || true
done
# Clear the root password so the firmware getty (ttymxc1) logs straight into a
# shell. (Test artifact; firmware untouched.)
sed -i "s|^root:[^:]*:|root::|" tree/etc/shadow 2>/dev/null || true
sed -i "s|^root:[^:]*:|root::|" tree/etc/passwd 2>/dev/null || true
# Mount the GUI partition at /root/gui (see header note 1). Injected right after
# the app makes the directory, so it runs regardless of $BOOTMODE.
A=tree/root/pioneer/apl_start
sed -i "/^mkdir -p \/root\/gui/a\\mount -t ext4 -o ro /dev/mmcblk0p2 /root/gui 2>/dev/null" "$A"
# Log the faulting PC/registers to the console if the app crashes (debug aid; the
# app binary is untouched). Injected right after apl_start cds into its dir.
sed -i "/^cd \/root\/pioneer/a\\echo 1 > /proc/sys/kernel/print-fatal-signals 2>/dev/null" "$A"
# Bring up the network. The firmware /etc/rc.d/init.d/network only configures
# eth0 when rc.conf sets SYSCFG_IFACE0=y, which the apl_start boot path never
# runs -- so loopback and eth0 come up down. Bring loopback up and run a DHCP
# client on eth0 so, when the emulator uses a bridged nic (XDJNIC=vmnet-bridged...,
# see qemu/watch.sh), the device becomes a normal host on your LAN and a rekordbox
# PC on the same LAN can reach it. (-b backgrounds after the first attempt so boot
# is not blocked when no DHCP server answers.) Linking to rekordbox is then
# enabled from the LINK menu in the app (needs the panel buttons).
sed -i "/^mkdir -p \/root\/gui/a\\ifconfig lo 127.0.0.1 up 2>/dev/null; udhcpc -b -i eth0 -t 3 -T 1 2>/dev/null \&" "$A"
# Remove the one Vivante GAL DirectFB gfxdriver so DirectFB software-renders the
# UI without needing /dev/galcore. This was the workaround from when the stock
# galcore path hung; galcore now fully initialises on the default QEMU path
# (/dev/galcore exists — see docs/emulator-status.md / docs/gpu2d-handover.md), so
# this removal is no longer required to boot. It is kept for now because the idle
# UI software-renders identically and the hardware GPU-2D blit path still has an
# open gui_task crash; restoring this gfxdriver to blit through galcore is the
# next step. This is the single compromise vs. a byte-for-byte rootfs.
rm -f tree/usr/lib/directfb-1.4-0/gfxdrivers/libdirectfb_gal.so

mke2fs -q -t ext4 -O ^metadata_csum,^64bit,^orphan_file,^metadata_csum_seed \
       -b 1024 -d tree -F p1.ext4 150000

# ---- partition 2: GUI assets ------------------------------------------------
mkdir -p gui; tar xzf /b/gui.tar.gz -C gui
mke2fs -q -t ext4 -O ^metadata_csum,^64bit,^orphan_file,^metadata_csum_seed \
       -b 1024 -d gui -F p2.ext4 60000

# ---- assemble the SD image --------------------------------------------------
printf "rootfs=sd\nmmcdev=/dev/mmcblk0p1\nboot_type=normal\nubifs_formatted=true\nbootdelay=0\n" > env.txt
mkenvimage -s 0x2000 -o env.bin env.txt
dd if=/dev/zero of=nand.img bs=1M count=256 status=none             # 256 MiB (real XDJ-RX NAND)
dd if=env.bin of=nand.img bs=1 seek=786432 conv=notrunc status=none # env @ 0xC0000
printf "label: dos\nstart=4096, size=307200, type=83\nstart=311296, type=83\n" | sfdisk nand.img >/dev/null 2>&1
dd if=p1.ext4 of=nand.img bs=512 seek=4096    conv=notrunc status=none
dd if=p2.ext4 of=nand.img bs=512 seek=311296  conv=notrunc status=none
cp nand.img /b/nand.img
'
mv "$Q/sdbuild/nand.img" "$Q/nand.img"
rm -rf "$Q/sdbuild"
echo "built $Q/nand.img  ->  qemu/watch.sh   (boots to the UI)"
