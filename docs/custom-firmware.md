# Custom firmware: build, install and verify a modified XDJ-RX `.UPD`

**Status: working end to end.** A firmware rebuilt with `xdj fw build` from the
official 2.21 image (new release number, edited root filesystem, edited `pioneer` payload)
installs through Pioneer's own updater on the emulated unit and boots:
`signature ok. version = 2.30` → `USER UPDATE 2.21 to 2.30` → every partition md5-verified →
"Firmware update is complete" (Ver.2.21 → Ver.2.30) → after a power cycle both console markers
print and test mode shows **SYSTEM Ver.2.30**.

This is the project's main deliverable: the emulator is a firmware testbed, so device features
(exFAT, FLAC, ...) are developed as custom firmware and tested by booting it. Stock `rbp`, the
kernel and the rootfs stay unmodified in the emulator workflow itself; modifications live in the
firmware you build.

- Tool: the **`xdj` CLI** (`crates/xdj-fw`, Rust; `cargo build --release` there).
- Skill: **`custom-firmware`** (how-to); format reference: skill **`upd-firmware-format`**.
- End-to-end example: **`tools/fw/make-test-upd.sh`**; regression: `tools/test/regress.py`
  (stage `custom`).

---

## 1. Workflow

Two equivalent ways in; both end in the same `build` and keep untouched metadata exact.

**A. Unpack, edit in place, repack** (quickest for exploring):
```sh
X=crates/xdj-fw/target/release/xdj
$X fw unpack  build/XDJ-RX_v221.zip build/unpacked    # iso/ rootfs/ pioneer/ gui/ data/ user/
$EDITOR build/unpacked/rootfs/etc/rc.d/rcS            # edit, add, delete, chmod +x, ln -s ...
$X fw diff    build/XDJ-RX_v221.zip build/unpacked    # review: + new  ~ changed  - removed ...
$X fw repack  build/XDJ-RX_v221.zip build/unpacked --release 2.31 -o build/MY.UPD
```
`repack` = `diff` turned into an overlay (`--keep-overlay DIR` to keep it) + `build`. It carries
content changes, new/removed files and dirs, symlink edits and exec-bit changes; ownership and
device nodes can't be edited from macOS and always come from the base image. Editing
`iso/images/release.txt` sets the release too (`--release` overrides).

**B. A curated overlay** (best for a change you keep and rebuild — see §2a):
```sh
$X fw unpack build/XDJ-RX_v221.zip build/unpacked     # browse everything
mkdir -p ov/pioneer/pioneer
cp build/unpacked/pioneer/pioneer/apl_start ov/pioneer/pioneer/   # copy what you change
$EDITOR ov/pioneer/pioneer/apl_start
$X fw build build/XDJ-RX_v221.zip --overlay ov --release 2.30 -o build/MY.UPD
$X fw info  build/MY.UPD                              # model, CRC, release, key check
$X fw stick build/MY.UPD build/my-stick.img           # FAT16 stick with XDJRX.UPD
```
Install on the emulated unit (desktop app): **File > Install Firmware Update…** and pick
`MY.UPD` — the app builds the USB stick itself, power-cycles into firmware update mode and
inserts it on USB1 (`POST /install-update`). Manual equivalent: File > *Enter Firmware Update
Mode (at next power on)*, power on, USB1 → LOAD `my-stick.img` (or LOAD it while powered off:
it is queued and inserted when the initramfs asks for it). The updater runs; when it shows "Firmware update is
complete. Turn the power off/on before using." power off, untick update mode, power on.
Scripted: `tools/test/regress.py --only custom` (does exactly this on a throwaway unit).

The key is found the usual way: `--key-file`, `$XDJRX_AES_KEY`, `XDJRX_AES_KEY=` in a `.env`
here or in a parent dir (written by `tools/fetch_key.py`), else the desktop app's saved key.

## 2a. Best practices for building a custom firmware

1. **Always start from the official image** of the version you target (`build/XDJ-RX_v221.zip`),
   never from a previous custom build — your change set stays small and reviewable, and a new
   official release is a one-line rebase (`xdj fw build NEW.zip --overlay ...`).
2. **Keep the change as a small overlay, outside this repo.** Firmware files are Pioneer's: never
   commit them, nor patches that contain their lines. Keep your overlay in a private location, or
   (like `tools/fw/make-test-upd.sh`) a script that generates it from `xdj fw unpack` output at
   build time. Use `fw unpack` + `fw diff` to see exactly what your edit touches.
3. **Bump the release** above what's installed (2.21 → 2.30, 2.31, ...). It's the number the unit
   shows (UTILITY > VERSION No., test mode SYSTEM) and the one the updater compares (as a float:
   use plain numbers). The footer model follows it automatically (`XDJAERO<release>`).
4. **Put the change where it is flashed by the update you'll run.** A normal (USER) update writes
   the kernel bank, rootfs (mtd5), pioneer (mtd7) and gui; `data/`/`user/` only reach the unit with
   an ALL update (provisioning). Respect the partition sizes — `build` refuses a rootfs > 30 MiB,
   pioneer > 8 MiB or uImage > 10 MiB.
5. **Write for the target.** BusyBox `ash` (no bash), ARM EABI5 binaries linked against the
   firmware's glibc 2.13 (`lib/`), LF line endings, a `#!/bin/sh` shebang and the exec bit on
   new scripts (`chmod +x` before `repack`; new files take the host mode). Replaced files keep
   their original mode/owner.
6. **Leave a visible marker** while developing — an `echo "### my-change vN ###"` in the script
   you touch shows on the console (View > System Log), proving the new payload is what booted.
7. **Verify before installing**: `xdj fw info` (model prefix, CRC, release), `xdj fw unpack` of the
   result + `xdj fw diff <result> <that dir>` (expect 0 changes), `cargo test --release` if you
   touched the codecs.
8. **Test on a throwaway unit, not your main one**: `tools/test/regress.py --only custom` style
   (or File > Install Firmware Update on a unit you can reprovision). Keep stock firmware
   bootable: the emulator's own workflow must never depend on a patched `rbp` or rootfs.
9. **Keep a way back**: rebuild the official image with a higher `--release` (downgrade
   behaviour is unverified), or reprovision the unit.

## 2. The overlay

`xdj fw build` never starts from an unpacked copy: an unprivileged host can't hold the root
filesystem's owners and device nodes. It starts from the **official image** and applies an
**overlay directory** on top, so every untouched entry keeps its exact metadata.

| overlay part | goes into | flashed to | written by a USER update? |
|---|---|---|---|
| `iso/...` | the update image itself (e.g. `iso/images/uImage`, `iso/update`) | — | — |
| `rootfs/...` | `images/rootfs.cramfs` | mtd5 (root fs) | yes |
| `pioneer/...` | `images/pioneer.tar.gz` (`pioneer/rbp`, `pioneer/apl_start`, ...) | mtd7 | yes |
| `gui/...` | `images/gui.tar.gz` (fonts, image data) | mtd9/mtd10 (UBI) | yes |
| `data/...` | `images/data.tar.gz` (settings XML) | mtd6/mtd8 | **no — ALL update only** |
| `user/...` | `images/user.tar.gz` | — | ALL update only |

Rules (`crates/xdj-fw/src/tree.rs`, `tarball.rs`):
- A file **replaces** the same path and keeps the original's mode, owner and type; a new path
  is **added** with the host file's permission bits, owned by root:root (missing parent dirs are
  created 0755). Host symlinks become symlinks.
- A `.xdj-delete` file inside a part lists paths to remove (one per line, `#` comments); an
  entry that doesn't exist is an error.
- Paths are as inside the payload: `pioneer/pioneer/apl_start` (that tarball has a top-level
  `pioneer/` dir), `data/valid` (its `./` prefix is handled), `rootfs/etc/rc.d/rcS`.
- Unknown top-level overlay dirs are an error (typo guard); `*.md` and dotfiles are ignored.
- `--release X.YY` writes `images/release.txt` and, unless `--model` is given, the footer model
  `XDJAERO<release>`. Use a number: the updater compares releases as floats.

**Never commit firmware contents.** Every file in the image is Pioneer's. Keep overlays outside
the repo, or generate them from the unpacked image at build time with a patch (as
`make-test-upd.sh` does with `awk`/`printf`).

## 3. What the device checks (and doesn't)

Read from the update initramfs and the `update` binary (unstripped ARM ELF in the ISO):

1. **`check_sig`** (initramfs; `check_sig.c` + `CommonCrc32.c`, 7.5 KB): the footer CRC32 over
   the encrypted body and that the 12-byte model string starts with `XDJAERO`. It prints the rest
   as the version ("signature ok. version = 2.30"). **No cryptographic signature.**
2. **`decrypt_update`**: `losetup -e aes` with the AES key from the GPL initramfs, `mount` (the
   kernel autodetects iso9660 and uses Rock Ridge), copies `lib/` and `usr/lib/` from the image,
   runs `./update USB <language>` from the mounted image.
3. **`update`**: reads the update type from the panel MCU (USER 0x10 normally; ALL 0x20 on a
   factory/blank unit — the emulator's `XDJ_UPDATE_TYPE`), compares versions ("already updated
   target=%f, release=%f"), reflashes the panel MCU from `PANELCOM.mot`, then per partition:
   `flash_erase` / `nandwrite` (or ubiformat for gui), then `nanddump | head -c <size> | md5sum`
   against the source file's md5 ("### check sum OK!!"). That md5 is the only content check, and
   it compares the image with itself. Last, `finEnv` writes the u-boot env: `release` (from
   `images/release.txt`), `rev_*` (from `system.rev`/`rbp.rev`/`subucom.rev`), `kernel_bank` (the
   bank it just wrote), `update_status=success`. It then shows the D007 "complete" screen and
   waits for the power to be cut.

So a rebuilt image installs exactly like an official one. A USER update writes, in order:
PANELCOM → the *other* kernel bank (mtd3 = A / mtd4 = B) → mtd5 rootfs.cramfs → mtd7 pioneer →
mtd9/mtd10 gui. The data partitions are only written by an ALL update.

## 4. The image formats (the codecs)

All in `crates/xdj-fw/src/`, each round-trip-tested against the real firmware
(`cargo test --release`, reads `build/XDJRX.iso`).

### `.UPD` container (`firmware.rs`)
`[AES-256-CBC body, per 512-byte sector, IV = LE32(sector) || 0×12][12-byte model, NUL-padded]
[LE32 CRC32 of the encrypted body]`. `pack` zero-pads the ISO to a sector multiple. A Pioneer
`.zip` (deflate or deflate64) holding the `.UPD` is accepted everywhere.

### ISO9660 + Rock Ridge (`iso.rs`)
The official image is `genisoimage -R -J` ("UsbAuto", Rock Ridge 1.09, Joliet). What the guest
uses is the Rock Ridge view: **NM** names, **PX** modes/owners (`update` and `mot2bin` are 0555;
most files 0444), **SL** symlinks (`lib/libc.so.6 -> libc-2.13.so`, ...).
- Reader: walks every directory, parses SUSP entries including **CE** continuation areas.
- Writer: ISO9660 with uppercase d-character identifiers (`NAME.EXT;1`, unique per dir), both
  path tables, the PVD + terminator, and Rock Ridge on every record: **SP** + **CE** in the root's
  `.` record pointing at a continuation sector holding **ER** "RRIP_1991A" (SP + PX + ER doesn't
  fit in one 255-byte record), **PX**
  (36-byte RRIP 1.09 form), **NM**, **SL** (root/`.`/`..` component flags). No Joliet (Linux uses
  Rock Ridge). Dates are fixed (2017-10-18). Directory records never cross a sector.
- Verified: write → read gives the identical tree; pycdlib (independent) reads a rebuilt image
  with the same 87 files, modes, symlinks and data except the files changed on purpose; the
  device's kernel mounts it and runs `./update` from it.

### cramfs (`cramfs.rs`)
The root filesystem: cramfs v2 (`flags = FSID_VERSION_2 | SORTED_DIRS`), 1075 inodes, 17 MB.
- Superblock 64 bytes + root inode at 64; inode = mode:16 uid:16 | size:24 gid:8 | namelen:6
  (×4) offset:26 (×4) + NUL-padded name. Directory: offset → its children's inodes (sorted by
  name), size = their total length. File/symlink: offset → a table of u32 block-end pointers,
  then zlib-compressed 4 KiB pages. Device: rdev in `size`, offset 0. CRC32 of the whole image
  with the crc field zeroed.
- **Kernel requirement:** the root's children must start at byte 76 (Linux checks `root_offset
  == sizeof(cramfs_super)`); the writer lays directories out breadth-first from 76, then the data.
- Limits enforced: file ≤ 16 MiB (24-bit size), uid ≤ 65535, gid ≤ 255, name ≤ 252 bytes. No
  data dedup (mkcramfs shares identical files; we don't — the 2.21 rootfs grows 17.2 → 17.4 MB,
  fine for the 30 MB mtd5).
- Verified: read → write → read identical (device nodes included); an independent Python reader
  (the `qemu/cramfs_extract.py` logic) walks a rebuilt image; the kernel boots it.

### Payload tarballs (`tarball.rs`)
`rewrite` re-emits every original entry with its original header (owner *names*
`xdj-new-release`, mtimes, order, the `./` prefix style), swapping in overlay data for replaced
files, appending new files (+ parent dirs) as root:root GNU entries, dropping `.xdj-delete`d
paths. Recompressed with gzip -9. Verified: an empty overlay reproduces identical entries for all
four tarballs.

### Build (`fwbuild.rs`)
`build(base, {overlay, release, model})`: read the ISO tree → apply `iso/` → for each payload part
present, decode, apply, re-encode, replace the image file (mode kept) → `release.txt` → model →
write the ISO (volume id copied from the base). `unpack` writes `iso/`, `rootfs/`, `pioneer/`,
`gui/`, `data/`, `user/`, each with a `.xdj-metadata` listing every path's mode/uid/gid/kind
(including the device nodes that can't be created unprivileged).

## 5. Verification

| check | result |
|---|---|
| `cargo test --release` (crates/xdj-fw) | ISO, cramfs, tar round trips + no-change build identical + overlay build + unpack→edit→repack (edit/add/delete/symlink; re-unpack diffs to 0): 5/5 |
| independent readers on TEST.UPD | pycdlib: 87 files, only release.txt / rootfs.cramfs / pioneer.tar.gz differ; an independent Python cramfs walk (`qemu/cramfs_extract.py` logic) finds the rcS marker |
| install on a clone of the provisioned 2.21 unit | `signature ok. version = 2.30`, USER update, PANELCOM + mtd3 + mtd5 + mtd7 + mtd9/10 Success, all md5 OK, 65 s; env `release=2.30`, `kernel_bank=A` |
| boot of the installed 2.30 | both markers on the console; test mode Mode 1: **SYSTEM Ver.2.30**, SUB 0.35, APL 4206, KERNEL 4195 |

Open observation: the *first* test-mode boot right after an update can come up in the normal UI
(the next one enters test mode). Tracked by `regress.py`'s "custom: test mode after update".

## 6. Not covered yet
- **The kernel / initramfs** (`images/uImage`): replace it raw via `iso/images/uImage`, but
  there is no uImage/initramfs rebuilder yet (mkimage header + CRCs, the embedded cpio).
- **u-boot** (`u-boot.bin.nand`, mtd0, ALL update only) and the **panel MCU** (`PANELCOM.mot`,
  Motorola S-records) are carried over untouched.
- **`rbp` patching** itself: possible through `pioneer/pioneer/rbp` in an overlay; no binary
  patch helper yet. (Never patch rbp for the emulator's own workflow — only in firmware you
  build and install.)
- **Downgrades:** the updater compares the image's release with the installed one ("already
  updated target=%f, release=%f"); whether it refuses an *older* or only an *equal* release is
  not verified; installing the *same* release shows the update screen without the progress run
  (likely the "already updated" path). Safe ways back to stock: build with a higher number, or reprovision the unit
  (delete `provisioned.stamp` + `nand-flash.img` in the app's firmware dir).
