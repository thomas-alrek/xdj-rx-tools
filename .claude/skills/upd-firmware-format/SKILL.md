---
name: upd-firmware-format
description: Decrypt, inspect, extract, and repack Pioneer XDJ-RX (.UPD) firmware update files, and understand the container + cryptoloop AES scheme and the AES key provenance. Use when working with a .UPD/.zip firmware image, extracting the rootfs/kernel/rbp from official firmware, building a custom firmware image, or reasoning about how rbp/the kernel are packaged and signed/encrypted.
---

# XDJ-RX `.UPD` firmware format

The XDJ-RX (XDJAERO / i.MX6 platform) ships firmware as an **encrypted ISO9660** image.
Tool: **`xdj`** (Rust, `crates/xdj-fw`; `cargo build --release` there → `crates/xdj-fw/target/release/xdj`).
The older `xdjrx_upd.py` does decrypt/extract/pack only. Key fetch: **`fetch_key.py`**.

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
xdj fw info    XDJRX.UPD                       # model, size, CRC, key check, release.txt
xdj fw decrypt XDJRX.UPD -o build/x.iso        # -> decrypted ISO9660
xdj fw unpack  XDJRX.UPD build/unpacked        # ISO tree + rootfs/pioneer/gui/data/user payloads
xdj fw diff    XDJRX.UPD build/unpacked        # what an edited unpack tree changes
xdj fw repack  XDJRX.UPD build/unpacked --release 2.31 -o NEW.UPD  # edit-in-place -> new image
xdj fw build   XDJRX.UPD --overlay DIR --release 2.30 -o NEW.UPD   # custom firmware (below)
xdj fw pack    modified.iso -o NEW.UPD --model XDJAERO2.21          # raw re-encrypt + footer
xdj fw stick   NEW.UPD stick.img               # FAT16 USB stick with XDJRX.UPD (to install)
```
Accepts a `.UPD` directly or a Pioneer `.zip` containing one (handles deflate64 members).
Output defaults land in `build/` (git-ignored).

## What's inside (for the emulator)
The ISO holds the device rootfs/update payload — the Linux kernel, the initramfs, and the
main application **`rbp`** (ARM32 ELF, the thing the emulator boots and this project reverse-
engineers). Extract it to get a fresh `rbp`/kernel; `build/extract/rbp` is the analyzed copy.

### Update flow on the device (initramfs) — `docs/service-modes.md`
ISO top level: `update` (the ARM updater, not stripped), `mot2bin`, `images/` (`uImage`,
`rootfs.cramfs`, `pioneer.tar.gz`, `gui.tar.gz`, `data.tar.gz`, `u-boot.bin.nand`, `fw_env.txt`,
`PANELCOM.mot` = sub-ucom firmware, `subucom.rev` = **0.35**, `release.txt` = 2.21), `gui/`, `lib/`.
Update mode = initramfs `/init` (embedded cpio in the kernel, `070701` at vmlinux+0x25b64) sees
GPIO3_IO30 HIGH (sub-ucom update request, USB STOP + RELOOP at power-on) or `boot_type=update` →
D001 screen → `mount_nandupdate` (USB1, `XDJRX.UPD`) → `check_sig` → `decrypt_update` → `update`
(writes NAND MTDs, reflashes the sub-ucom over `/dev/subucom_spi1.0`). Emulator: `XDJ_BOOT_UPDATE=1`.

## Building a custom firmware (`xdj fw build`) — verified end to end 2026-11-15
How-to: skill **[custom-firmware]**; full write-up: `docs/custom-firmware.md`.
**There is no cryptographic signature.** The update's `check_sig` (initramfs, `check_sig.c` +
`CommonCrc32.c`) only checks the footer CRC32 and that the model string starts with `XDJAERO`;
it prints the rest as the version ("signature ok. version = 2.30"). The updater's own checks are
an md5 of each written partition read back vs its source file, and a version compare
("already updated target=%f, release=%f").

`xdj fw build BASE --overlay DIR --release X.YY -o OUT.UPD` starts from the official image (so
untouched entries keep their exact owners/modes/device nodes/symlinks) and applies an overlay:
`DIR/rootfs/...` → into `images/rootfs.cramfs` (mtd5), `DIR/pioneer/...` → `pioneer.tar.gz` (mtd7:
`rbp`, `apl_start`), `DIR/gui|data|user/...` → those tarballs, `DIR/iso/...` → raw image files (e.g.
`iso/images/uImage`). A file replaces (keeping mode/owner) or adds (host mode, root:root); a
`.xdj-delete` file in a part lists paths to remove. `--release` sets `images/release.txt` (installed
as the u-boot env `release`, shown by UTILITY → VERSION No. and test mode) and the footer model
`XDJAERO<release>`. Codecs: `crates/xdj-fw/src/{iso,cramfs,tarball}.rs` (round-trip tests:
`cargo test --release`, against `build/XDJRX.iso`).

End-to-end check: **`tools/fw/make-test-upd.sh`** builds `build/test-upd/TEST.UPD` (2.30, console
markers in `rcS` and `apl_start`; the overlay is generated from the unpacked firmware, so no
Pioneer file is committed) + `stick.img`; install it in update mode with the stick on USB1. **Do not patch `rbp`
in the emulator workflow** — custom firmware is tested by booting it, so keep stock `rbp`
runnable (see the project's standing constraint).

## Gotchas
- CRC32 is over the **encrypted** body, not the plaintext ISO.
- Wrong key → no `CD001` at `0x8000`; `info`/`decrypt` report it explicitly.
- Model string is 12 bytes, NUL-padded; `pack` truncates/pads to 12.
- Sector IV index is the **absolute** 512-byte offset, so `info` checks logical sector 16 by
  decrypting body sector **64** (0x8000/512) with `IV=LE32(64)`.

## References
`crates/xdj-fw` (`xdj` CLI + library, also used by `xdj-host`), `tools/fw/make-test-upd.sh`,
`xdjrx_upd.py`, `fetch_key.py` (repo root), `README.md`, `QEMU.md`. Related skills:
[rbp-binary-internals], [run-xdj-emulator].
