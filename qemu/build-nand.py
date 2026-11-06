#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["pycdlib>=1.14"]
# ///
"""
Build the XDJ-RX SD/nand test image that boots the firmware to its on-screen UI
in QEMU -- WITHOUT Docker.

This is a pure-Python rewrite of the old qemu/build-nand.sh container recipe.
The only native dependency is e2fsprogs (mke2fs + debugfs), used to create the
ext4 filesystems and to inject the /dev device nodes.  Everything the container
previously needed root/Linux for is done here without root:

  * ISO extraction            -> pycdlib            (was: container)
  * cramfs -> directory tree  -> qemu/cramfs_extract.py
  * tar payloads              -> tarfile
  * /dev device nodes         -> debugfs 'mknod'     (was: mknod, needs root)
  * uid/gid 0 ownership       -> debugfs 'sif'       (was: implicit, ran as root)
  * ext4 images               -> mke2fs -d           (was: in container)
  * u-boot env blob           -> pure Python crc32   (was: mkenvimage)
  * MBR partition table       -> pure Python         (was: sfdisk)
  * image assembly            -> pure Python         (was: dd)

Install e2fsprogs if needed:
    macOS:  brew install e2fsprogs       (keg-only; this script finds it)
    Debian: apt-get install e2fsprogs

The firmware's embedded /init supports three rootfs modes (nand/sd/nfs), chosen
from the u-boot env.  QEMU models no NAND but does model the uSDHC/SD controller,
so we use the firmware's own SD-boot mode:

  SD layout:  env blob at byte 0xC0000 (rootfs=sd, mmcdev=/dev/mmcblk0p1)
              MBR partition 1 (ext4) = rootfs + app
              MBR partition 2 (ext4) = GUI assets (fonts + imagedata)

Two test-bed adaptations are made to the *rootfs* (the firmware binaries are
untouched -- this is assembled from the firmware's own payloads):

  1. The GUI partition is mounted at /root/gui from apl_start.  On the device
     that mount is `mount_ubifs gui 10 /root/gui` from NAND, gated on $BOOTMODE
     (NAND/MMC).  BOOTMODE comes from the i.MX6 boot-mode straps, which QEMU
     does not emulate, so neither branch runs -- we add an unconditional ext4
     mount of partition 2 instead.
  2. The root password is cleared so the firmware getty on ttymxc1 logs straight
     into a shell for poking at the running system.

Usage:  qemu/build-nand.py        # -> build/qemu/nand.img   (then qemu/watch.sh)
        (or via the wrapper: qemu/build-nand.sh)
"""

import os
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import zlib
from pathlib import Path

import pycdlib

# --- layout -----------------------------------------------------------------
REPO = Path(__file__).resolve().parent.parent
ISO = REPO / "build" / "XDJRX.iso"
Q = REPO / "build" / "qemu"
BUILD = Q / "sdbuild"
CRAMFS_EXTRACT = REPO / "qemu" / "cramfs_extract.py"

IMG_SIZE = 256 * 1024 * 1024          # 256 MiB (real XDJ-RX NAND)
SECTOR = 512
ENV_OFFSET = 0x000C0000               # u-boot env @ 0xC0000
ENV_SIZE = 0x2000
P1_START = 4096                       # sectors
P1_SECTORS = 307200                   # 150 MiB
P2_START = 311296                     # sectors
# partition 2 fills the rest of the disk, matching sfdisk's "start=..., (no size)"
P2_SECTORS = (IMG_SIZE // SECTOR) - P2_START

# ext4 options: ONLY features kernel 3.0.35 supports.
MKE2FS_OPTS = ["-O", "^metadata_csum,^64bit,^orphan_file,^metadata_csum_seed",
               "-b", "1024"]
P1_BLOCKS = 150000                    # 1 KiB blocks
P2_BLOCKS = 60000

# /dev device nodes the firmware boot path needs.  (name, type, major, minor)
DEV_NODES = [
    ("console",    "c",   5,  1),
    ("null",       "c",   1,  3),
    ("ttymxc1",    "c", 207, 17),
    ("ttymxc0",    "c", 207, 16),
    ("tty",        "c",   5,  0),
    ("zero",       "c",   1,  5),
    ("ptmx",       "c",   5,  2),
    ("fb0",        "c",  29,  0),
    ("mmcblk0",    "b", 179,  0),
    ("mmcblk0p1",  "b", 179,  1),
    ("mmcblk0p2",  "b", 179,  2),
]

# u-boot env (identical to the old mkenvimage input)
ENV_ENTRIES = [
    "rootfs=sd",
    "mmcdev=/dev/mmcblk0p1",
    "boot_type=normal",
    "ubifs_formatted=true",
    "bootdelay=0",
]


def log(msg):
    print(msg, flush=True)


def die(msg):
    sys.exit(f"build-nand: {msg}")


# --- e2fsprogs discovery ----------------------------------------------------
def find_tool(name):
    """Locate an e2fsprogs tool, preferring the Homebrew keg (keg-only on mac)
    over whatever happens to be on PATH (e.g. Android SDK's stray mke2fs)."""
    candidates = [
        f"/opt/homebrew/opt/e2fsprogs/sbin/{name}",   # Apple Silicon brew
        f"/usr/local/opt/e2fsprogs/sbin/{name}",      # Intel brew
        f"/opt/homebrew/sbin/{name}",
        f"/usr/sbin/{name}",
        f"/sbin/{name}",
    ]
    for c in candidates:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    found = shutil.which(name)
    if found:
        return found
    die(f"need '{name}' (e2fsprogs). Install it:\n"
        f"    macOS:  brew install e2fsprogs\n"
        f"    Debian: sudo apt-get install e2fsprogs")


# --- firmware payload extraction --------------------------------------------
def extract_iso(iso_path, dest):
    """Pull the three rootfs payloads out of the firmware ISO (no mounting)."""
    iso = pycdlib.PyCdlib()
    iso.open(str(iso_path))
    try:
        for n in ("rootfs.cramfs", "pioneer.tar.gz", "gui.tar.gz"):
            with open(dest / n, "wb") as f:
                iso.get_file_from_iso_fp(f, rr_path=f"/images/{n}")
    finally:
        iso.close()


def extract_cramfs(img, tree):
    """Expand the cramfs rootfs into a directory tree via the shared extractor.

    cramfs_extract.py skips device nodes when not root (we re-add them into the
    ext4 image with debugfs), and preserves files, dirs, symlinks and modes.
    Run with -I so the untrusted image dir can't shadow stdlib imports."""
    subprocess.run([sys.executable, "-I", str(CRAMFS_EXTRACT), str(img), str(tree)],
                   check=True)


# --- rootfs test-bed adaptations (equivalent to the old sed edits) ----------
def insert_after(path, pattern, new_line):
    """Insert new_line immediately after the first line matching `pattern`
    (anchored, like `sed '/re/a\\ ...'`).  Applying several inserts that target
    the SAME line in sequence reproduces sed's behaviour: the most recent insert
    ends up directly under the matched line."""
    lines = path.read_text().splitlines()
    rx = re.compile(pattern)
    for i, line in enumerate(lines):
        if rx.search(line):
            lines.insert(i + 1, new_line)
            break
    path.write_text("\n".join(lines) + "\n")


def adapt_rootfs(tree):
    root = tree / "root"
    (root).mkdir(parents=True, exist_ok=True)
    (tree / "dev").mkdir(parents=True, exist_ok=True)

    # The app (rbp + its payload) lands in /root.
    with tarfile.open(BUILD / "pioneer.tar.gz") as t:
        _extractall(t, root)

    # The firmware is dual-core: apl_start runs /root/pioneer/rbp only when it
    # sees a second CPU, otherwise /root/pioneer/rbp_solo (which the firmware
    # does NOT ship).  QEMU emulates two Cortex-A9 cores much slower than one, so
    # for a fast test bed we run single-core -- add rbp_solo as a symlink to rbp
    # so apl_start still launches the app on one core.  (Test artifact; the
    # shipped firmware is untouched and runs dual-core on real hardware.)
    solo = root / "pioneer" / "rbp_solo"
    if not solo.exists():
        solo.symlink_to("rbp")

    # Clear the root password so the firmware getty (ttymxc1) logs straight into
    # a shell.  (Test artifact; firmware untouched.)
    for name in ("shadow", "passwd"):
        p = tree / "etc" / name
        if p.exists():
            p.write_text(re.sub(r"(?m)^root:[^:]*:", "root::", p.read_text()))

    apl = root / "pioneer" / "apl_start"
    if apl.exists():
        # Mount the GUI partition at /root/gui (header note 1).  Injected right
        # after the app makes the directory, so it runs regardless of $BOOTMODE.
        insert_after(apl, r"^mkdir -p /root/gui",
                     "mount -t ext4 -o ro /dev/mmcblk0p2 /root/gui 2>/dev/null")
        # Log the faulting PC/registers to the console if the app crashes (debug
        # aid; the app binary is untouched).  Right after apl_start cds in.
        insert_after(apl, r"^cd /root/pioneer",
                     "echo 1 > /proc/sys/kernel/print-fatal-signals 2>/dev/null")
        # Bring up the network.  The firmware's init only configures eth0 when
        # rc.conf sets SYSCFG_IFACE0=y, which the apl_start boot path never runs
        # -- so bring loopback up and run a DHCP client on eth0 so that, when the
        # emulator uses a bridged nic (XDJNIC=bridged, see qemu/watch.sh), the
        # device becomes a normal host on the LAN that a rekordbox PC can reach.
        # (-b backgrounds after the first attempt so boot is not blocked when no
        # DHCP server answers.)
        insert_after(apl, r"^mkdir -p /root/gui",
                     "ifconfig lo 127.0.0.1 up 2>/dev/null; "
                     "udhcpc -b -i eth0 -t 3 -T 1 2>/dev/null &")

    # The Vivante GAL DirectFB gfxdriver (libdirectfb_gal.so) is kept intact:
    # this is a byte-for-byte rootfs.  galcore fully initialises on the default
    # QEMU path (/dev/galcore exists), so DirectFB blits through the real galcore
    # device as on hardware.


def _extractall(tar, dest):
    """tar.extractall that works across Python versions (the 'filter' arg is
    required from 3.14).  These are the firmware's own payloads (plain files and
    dirs -- verified: no device nodes/symlinks), extracted into a dir we own."""
    try:
        tar.extractall(dest, filter="fully_trusted")
    except TypeError:
        tar.extractall(dest)


# --- ext4 build + debugfs post-processing -----------------------------------
def collect_paths(tree):
    """Every entry under `tree` as an in-image absolute path, each listed once
    (symlinks included, not followed)."""
    out = []
    for dirpath, dirnames, filenames in os.walk(tree):
        for name in dirnames + filenames:
            full = Path(dirpath) / name
            out.append("/" + str(full.relative_to(tree)))
    return out


def build_ext4(tree, out_img, blocks, mke2fs, debugfs, add_dev_nodes):
    log(f"  mke2fs ext4 ({blocks} x 1KiB blocks) <- {tree.name}/")
    subprocess.run([mke2fs, "-q", "-t", "ext4", *MKE2FS_OPTS,
                    "-d", str(tree), "-F", str(out_img), str(blocks)],
                   check=True)

    # Build one debugfs command batch: create /dev nodes (mke2fs -d can't, and we
    # can't mknod on the host without root), then normalise every inode to uid/gid
    # 0 -- the old recipe ran as root so everything was root-owned; reproduce that
    # (also makes any setuid binary setuid-root, as intended).
    cmds = []
    if add_dev_nodes:
        # NOTE: this e2fsprogs 'mknod' only links the node when given a RELATIVE
        # name after `cd` into the parent; an absolute path leaves it unlinked.
        cmds.append("cd /dev")
        for name, typ, major, minor in DEV_NODES:
            cmds.append(f"mknod {name} {typ} {major} {minor}")
        cmds.append("cd /")
        # debugfs 'mknod' leaves mode 0000; the old `mknod` left 0644 (0666 & ~umask).
        # Match it -- the mode field includes the type bits, so OR them back in.
        for name, typ, _major, _minor in DEV_NODES:
            type_bits = 0o020000 if typ == "c" else 0o060000
            cmds.append(f"sif /dev/{name} mode 0{type_bits | 0o644:o}")
    for p in collect_paths(tree):
        cmds.append(f"sif {p} uid 0")
        cmds.append(f"sif {p} gid 0")

    log(f"  debugfs: {len(DEV_NODES) if add_dev_nodes else 0} device nodes, "
        f"chown {len(cmds) // 2 - (len(DEV_NODES) if add_dev_nodes else 0)} inodes -> 0:0")
    proc = subprocess.run([debugfs, "-w", "-f", "/dev/stdin", str(out_img)],
                          input="\n".join(cmds) + "\nquit\n",
                          text=True, capture_output=True)
    if proc.returncode != 0:
        die(f"debugfs failed:\n{proc.stdout}\n{proc.stderr}")

    # Sanity: the image must be clean (an unlinked mknod would leave errors).
    e2fsck = find_tool("e2fsck")
    chk = subprocess.run([e2fsck, "-fn", str(out_img)],
                         capture_output=True, text=True)
    if "WARNING: Filesystem still has errors" in (chk.stdout + chk.stderr):
        die(f"e2fsck reported errors in {out_img.name}:\n{chk.stdout}")


# --- pure-Python u-boot env (replaces mkenvimage) ---------------------------
def make_env_blob(entries, size):
    """Reproduce `mkenvimage -s <size>` byte-for-byte: a little-endian CRC32 over
    the (0xFF-padded) data area, followed by NUL-separated key=value entries
    terminated by an empty entry.  Padding byte is 0xFF (mkenvimage's default)."""
    data = ("\x00".join(entries) + "\x00\x00").encode("latin1")
    if len(data) > size - 4:
        die("env entries exceed env size")
    payload = data.ljust(size - 4, b"\xff")
    crc = zlib.crc32(payload) & 0xFFFFFFFF
    return struct.pack("<I", crc) + payload


# --- pure-Python MBR (replaces sfdisk) --------------------------------------
def _chs(lba, heads=255, sectors=63):
    """LBA -> packed 3-byte CHS (matching sfdisk's default 255/63 geometry),
    capped at the classic 1023/254/63 overflow marker."""
    c = lba // (heads * sectors)
    rem = lba % (heads * sectors)
    h = rem // sectors
    s = rem % sectors + 1
    if c > 1023:
        c, h, s = 1023, heads - 1, sectors
    return bytes([h & 0xFF, (s & 0x3F) | ((c >> 8) << 6) & 0xC0, c & 0xFF])


def make_mbr(parts):
    """Build a DOS/MBR sector.  `parts` is a list of (lba_start, sectors, type)."""
    mbr = bytearray(512)
    for i, (start, count, ptype) in enumerate(parts):
        e = 446 + i * 16
        mbr[e] = 0x00                                   # not bootable
        mbr[e + 1:e + 4] = _chs(start)
        mbr[e + 4] = ptype
        mbr[e + 5:e + 8] = _chs(start + count - 1)
        struct.pack_into("<I", mbr, e + 8, start)
        struct.pack_into("<I", mbr, e + 12, count)
    mbr[510] = 0x55
    mbr[511] = 0xAA
    return bytes(mbr)


# --- image assembly (replaces dd) -------------------------------------------
def assemble_image(out, env_blob, mbr, p1, p2):
    with open(out, "wb") as f:
        f.truncate(IMG_SIZE)                            # sparse 256 MiB of zeros
        f.seek(ENV_OFFSET); f.write(env_blob)           # env @ 0xC0000
        f.seek(0); f.write(mbr)                          # partition table
        for img, start in ((p1, P1_START), (p2, P2_START)):
            f.seek(start * SECTOR)
            with open(img, "rb") as s:
                shutil.copyfileobj(s, f, 1024 * 1024)


# --- main -------------------------------------------------------------------
def main():
    if not ISO.exists():
        die(f"need {ISO.relative_to(REPO)} -- run: "
            f"python3 xdjrx_upd.py decrypt XDJRX.UPD -o {ISO.relative_to(REPO)}")

    mke2fs = find_tool("mke2fs")
    debugfs = find_tool("debugfs")

    if BUILD.exists():
        shutil.rmtree(BUILD)
    BUILD.mkdir(parents=True, exist_ok=True)

    log("Extracting rootfs payloads from the firmware ISO…")
    extract_iso(ISO, BUILD)

    # ---- partition 1: rootfs + app ----
    log("Building ext4 rootfs (partition 1)…")
    tree = BUILD / "tree"
    extract_cramfs(BUILD / "rootfs.cramfs", tree)
    adapt_rootfs(tree)
    p1 = BUILD / "p1.ext4"
    build_ext4(tree, p1, P1_BLOCKS, mke2fs, debugfs, add_dev_nodes=True)

    # ---- partition 2: GUI assets ----
    log("Building ext4 GUI partition (partition 2)…")
    gui = BUILD / "gui"
    gui.mkdir(exist_ok=True)
    with tarfile.open(BUILD / "gui.tar.gz") as t:
        _extractall(t, gui)
    p2 = BUILD / "p2.ext4"
    build_ext4(gui, p2, P2_BLOCKS, mke2fs, debugfs, add_dev_nodes=False)

    # ---- env + partition table + assemble ----
    log("Writing env blob, partition table and assembling the image…")
    env_blob = make_env_blob(ENV_ENTRIES, ENV_SIZE)
    mbr = make_mbr([
        (P1_START, P1_SECTORS, 0x83),       # Linux
        (P2_START, P2_SECTORS, 0x83),
    ])
    out = BUILD / "nand.img"
    assemble_image(out, env_blob, mbr, p1, p2)

    final = Q / "nand.img"
    shutil.move(str(out), str(final))
    shutil.rmtree(BUILD)
    log(f"built {final.relative_to(REPO)}  ->  qemu/watch.sh   (boots to the UI)")


if __name__ == "__main__":
    main()
