#!/usr/bin/env python3
# /// script
# requires-python = ">=3.8"
# dependencies = ["pycdlib>=1.14"]
# ///
"""
qemu_prepare.py — build everything needed to boot the UNMODIFIED XDJ-RX kernel
in QEMU (qemu-system-arm -M sabrelite).

It produces, under build/qemu/:
  uImage        kernel image, extracted from the decrypted firmware ISO (as-is)
  vmlinux.bin   the gzip payload inside the zImage, decompressed (the raw kernel)
  tramp.bin     tiny ARM trampoline: sets r0=0, r1=3769 (MACH_TYPE sabrelite),
                r2=ATAGs, then jumps to the kernel entry
  atags.bin     hand-built ATAG list (CORE + MEM 1GB@0x10000000 + CMDLINE + NONE)

Why the trampoline + manual boot?  QEMU's 'sabrelite' is a device-tree machine;
it passes board-id -1.  The firmware's Freescale 3.0.35 kernel is ATAG-based and
rejects an unknown machine id (it spins in dump_machine_table()).  It *does*
support machine id 3769 ("Freescale i.MX 6Quad Sabre-Lite Board").  The
trampoline supplies that id and ATAGs without modifying the firmware at all.

Usage:
  python3 qemu_prepare.py            # uses build/XDJRX_2.21.iso (from xdjrx_upd.py)
  python3 qemu_prepare.py path/to.iso
Then:
  ./qemu_run.sh
"""
import gzip, os, struct, sys, zlib

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "build", "qemu")
KERNEL_ENTRY = 0x10008000          # uImage load/entry (i.MX6 DRAM base 0x10000000 + 0x8000)
ATAGS_ADDR   = 0x10000100
TRAMP_ADDR   = 0x10000000
MACH_TYPE_SABRELITE = 3769         # 0x0EB9, present in the firmware kernel's machine table
RAM_BASE = 0x10000000
RAM_SIZE = 0x40000000              # 1 GiB
# ttymxc1 = UART2 = the port QEMU exposes; keep_bootcon keeps the working polled
# early console alive (the full imx-uart driver's TX doesn't reach QEMU's chardev).
CMDLINE = "console=ttymxc1,115200 earlyprintk keep_bootcon"


def extract_uimage(iso_path):
    import pycdlib
    iso = pycdlib.PyCdlib(); iso.open(iso_path)
    pathkw = "rr_path" if iso.has_rock_ridge() else "iso_path"
    dst = os.path.join(OUT, "uImage")
    with open(dst, "wb") as f:
        iso.get_file_from_iso_fp(f, **{pathkw: "/images/uImage"})
    iso.close()
    return dst


def decompress_kernel(uimage_path):
    img = open(uimage_path, "rb").read()[64:]   # strip 64-byte U-Boot header
    i = img.find(b"\x1f\x8b\x08")               # gzip magic (zImage payload)
    if i < 0:
        sys.exit("no gzip payload found in uImage (unexpected)")
    try:
        raw = gzip.decompress(img[i:])
    except Exception:
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        raw = d.decompress(img[i:])
    open(os.path.join(OUT, "vmlinux.bin"), "wb").write(raw)
    return len(raw)


def build_trampoline():
    # ARM: mov r0,#0 ; ldr r1,[pc,#8] ; ldr r2,[pc,#8] ; ldr pc,[pc,#8] ; pad ; id ; atags ; entry
    words = [0xe3a00000, 0xe59f1008, 0xe59f2008, 0xe59ff008,
             0x00000000, MACH_TYPE_SABRELITE, ATAGS_ADDR, KERNEL_ENTRY]
    open(os.path.join(OUT, "tramp.bin"), "wb").write(
        b"".join(struct.pack("<I", w) for w in words))


def build_atags():
    def w(*v): return b"".join(struct.pack("<I", x) for x in v)
    cmd = CMDLINE.encode() + b"\x00"
    cmd += b"\x00" * ((-len(cmd)) % 4)
    atags  = w(5, 0x54410001, 0, 0x1000, 0)                 # ATAG_CORE
    atags += w(4, 0x54410002, RAM_SIZE, RAM_BASE)           # ATAG_MEM
    atags += w(2 + len(cmd) // 4, 0x54410009) + cmd         # ATAG_CMDLINE
    atags += w(0, 0)                                        # ATAG_NONE
    open(os.path.join(OUT, "atags.bin"), "wb").write(atags)


def main():
    os.makedirs(OUT, exist_ok=True)
    iso = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "build", "XDJRX_2.21.iso")
    if not os.path.isfile(iso):
        sys.exit(f"ISO not found: {iso}\n  run: python3 xdjrx_upd.py decrypt XDJRX.UPD -o build/XDJRX_2.21.iso")
    print(f"extracting uImage from {iso} ...")
    uimg = extract_uimage(iso)
    n = decompress_kernel(uimg)
    print(f"  decompressed kernel: {n} bytes -> build/qemu/vmlinux.bin")
    build_trampoline(); build_atags()
    print(f"  trampoline (r1={MACH_TYPE_SABRELITE}) + ATAGs ('{CMDLINE}') written")
    print("ready -> ./qemu_run.sh")


if __name__ == "__main__":
    main()
