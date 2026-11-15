#!/usr/bin/env python3
"""
xdj_update_stick.py -- build the USB stick image a firmware update boots from.

    xdj_update_stick.py <firmware.UPD|.zip> <out.img>

Writes a raw FAT16 "superfloppy" image (no partition table) with the firmware at
the root as XDJRX.UPD -- what the update initramfs looks for: mount_nandupdate()
tries `mount -t vfat /dev/sdX1`, then the whole disk `/dev/sdX`, and wants
/mnt/usb/XDJRX.UPD.  Pure Python (no hdiutil/mkfs/mtools, no root), so the app can
build it on any host.  A Pioneer .zip is unpacked to its .UPD first.

The app inserts this stick on USB1 to provision a blank NAND unit with the
firmware's own ALL update (docs/update-path-plan.md, "Provisioning").
"""
import os
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

SECTOR = 512
ROOT_ENTRIES = 512                      # FAT16 fixed root directory
ROOT_SECTORS = ROOT_ENTRIES * 32 // SECTOR
RESERVED = 1                            # just the boot sector
NFATS = 2
SLACK_CLUSTERS = 64                     # free space after the file (the updater only reads)


def _geometry(size):
    """(sectors_per_cluster, data_clusters, fat_sectors) for a FAT16 volume holding
    one file of `size` bytes.  Linux picks the FAT type from the cluster count alone,
    so it must land in 4085..65524."""
    for spc in (1, 2, 4, 8, 16, 32, 64):
        need = -(-size // (spc * SECTOR)) + SLACK_CLUSTERS
        n = max(need, 4085 + 16)
        if n > 65524:
            continue
        fat = -(-((n + 2) * 2) // SECTOR)
        return spc, n, fat
    sys.exit(f"error: {size} bytes is too big for a FAT16 stick")


def _dos_datetime(t):
    lt = time.localtime(t)
    d = ((lt.tm_year - 1980) << 9) | (lt.tm_mon << 5) | lt.tm_mday
    tm = (lt.tm_hour << 11) | (lt.tm_min << 5) | (lt.tm_sec // 2)
    return d, tm


def _dirent(name83, attr, cluster, size, t):
    d, tm = _dos_datetime(t)
    return struct.pack("<11sBBBHHHHHHHI", name83, attr, 0, 0, tm, d, d, 0, tm, d,
                       cluster, size)


def build(data, out):
    spc, nclus, fat_secs = _geometry(len(data))
    csize = spc * SECTOR
    total = RESERVED + NFATS * fat_secs + ROOT_SECTORS + nclus * spc
    t = time.time()

    bs = bytearray(SECTOR)
    bs[0:3] = b"\xEB\x3C\x90"
    bs[3:11] = b"MSWIN4.1"
    struct.pack_into("<HBHBHHBHHHII", bs, 11,
                     SECTOR, spc, RESERVED, NFATS, ROOT_ENTRIES,
                     total if total < 0x10000 else 0, 0xF8, fat_secs,
                     32, 64, 0, total if total >= 0x10000 else 0)
    struct.pack_into("<BBBI11s8s", bs, 36, 0x80, 0, 0x29,
                     int(t) & 0xFFFFFFFF, b"XDJ UPDATE ", b"FAT16   ")
    bs[510:512] = b"\x55\xAA"

    nfile = -(-len(data) // csize)
    fat = bytearray(fat_secs * SECTOR)
    struct.pack_into("<HH", fat, 0, 0xFFF8, 0xFFFF)
    for i in range(nfile):                       # clusters 2..nfile+1, contiguous
        c = 2 + i
        struct.pack_into("<H", fat, c * 2, c + 1 if i < nfile - 1 else 0xFFFF)

    root = bytearray(ROOT_SECTORS * SECTOR)
    root[0:32] = _dirent(b"XDJ UPDATE ", 0x08, 0, 0, t)            # volume label
    root[32:64] = _dirent(b"XDJRX   UPD", 0x20, 2 if data else 0, len(data), t)

    with open(out, "wb") as f:
        f.write(bs)
        for _ in range(NFATS):
            f.write(fat)
        f.write(root)
        f.write(data)
        f.truncate(total * SECTOR)
    return total * SECTOR


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__.strip().split("\n\n")[1])
    import xdjrx_upd
    data, _ = xdjrx_upd.read_upd_bytes(sys.argv[1])
    xdjrx_upd.split_container(data)              # sanity: exits if not an XDJ .UPD
    tmp = sys.argv[2] + ".tmp"
    n = build(data, tmp)
    os.replace(tmp, sys.argv[2])
    print(f"{sys.argv[2]}: FAT16 stick, {n // (1 << 20)} MiB, XDJRX.UPD = {len(data)} bytes")


if __name__ == "__main__":
    main()
