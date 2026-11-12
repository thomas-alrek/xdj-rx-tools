---
name: build-xdj-sd-image
description: Build the XDJ-RX SD/nand boot image (build/qemu/nand.img) that boots the firmware to its UI in QEMU - a pure-Python (no Docker, no root) assembly from the firmware's own decrypted payloads, plus the kernel boot blobs. Use when (re)building the bootable image, changing the rootfs/GUI/u-boot-env, debugging the SD boot path, or setting up the emulator from a fresh firmware.
---

# Building the XDJ-RX SD/nand boot image

The emulator boots the firmware from an **SD image** (`build/qemu/nand.img`) because QEMU
models the uSDHC/SD controller but no NAND; the firmware's own `/init` supports an `sd` rootfs
mode. The image is assembled from the firmware's **own payloads** — the firmware binaries are
not modified.

## Build
```bash
qemu/build-nand.sh          # wrapper → qemu/build-nand.py  → build/qemu/nand.img
# then: qemu/watch.sh       (attaches nand.img as mmc0)
```
Or `./run.sh` from repo root does the whole chain: build patched QEMU → `xdjrx_upd.py decrypt`
→ `qemu_prepare.py` (kernel boot blobs) → `build-nand.sh` → boot.

**Deps:** `python3` + `pycdlib`, and **e2fsprogs** (`mke2fs` + `debugfs`). macOS:
`brew install e2fsprogs` (keg-only; the script finds it). Debian: `apt-get install e2fsprogs`.
No Docker, no root — the old container recipe is gone.

**Inputs:** the decrypted firmware ISO (`build/XDJRX.iso` from
`xdjrx_upd.py decrypt XDJRX.UPD -o build/XDJRX.iso`; see the `upd-firmware-format` skill) and
`qemu/cramfs_extract.py` (unpacks `rootfs.cramfs`).

## SD layout (`qemu/build-nand.py`)
- **u-boot env** blob at byte **`0xC0000`** → `rootfs=sd`, `mmcdev=/dev/mmcblk0p1`. The SD/NAND
  DIP strap defaults to SD in QEMU so `fw_printenv` reads this env. (env CRC32 done in pure Python.)
- **MBR partition 1 = ext4** → rootfs (`rootfs.cramfs` tree) + app (`pioneer.tar.gz`).
- **MBR partition 2 = ext4** → GUI assets (fonts + `imagedata`), mounted at `/root/gui`.
- ext4 is built with **only features kernel 3.0.35 can mount** — **no `metadata_csum` / `64bit`
  / `orphan_file`**. `/dev` nodes via `debugfs mknod`; uid/gid 0 via `debugfs sif`; MBR + image
  assembly in pure Python (no `sfdisk`/`dd`/root).

## Two rootfs adaptations (firmware binaries untouched)
1. **GUI mount:** partition 2 is mounted at `/root/gui` from `apl_start` **unconditionally** —
   the device's own `mount_ubifs gui … /root/gui` is gated on `$BOOTMODE` (from i.MX6 boot-mode
   straps QEMU doesn't emulate), so neither branch runs on its own.
2. **Root password cleared** so the firmware getty on `ttymxc1`/`ttymxc0` logs straight into a shell.

## Gotchas
- The SD path copies the rootfs into a **fixed 48 MB tmpfs** (hard-coded in `/init`), so the
  rootfs partition must fit (base+app ≈ 40 MB — `gui.tar.gz` is delivered on partition 2, not here).
- `rbp` reads fonts/imagedata from `/root/gui` (`pset/fontdata`, `pset/imagedata/imagedata.dat`,
  `pioneer/fontdata`) — without partition 2 mounted there, the splash/UI won't render.
- Board is **SABRE-SD** (MACH_TYPE 3980, `ATAG_REVISION=0x500`): rootfs controller is uSDHC3
  (`mmc0`), wired by `qemu/patches/03`. See the `qemu-imx6-patches` skill.

## References
`qemu/build-nand.py` (+ `.sh`), `qemu/cramfs_extract.py`, `qemu_prepare.py`, `QEMU.md`
("Boot to a shell via SD"), `run.sh`. Skills: [upd-firmware-format], [qemu-imx6-patches],
[run-xdj-emulator]. Memory: [[nand-build-no-docker]].
