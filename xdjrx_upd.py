#!/usr/bin/env python3
"""
xdjrx_upd.py — decrypt / inspect / repack Pioneer XDJ-RX firmware update files (.UPD)

The XDJ-RX (XDJAERO i.MX6 platform) distributes firmware as an encrypted
ISO9660 image produced by Linux **cryptoloop** (`losetup -e aes`). This tool
reproduces that transform on a PC.

Container layout
----------------
    [ encrypted body : N * 512 bytes ]      cryptoloop AES-256-CBC, per-sector
    [ model string   : 12 bytes      ]      e.g. b"XDJAERO2.21\0"
    [ body CRC32      : 4 bytes, LE   ]      zlib.crc32 of the encrypted body

Crypto (verified against the GPL kernel source, linux-3.0.35
drivers/block/cryptoloop.c + loop.c, and util-linux-ng-2.13 mount/lomount.c):
    cipher/mode : cbc(aes)                      (AES-256, 32-byte key)
    key         : first line of aes256.key, bytes [0:31] + one 0x00 byte
                  (util-linux xstrncpy copies LO_KEY_SIZE-1 bytes, then NUL;
                   LO_KEY_SIZE = 32)
    IV/sector   : each 512-byte sector i uses IV = LE32(i) || 12 * 0x00
                  (i = absolute 512-byte offset from the start of the body)

The key ships inside Pioneer's own GPL source release for the XDJ-RX
(pioneer_xdj_rx/initramfs.tar.gz -> initramfs/usr/local/pioneer/aes256.key).
This is interoperability/preservation work on hardware you own.

Usage
-----
    python3 xdjrx_upd.py decrypt XDJRX.UPD -o XDJRX_2.21.iso
    python3 xdjrx_upd.py info    XDJRX.UPD
    python3 xdjrx_upd.py extract XDJRX.UPD -d firmware/contents   # needs 7z/bsdtar or macOS
    python3 xdjrx_upd.py pack    modified.iso -o XDJRX_new.UPD --model XDJAERO2.21

    # If you have the raw aes256.key passphrase file instead of the built-in key:
    python3 xdjrx_upd.py decrypt XDJRX.UPD --key-file aes256.key -o out.iso

Requires: pycryptodome  (pip install pycryptodome)   [or 'cryptography']
"""
import argparse, struct, sys, zlib, subprocess, shutil, os

SECTOR = 512
ENV_VAR = "XDJRX_AES_KEY"   # 64 hex chars (32-byte AES-256 key) in environment or .env


# ---- AES backend (pycryptodome preferred, cryptography fallback) ------------
def _aes_cbc_decrypt(key, iv, ct):
    try:
        from Crypto.Cipher import AES
        return AES.new(key, AES.MODE_CBC, iv).decrypt(ct)
    except ImportError:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        d = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        return d.update(ct) + d.finalize()


def _aes_cbc_encrypt(key, iv, pt):
    try:
        from Crypto.Cipher import AES
        return AES.new(key, AES.MODE_CBC, iv).encrypt(pt)
    except ImportError:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        e = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        return e.update(pt) + e.finalize()


def _iv(sector_index):
    return struct.pack("<I", sector_index & 0xFFFFFFFF) + b"\x00" * 12


def derive_key_from_passphrase_file(path):
    """Replicate util-linux losetup -e aes key setup: first line, [0:31] + NUL."""
    with open(path, "rb") as f:
        raw = f.read()
    line = raw.split(b"\n", 1)[0]          # xgetpass stops at '\n'
    key = (line[:31]).ljust(31, b"\x00") + b"\x00"   # xstrncpy: 31 bytes + NUL
    assert len(key) == 32, len(key)
    return key


def load_dotenv(path=None):
    """Minimal .env reader (no dependency). Searches the given path, then the
    script directory, then the CWD. Returns a dict of KEY->value."""
    candidates = [path] if path else []
    candidates += [os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
                   os.path.join(os.getcwd(), ".env")]
    out = {}
    for p in candidates:
        if p and os.path.isfile(p):
            for line in open(p, encoding="utf-8"):
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
            break
    return out


def _load_key(args):
    # 1) explicit passphrase file wins
    if getattr(args, "key_file", None):
        return derive_key_from_passphrase_file(args.key_file)
    # 2) environment variable, then .env file
    val = os.environ.get(ENV_VAR) or load_dotenv().get(ENV_VAR)
    if val:
        try:
            key = bytes.fromhex(val)
        except ValueError:
            sys.exit(f"error: {ENV_VAR} must be hex (64 chars)")
        if len(key) != 32:
            sys.exit(f"error: {ENV_VAR} must be 32 bytes (64 hex chars), got {len(key)}")
        return key
    sys.exit(
        f"error: no key found.\n"
        f"  Provide it one of these ways:\n"
        f"    • python3 fetch_key.py          # writes .env from Pioneer's GPL source\n"
        f"    • export {ENV_VAR}=<64 hex>\n"
        f"    • --key-file aes256.key         # derive from a passphrase file")


# ---- core -------------------------------------------------------------------
def read_upd_bytes(path):
    """Return (upd_bytes, stem) for a .UPD file OR a Pioneer .zip containing one.

    `stem` is a sensible base name for default output (the .UPD member's name
    without extension). Handles standard-deflate zips via zipfile, and falls
    back to deflate64 (method 9) the way the GPL source parts need."""
    with open(path, "rb") as f:
        head = f.read(4)
    if head[:2] != b"PK":
        with open(path, "rb") as f:
            return f.read(), os.path.splitext(os.path.basename(path))[0]

    import zipfile, struct
    zf = zipfile.ZipFile(path)
    members = [i for i in zf.infolist()
               if not i.is_dir() and i.filename.lower().endswith(".upd")]
    if not members:
        members = [i for i in zf.infolist() if not i.is_dir()]  # fall back: any file
    if not members:
        sys.exit(f"error: no .UPD found inside {path}")
    if len(members) > 1:
        names = ", ".join(m.filename for m in members)
        sys.exit(f"error: multiple candidate files in {path}: {names}")
    info = members[0]
    stem = os.path.splitext(os.path.basename(info.filename))[0]
    try:
        return zf.read(info), stem
    except NotImplementedError:
        pass  # deflate64 (method 9)
    with open(path, "rb") as f:
        f.seek(info.header_offset)
        assert f.read(4) == b"PK\x03\x04"
        fx = struct.unpack("<HHHHHIIIHH", f.read(26))
        nlen, elen = fx[-2], fx[-1]
        f.seek(info.header_offset + 30 + nlen + elen)
        comp = f.read(info.compress_size)
    if info.compress_type == 0:                       # stored
        return comp, stem
    try:
        import inflate64
    except ImportError:
        sys.exit("error: .UPD is deflate64-compressed inside the zip; "
                 "pip install inflate64")
    return inflate64.Inflater().inflate(comp), stem


def split_container(data):
    if len(data) < 16 or (len(data) - 16) % SECTOR != 0:
        sys.exit("error: not an XDJ .UPD (size-16 must be a multiple of 512)")
    body = data[:-16]
    model = data[-16:-4].split(b"\x00", 1)[0].decode("latin1")
    crc = struct.unpack("<I", data[-4:])[0]
    return body, model, crc


def decrypt_body(body, key):
    out = bytearray(len(body))
    for i in range(len(body) // SECTOR):
        off = i * SECTOR
        out[off:off + SECTOR] = _aes_cbc_decrypt(key, _iv(i), body[off:off + SECTOR])
    return bytes(out)


def encrypt_body(plain, key):
    if len(plain) % SECTOR != 0:
        plain = plain + b"\x00" * (SECTOR - len(plain) % SECTOR)
    out = bytearray(len(plain))
    for i in range(len(plain) // SECTOR):
        off = i * SECTOR
        out[off:off + SECTOR] = _aes_cbc_encrypt(key, _iv(i), plain[off:off + SECTOR])
    return bytes(out)


def _check_iso(plain):
    pvd = plain[0x8000:0x8006]                 # 2048-byte logical sector 16
    return pvd[1:6] == b"CD001", pvd


# ---- commands ---------------------------------------------------------------
def cmd_info(args):
    data, _ = read_upd_bytes(args.upd)
    body, model, crc = split_container(data)
    calc = zlib.crc32(body) & 0xFFFFFFFF
    print(f"file        : {args.upd}")
    print(f"size        : {len(data)} bytes ({len(body)//SECTOR} sectors + 16-byte footer)")
    print(f"model string: {model!r}")
    print(f"footer CRC32: {crc:08x}  (crc32(body)={calc:08x}) -> {'OK' if crc==calc else 'MISMATCH'}")
    key = _load_key(args)
    s16 = _aes_cbc_decrypt(key, _iv(64), body[64*SECTOR:65*SECTOR])  # logical sector 16 = byte 0x8000
    print(f"key check   : ISO9660 PVD magic {'FOUND (CD001)' if s16[1:6]==b'CD001' else 'not found'}")


def cmd_decrypt(args):
    data, stem = read_upd_bytes(args.upd)
    body, model, crc = split_container(data)
    calc = zlib.crc32(body) & 0xFFFFFFFF
    if crc != calc:
        print(f"warning: footer CRC {crc:08x} != crc32(body) {calc:08x}", file=sys.stderr)
    key = _load_key(args)
    plain = decrypt_body(body, key)
    ok, pvd = _check_iso(plain)
    if not ok:
        sys.exit(f"error: wrong key? ISO9660 'CD001' not found at 0x8000 (got {pvd.hex()})")
    out = args.output or (stem + ".iso")
    open(out, "wb").write(plain)
    print(f"model {model!r}  ->  {out}  ({len(plain)} bytes, ISO9660 verified)")
    return out


def cmd_pack(args):
    plain = open(args.iso, "rb").read()
    key = _load_key(args)
    body = encrypt_body(plain, key)
    model = args.model.encode("latin1")[:12].ljust(12, b"\x00")
    footer = model + struct.pack("<I", zlib.crc32(body) & 0xFFFFFFFF)
    out = args.output or (os.path.splitext(args.iso)[0] + ".UPD")
    open(out, "wb").write(body + footer)
    print(f"packed {args.iso} ({len(plain)} bytes) -> {out}  model={args.model}")


def cmd_extract(args):
    iso = cmd_decrypt(args)
    dest = args.dest or "contents"
    os.makedirs(dest, exist_ok=True)
    if shutil.which("7z"):
        subprocess.run(["7z", "x", "-y", f"-o{dest}", iso], check=True)
    elif shutil.which("bsdtar"):
        subprocess.run(["bsdtar", "-xf", iso, "-C", dest], check=True)
    elif sys.platform == "darwin":
        mnt = os.path.abspath(dest + "_mnt")
        subprocess.run(["hdiutil", "attach", "-nobrowse", "-readonly",
                        "-mountpoint", mnt, iso], check=True)
        subprocess.run(["cp", "-a", mnt + "/.", dest], check=True)
        subprocess.run(["hdiutil", "detach", mnt], check=True)
    else:
        sys.exit("need '7z' or 'bsdtar' to extract the ISO (brew install p7zip / libarchive)")
    print(f"extracted ISO contents -> {dest}/")


def main():
    ap = argparse.ArgumentParser(description="Pioneer XDJ-RX .UPD firmware tool")
    ap.add_argument("--key-file", help="aes256.key passphrase file (else built-in key)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    UPD_HELP = ".UPD file, or a Pioneer .zip containing one"

    p = sub.add_parser("info", help="print container metadata and verify key/CRC")
    p.add_argument("upd", help=UPD_HELP); p.set_defaults(func=cmd_info)

    p = sub.add_parser("decrypt", help=".UPD/.zip -> decrypted .iso")
    p.add_argument("upd", help=UPD_HELP); p.add_argument("-o", "--output")
    p.set_defaults(func=cmd_decrypt)

    p = sub.add_parser("extract", help=".UPD/.zip -> decrypted .iso + unpacked tree")
    p.add_argument("upd", help=UPD_HELP); p.add_argument("-o", "--output")
    p.add_argument("-d", "--dest", help="output directory for ISO contents")
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("pack", help="(modified) .iso -> .UPD  (re-encrypt + footer)")
    p.add_argument("iso"); p.add_argument("-o", "--output")
    p.add_argument("--model", default="XDJAERO2.21", help="footer model string")
    p.set_defaults(func=cmd_pack)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
