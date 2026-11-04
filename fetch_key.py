#!/usr/bin/env python3
"""
fetch_key.py — obtain the XDJ-RX firmware AES key and write it to .env

The key is not stored in this repo. It lives inside Pioneer's own publicly
published GPL/LGPL source release for the XDJ-RX, at:

    pioneer_xdj_rx.tar.gz
      └─ pioneer_xdj_rx/initramfs.tar.gz
           └─ initramfs/usr/local/pioneer/aes256.key

This script resolves the key with the least work possible:

  1. If ./aes256.key already exists  -> derive the key from it.          (instant)
  2. Else download the GPL source parts from Pioneer, unwrap the
     deflate64 ZIPs, stream just the two nested tars, pull aes256.key,
     derive the key, and cache ./aes256.key.                            (~280 MB)

The derived 32-byte AES-256 key is written to ./.env as:

    XDJRX_AES_KEY=<64 hex chars>

which xdjrx_upd.py reads. Nothing Pioneer-owned is committed: .env and
aes256.key are git-ignored.

    python3 fetch_key.py                 # resolve + write .env
    python3 fetch_key.py --force         # re-fetch even if .env has a key
    python3 fetch_key.py --version 1.40  # use a different GPL source version

Downloading/unwrapping needs:  pip install inflate64
"""
import argparse, io, os, sys, tarfile, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(HERE, ".env")
KEY_FILE = os.path.join(HERE, "aes256.key")
ENV_VAR = "XDJRX_AES_KEY"

# Pioneer "GPL/LGPL Source Code" page -> XDJ-RX. The downloads are deflate64
# ZIPs that wrap pioneer_xdj_rx.tar.gz.00 / .01 (concatenate to reassemble).
GPL_SOURCES = {
    "1.50": [
        "https://files.microcms-assets.io/assets/3b9e29ce734e49babfedb3f8d1e728e3/18acb46e94654aa1a0bb59265e67dcf9/E58B0262-B827-4181-A226-767EFC34A11E.zip",
        "https://files.microcms-assets.io/assets/3b9e29ce734e49babfedb3f8d1e728e3/3b9db487ff62463dae162b1e25c8c389/99510A92-C1F8-4290-935E-748361FF1782.zip",
    ],
    "1.40": [
        "https://files.microcms-assets.io/assets/3b9e29ce734e49babfedb3f8d1e728e3/5ff925cd2eb1426e92853c692f5cce93/2BA53441-0152-4E5F-8020-3837300F695B.zip",
        "https://files.microcms-assets.io/assets/3b9e29ce734e49babfedb3f8d1e728e3/58b50fb5b721444a864ea5afc0956900/0133CC65-46C5-4A53-B207-0EC4959FCABF.zip",
    ],
    "1.00": [
        "https://files.microcms-assets.io/assets/3b9e29ce734e49babfedb3f8d1e728e3/47c018d7dfcf450bbf38adc5fbe909a0/6640A1DE-47FA-476D-B9C7-A2206B15A4E3.zip",
        "https://files.microcms-assets.io/assets/3b9e29ce734e49babfedb3f8d1e728e3/4ce7a17e45d941d0b5d3590916a6c5a7/DFF28B9A-F227-4ABE-BBC9-F68BCE758737.zip",
    ],
}
INITRAMFS_MEMBER = "pioneer_xdj_rx/initramfs.tar.gz"
AES_KEY_MEMBER = "initramfs/usr/local/pioneer/aes256.key"


def derive_key(passphrase_bytes_firstline):
    """Reproduce util-linux losetup -e aes key setup: first line [0:31] + NUL."""
    line = passphrase_bytes_firstline.split(b"\n", 1)[0]
    key = line[:31].ljust(31, b"\x00") + b"\x00"   # xstrncpy: 31 bytes then NUL
    assert len(key) == 32
    return key


def _unwrap_deflate64_zip(zip_bytes):
    """Return the single stored member of a deflate64 ZIP (method 9)."""
    import zipfile, struct
    try:
        import inflate64
    except ImportError:
        sys.exit("error: need 'inflate64' to unwrap Pioneer's deflate64 ZIPs.\n"
                 "       pip install inflate64")
    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    info = zf.infolist()[0]
    f = io.BytesIO(zip_bytes)
    f.seek(info.header_offset)
    assert f.read(4) == b"PK\x03\x04"
    fx = struct.unpack("<HHHHHIIIHH", f.read(26))
    nlen, elen = fx[-2], fx[-1]
    f.seek(info.header_offset + 30 + nlen + elen)
    comp = f.read(info.compress_size)
    return inflate64.Inflater().inflate(comp)


def _download(url):
    print(f"  downloading {url.rsplit('/',1)[-1]} ...", flush=True)
    req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
    with urllib.request.urlopen(req) as r:
        return r.read()


def fetch_passphrase_from_gpl(version):
    urls = GPL_SOURCES[version]
    print(f"Fetching Pioneer XDJ-RX GPL source v{version} ({len(urls)} parts)...")
    tar_gz = b"".join(_unwrap_deflate64_zip(_download(u)) for u in urls)
    print(f"  reassembled pioneer_xdj_rx.tar.gz ({len(tar_gz)} bytes); extracting key...")
    with tarfile.open(fileobj=io.BytesIO(tar_gz), mode="r:gz") as outer:
        inner_gz = outer.extractfile(INITRAMFS_MEMBER).read()
    with tarfile.open(fileobj=io.BytesIO(inner_gz), mode="r:gz") as inner:
        passphrase = inner.extractfile(AES_KEY_MEMBER).read()
    return passphrase


def get_passphrase(version):
    if os.path.isfile(KEY_FILE):
        print(f"Using existing {os.path.relpath(KEY_FILE)} (delete it to re-download).")
        return open(KEY_FILE, "rb").read()
    passphrase = fetch_passphrase_from_gpl(version)
    with open(KEY_FILE, "wb") as f:
        f.write(passphrase)
    print(f"  cached passphrase -> {os.path.relpath(KEY_FILE)}")
    return passphrase


def write_env(key):
    lines, found = [], False
    if os.path.isfile(ENV_PATH):
        for line in open(ENV_PATH, encoding="utf-8"):
            if line.strip().startswith(ENV_VAR + "="):
                lines.append(f"{ENV_VAR}={key.hex()}\n"); found = True
            else:
                lines.append(line)
    if not found:
        lines.append(f"{ENV_VAR}={key.hex()}\n")
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.writelines(lines)


def main():
    ap = argparse.ArgumentParser(description="Resolve the XDJ-RX AES key into .env")
    ap.add_argument("--version", choices=sorted(GPL_SOURCES), default="1.50",
                    help="GPL source version to pull from (key is identical across all)")
    ap.add_argument("--force", action="store_true", help="refresh even if .env already has the key")
    args = ap.parse_args()

    if not args.force:
        existing = {}
        if os.path.isfile(ENV_PATH):
            for line in open(ENV_PATH, encoding="utf-8"):
                if "=" in line:
                    k, v = line.split("=", 1); existing[k.strip()] = v.strip()
        if existing.get(ENV_VAR):
            print(f".env already has {ENV_VAR}; nothing to do (use --force to refresh).")
            return

    key = derive_key(get_passphrase(args.version))
    write_env(key)
    print(f"\nWrote {ENV_VAR} to {os.path.relpath(ENV_PATH)}  ({key.hex()})")
    print("xdjrx_upd.py will now pick up the key automatically.")


if __name__ == "__main__":
    main()
