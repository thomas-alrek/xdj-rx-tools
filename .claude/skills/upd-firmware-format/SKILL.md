---
name: upd-firmware-format
description: Decrypt, inspect, extract, and repack Pioneer XDJ-RX (.UPD) firmware update files, and understand the container + cryptoloop AES scheme and the AES key provenance. Use when working with a .UPD/.zip firmware image, extracting the rootfs/kernel/rbp from official firmware, building a custom firmware image, or reasoning about how rbp/the kernel are packaged and signed/encrypted.
---

# XDJ-RX `.UPD` firmware format

The XDJ-RX (XDJAERO / i.MX6 platform) ships firmware as an **encrypted ISO9660** image.
Tool: **`xdjrx_upd.py`** (repo root). Key fetch: **`fetch_key.py`**.

## Container layout
```
[ encrypted body : N × 512 bytes ]   # cryptoloop AES-256-CBC, per-sector
[ model string   : 12 bytes      ]   # e.g. b"XDJAERO2.21\0"
[ body CRC32      : 4 bytes LE    ]   # zlib.crc32 of the ENCRYPTED body
```
`size − 16` is always a multiple of 512. The body decrypts to a plain ISO9660 (PVD magic
`CD001` at byte `0x8000` = 2048-byte logical sector 16) — that's the key/decrypt sanity check.

## Crypto (Linux cryptoloop `losetup -e aes`, verified vs linux-3.0.35 + util-linux-ng-2.13)
- cipher/mode: **`cbc(aes)`**, AES-**256** (32-byte key).
- **Per-512-byte sector**: sector `i` (absolute 512-byte offset from body start) uses
  **IV = `LE32(i) || 12×0x00`**; each sector is an independent CBC unit.
- **Key derivation** from the passphrase file: first line, bytes `[0:31]` + one `0x00`
  (util-linux `xstrncpy` copies `LO_KEY_SIZE-1`=31 bytes then NUL; total 32).

## The AES key (not in this repo)
Ships inside Pioneer's own **GPL/LGPL source release**:
`pioneer_xdj_rx.tar.gz → initramfs.tar.gz → initramfs/usr/local/pioneer/aes256.key`.
- `python3 fetch_key.py` resolves it: uses `./aes256.key` if present, else downloads the GPL
  source parts (deflate64 zips, ~280 MB), streams the nested tars, pulls `aes256.key`, derives
  the 32-byte key, and writes `./.env` as `XDJRX_AES_KEY=<64 hex>`. `--version 1.50|1.40|1.00`.
- `xdjrx_upd.py` reads the key from `--key-file`, then `$XDJRX_AES_KEY`, then `.env`.
- **Both `.env` and `aes256.key` are git-ignored** — never commit Pioneer-owned material.
  This is interoperability/preservation on owned hardware.

## Commands
```
python3 xdjrx_upd.py info    XDJRX.UPD                 # model, size, CRC check, key/ISO check
python3 xdjrx_upd.py decrypt XDJRX.UPD -o build/x.iso  # -> decrypted ISO9660
python3 xdjrx_upd.py extract XDJRX.UPD -d build/contents# decrypt + unpack (pycdlib / 7z / bsdtar / hdiutil)
python3 xdjrx_upd.py pack    modified.iso -o NEW.UPD --model XDJAERO2.21  # re-encrypt + footer
```
Accepts a `.UPD` directly or a Pioneer `.zip` containing one (handles deflate64 members).
Output defaults land in `build/` (git-ignored).

## What's inside (for the emulator)
The ISO holds the device rootfs/update payload — the Linux kernel, the initramfs, and the
main application **`rbp`** (ARM32 ELF, the thing the emulator boots and this project reverse-
engineers). Extract it to get a fresh `rbp`/kernel; `build/extract/rbp` is the analyzed copy.

## Building a custom firmware
`decrypt`/`extract` → modify the ISO tree → rebuild an ISO9660 (preserve Rock Ridge
names/symlinks) → `pack` with the correct `--model`. The device verifies the footer CRC32
(over the *encrypted* body) and the model string; keep both consistent. **Do not patch `rbp`
in the emulator workflow** — custom firmware is tested by booting it, so keep stock `rbp`
runnable (see the project's standing constraint).

## Gotchas
- CRC32 is over the **encrypted** body, not the plaintext ISO.
- Wrong key → no `CD001` at `0x8000`; `info`/`decrypt` report it explicitly.
- Model string is 12 bytes, NUL-padded; `pack` truncates/pads to 12.
- Sector IV index is the **absolute** 512-byte offset, so `info` checks logical sector 16 by
  decrypting body sector **64** (0x8000/512) with `IV=LE32(64)`.

## References
`xdjrx_upd.py`, `fetch_key.py` (repo root), `README.md`, `QEMU.md`. Related skills:
[rbp-binary-internals], [run-xdj-emulator].
