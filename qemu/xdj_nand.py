#!/usr/bin/env python3
"""
xdj_nand.py -- host-side tool for the emulated XDJ-RX NAND image (qemu/xdj/xdj-nand.c).

The image ($XDJ_NAND_IMG) is the raw flash: 131072 pages of 2048+64 bytes (page-major,
OOB included), exactly what the emulated chip stores. Pages are in the i.MX6 GPMI/BCH
layout the kernel's gpmi-nand driver produces, which this tool mirrors so the host can
read and write *logical* (MTD) data:

  raw page = [10-byte metadata][512 data0][13 ecc0][512 data1][13 ecc1]...[512 data3]
             [13 ecc3][2 spare]          (BCH t=8, GF13, 4 chunks, bit-packed LSB first)
  block-mark swap: logical byte 1999 <-> metadata[0] (so raw byte 2048, the factory
  bad-block mark, holds the OOB's first byte).  Parity bytes are zero (the emulator
  never checks them); an erased (all-0xff) page reads back as 0xff.

Commands
  init <img> [--iso XDJRX.iso | --env fw_env.txt]
        create an erased 256 MiB image and seed the u-boot env partition (mtd2) with
        the firmware's own images/fw_env.txt -- the env a factory-fresh unit carries
        (u-boot's default env, saved). The firmware updater then installs everything
        else (see docs/update-path-plan.md).
  env <img>                       print the NAND u-boot env
  boot <img> <outdir>             do what u-boot's bootcmd_nand does: pick kernel_bank,
                                  read that bank's uImage from NAND, and write
                                  vmlinux.bin + atags.bin (bootargs from the env) +
                                  tramp.bin for the QEMU loader
  info <img>                      partitions, env summary, which banks hold a uImage
"""
import argparse
import mmap
import os
import re
import struct
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

PAGE, OOB = 2048, 64
RAW = PAGE + OOB
PPB, BLOCKS = 64, 2048
PAGES = PPB * BLOCKS
SIZE = PAGES * RAW
ERASE = PAGE * PPB

# BCH geometry as gpmi-nand computes it for 2048+64 (common_nfc_set_geometry)
META, CHUNK, NCHUNK, GF = 10, 512, 4, 13
STRENGTH = ((OOB - META) * 8 // (GF * NCHUNK)) & ~1          # 8
ECC_BITS = STRENGTH * GF                                      # 104
BBM_BIT = PAGE * 8 - (ECC_BITS * (NCHUNK - 1) + META * 8)     # 15992
BBM_BYTE, BBM_SHIFT = divmod(BBM_BIT, 8)                      # 1999, 0

ENV_PART, ENV_SIZE = "env", 0x20000                           # fw_env.config NAND line


def _layout():
    """(raw bit offset, length bits) of the metadata and each data chunk."""
    pos, data = META * 8, []
    for _ in range(NCHUNK):
        data.append(pos)
        pos += CHUNK * 8 + ECC_BITS
    return data


DATA_POS = _layout()
assert all(p % 8 == 0 for p in DATA_POS), "only the byte-aligned layout is implemented"


def raw_to_page(raw):
    """Logical page (2048 bytes) + metadata from a raw page, as gpmi_ecc_read_page."""
    if raw.count(0xff) == len(raw):
        return b"\xff" * PAGE
    pay = bytearray()
    for p in DATA_POS:
        pay += raw[p // 8:p // 8 + CHUNK]
    meta = bytearray(raw[:META])
    # undo the block-mark swap (BBM_SHIFT == 0 for this geometry)
    pay[BBM_BYTE], meta[0] = meta[0], pay[BBM_BYTE]
    return bytes(pay)


def page_to_raw(data, oob0=0xff):
    """Raw page for logical data, as gpmi_ecc_write_page + the BCH encoder (zero parity)."""
    pay = bytearray(data.ljust(PAGE, b"\xff"))
    meta = bytearray(b"\xff" * META)
    meta[0], pay[BBM_BYTE] = pay[BBM_BYTE], oob0
    raw = bytearray(b"\xff" * RAW)
    raw[:META] = meta
    for i, p in enumerate(DATA_POS):
        raw[p // 8:p // 8 + CHUNK] = pay[i * CHUNK:(i + 1) * CHUNK]
        raw[p // 8 + CHUNK:p // 8 + CHUNK + ECC_BITS // 8] = b"\x00" * (ECC_BITS // 8)
    return bytes(raw)


class Nand:
    def __init__(self, path, create=False):
        if create or not os.path.exists(path):
            with open(path, "wb") as f:
                f.truncate(SIZE)
            self._fresh = True
        else:
            self._fresh = False
        if os.path.getsize(path) < SIZE:
            sys.exit(f"{path}: not a {SIZE}-byte XDJ NAND image")
        self.f = open(path, "r+b")
        self.m = mmap.mmap(self.f.fileno(), SIZE)
        if self._fresh:
            self.m[:] = b"\xff" * SIZE

    def raw(self, page):
        return self.m[page * RAW:(page + 1) * RAW]

    def read(self, page):
        return raw_to_page(self.raw(page))

    def bad(self, block):
        return self.m[block * PPB * RAW + PAGE] != 0xff

    def erase(self, block):
        self.m[block * PPB * RAW:(block + 1) * PPB * RAW] = b"\xff" * (PPB * RAW)

    def program(self, page, data):
        new = page_to_raw(data)
        old = self.raw(page)
        self.m[page * RAW:(page + 1) * RAW] = bytes(a & b for a, b in zip(old, new))

    def read_range(self, off, length, skipbad=True):
        """Like u-boot `nand read` / nanddump --bb=skipbad: logical bytes, bad blocks skipped."""
        out, block = bytearray(), off // ERASE
        skip = off % ERASE
        while len(out) < length and block < BLOCKS:
            if skipbad and self.bad(block):
                block += 1
                continue
            for pg in range(PPB):
                out += self.read(block * PPB + pg)
            block += 1
        return bytes(out[skip:skip + length])

    def write_range(self, off, data):
        """Erase the covered blocks and program `data` at a block-aligned offset."""
        assert off % ERASE == 0
        for i in range(0, len(data), ERASE):
            block = (off + i) // ERASE
            self.erase(block)
            chunk = data[i:i + ERASE]
            for pg in range((len(chunk) + PAGE - 1) // PAGE):
                self.program(block * PPB + pg, chunk[pg * PAGE:(pg + 1) * PAGE])

    def close(self):
        self.m.flush()
        self.m.close()
        self.f.close()


# ---- partitions / env ---------------------------------------------------------------

def parse_mtdparts(s):
    """'gpmi-nand:3M(boot),...,-@0x0(all)' -> [(name, offset, size)] like cmdlinepart."""
    s = s.split(":", 1)[1] if ":" in s.split("(", 1)[0] else s
    parts, off = [], 0
    for item in s.split(","):
        m = re.match(r"(-|[0-9a-fx]+[kKmMgG]?)(?:@([0-9a-fx]+[kKmMgG]?))?\(([^)]*)\)", item)
        if not m:
            continue
        def num(t):
            mul = {"k": 1 << 10, "m": 1 << 20, "g": 1 << 30}.get(t[-1].lower(), 1)
            return int(t[:-1] if mul > 1 else t, 0) * mul
        if m.group(2):
            off = num(m.group(2))
        size = PAGE * PAGES - off if m.group(1) == "-" else num(m.group(1))
        parts.append((m.group(3), off, size))
        off += size
    return parts


def env_blob(entries, size=ENV_SIZE):
    data = ("\x00".join(entries) + "\x00\x00").encode("latin1")
    payload = data.ljust(size - 4, b"\xff")
    return struct.pack("<I", zlib.crc32(payload) & 0xffffffff) + payload


def read_env(nand, parts):
    off = dict((n, o) for n, o, _ in parts)[ENV_PART]
    blob = nand.read_range(off, ENV_SIZE)
    crc, payload = struct.unpack("<I", blob[:4])[0], blob[4:]
    if zlib.crc32(payload) & 0xffffffff != crc:
        return None
    env = {}
    for e in payload.split(b"\x00"):
        if not e:
            break
        k, _, v = e.decode("latin1").partition("=")
        env[k] = v
    return env


def fw_env_from(args):
    if args.env:
        lines = open(args.env, encoding="latin1").read().splitlines()
    else:
        import io
        import pycdlib
        iso = pycdlib.PyCdlib()
        iso.open(args.iso)
        kw = "rr_path" if iso.has_rock_ridge() else "iso_path"
        buf = io.BytesIO()
        iso.get_file_from_iso_fp(buf, **{kw: "/images/fw_env.txt"})
        iso.close()
        lines = buf.getvalue().decode("latin1").splitlines()
    return [l.strip() for l in lines if "=" in l]


def default_parts():
    import qemu_prepare
    return parse_mtdparts(qemu_prepare.MTDPARTS_FALLBACK)


def env_parts(env):
    if env and env.get("mtdids") and env.get("partition"):
        return parse_mtdparts(env["mtdids"] + ":" + env["partition"])
    return default_parts()


def expand(s, env):
    return re.sub(r"\$\{(\w+)\}", lambda m: env.get(m.group(1), ""), s)


def uboot_bootargs(env):
    """bootargs as `run bootargs_base bootargs_nand` builds them."""
    env = dict(env)
    for var in ("bootargs_base", "bootargs_nand"):
        cmd = env.get(var, "")
        if cmd.startswith("setenv bootargs "):
            env["bootargs"] = expand(cmd[len("setenv bootargs "):], env)
    return env.get("bootargs", "")


def emulator_cmdline(bootargs):
    """The env's bootargs plus what the emulator's display/console need (qemu_prepare.CMDLINE):
    the real u-boot's bare `video=mxcfb0` is replaced by the emulator's LVDS mode line."""
    import qemu_prepare
    emu = qemu_prepare.CMDLINE.split()
    keep = [a for a in bootargs.split() if not a.startswith(("video=", "console="))]
    return " ".join(emu[:3] + keep + [a for a in emu if a.startswith("video=")])


# ---- commands ---------------------------------------------------------------------

def cmd_init(args):
    entries = fw_env_from(args)
    nand = Nand(args.image, create=True)
    env = dict(e.split("=", 1) for e in entries)
    parts = env_parts(env)
    off = dict((n, o) for n, o, _ in parts)[ENV_PART]
    nand.write_range(off, env_blob(entries))
    nand.close()
    print(f"{args.image}: erased 256 MiB NAND, env ({len(entries)} vars) at mtd2 "
          f"0x{off:x}; kernel_bank={env.get('kernel_bank')} rootfs={env.get('rootfs')}")


def cmd_env(args):
    nand = Nand(args.image)
    env = read_env(nand, default_parts())
    if env is None:
        sys.exit("env: bad CRC (blank or corrupt env partition)")
    for k, v in env.items():
        print(f"{k}={v}")


def bank_range(env, bank):
    """(offset, length) from bootargs_nand_<bank> = 'nand read ${loadaddr} <off> <len>'."""
    m = re.search(r"nand read \S+ (0x[0-9a-fA-F]+|\d+) (0x[0-9a-fA-F]+|\d+)",
                  env.get(f"bootargs_nand_{bank}", ""))
    if not m:
        sys.exit(f"env has no usable bootargs_nand_{bank}")
    return int(m.group(1), 0), int(m.group(2), 0)


def read_uimage(nand, off, length):
    img = nand.read_range(off, length)
    magic, _, _, size = struct.unpack(">IIII", img[:16])
    if magic != 0x27051956:
        return None
    return img[:64 + size]


def cmd_boot(args):
    import qemu_prepare
    nand = Nand(args.image)
    env = read_env(nand, default_parts())
    if env is None:
        sys.exit("boot: no valid u-boot env in the NAND image (run `init` first)")
    bank = env.get("kernel_bank", "A")
    off, length = bank_range(env, bank)
    uimg = read_uimage(nand, off, length)
    if uimg is None:
        sys.exit(f"boot: kernel bank {bank} (0x{off:x}) holds no uImage -- install a firmware")
    os.makedirs(args.outdir, exist_ok=True)
    up = os.path.join(args.outdir, "uImage")
    open(up, "wb").write(uimg)
    n = qemu_prepare.decompress_kernel(up, out=args.outdir)
    cmdline = emulator_cmdline(uboot_bootargs(env))
    qemu_prepare.build_boot_blobs(out=args.outdir, cmdline=cmdline)
    nand.close()
    print(f"kernel bank {bank} @0x{off:x}: uImage {len(uimg)} bytes -> vmlinux {n} bytes")
    print(f"cmdline: {cmdline}")


def cmd_info(args):
    nand = Nand(args.image)
    env = read_env(nand, default_parts())
    for i, (n, o, s) in enumerate(env_parts(env)):
        print(f"mtd{i:<2} 0x{o:08x} 0x{s:08x} {n}")
    if env is None:
        print("env: blank/invalid")
        return
    print("env: " + ", ".join(f"{k}={env.get(k)}" for k in
                              ("rootfs", "kernel_bank", "boot_type", "update_status",
                               "release", "ubifs_formatted")))
    for bank in ("A", "B"):
        try:
            off, length = bank_range(env, bank)
        except SystemExit:
            continue
        u = read_uimage(nand, off, length)
        name = u[32:64].split(b"\0")[0].decode("latin1") if u else "-"
        print(f"kernel {bank} @0x{off:x}: {len(u) if u else 'empty'} {name}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("init")
    p.add_argument("image")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--iso")
    g.add_argument("--env")
    p.set_defaults(fn=cmd_init)
    p = sp.add_parser("env"); p.add_argument("image"); p.set_defaults(fn=cmd_env)
    p = sp.add_parser("boot"); p.add_argument("image"); p.add_argument("outdir")
    p.set_defaults(fn=cmd_boot)
    p = sp.add_parser("info"); p.add_argument("image"); p.set_defaults(fn=cmd_info)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
