---
name: build-xdj-sd-image
description: Build the XDJ-RX SD test-bed image (build/qemu/nand.img) that boots the firmware to its UI in QEMU with a root shell - a pure-Python (no Docker, no root) assembly from the firmware's own decrypted payloads, plus the kernel boot blobs (`xdj prepare`). Use when (re)building the SD image, changing its rootfs/GUI/u-boot-env, debugging the SD boot path, or setting up the dev emulator (watch.sh / relaunch.sh) from a fresh firmware. The emulated NAND unit (desktop app) is provisioned by the firmware's own updater instead - see run-xdj-emulator.
---

# Building the XDJ-RX SD test-bed image

The dev launchers (`qemu/watch.sh`, `tools/xdj-panel/debug/relaunch.sh`) boot the firmware from an
**SD image** (`build/qemu/nand.img`, the name is historical) using the firmware's own `sd` rootfs
mode in `/init`. The image is assembled from the firmware's **own payloads** — the firmware
binaries are not modified. It is the **root-shell test bed**; the real unit layout is the emulated
NAND (`qemu/xdj/xdj-nand.c`), provisioned by an ALL update (`xdj nand init` + update mode; skill
[run-xdj-emulator], `docs/firmware-update-and-nand.md`). The desktop app uses only NAND.

## Build
```bash
qemu/build-nand.sh          # wrapper → qemu/build-nand.py  → build/qemu/nand.img
# then: qemu/watch.sh       (attaches nand.img as mmc0)
```
Or `./run.sh` from repo root does the whole chain: `xdj fw decrypt` → `xdj prepare` (kernel boot
blobs) → build patched QEMU → `build-nand.sh` → boot.

**Deps:** `python3` + `pycdlib` (`tools/requirements.txt`, or `uv run` reads the script's inline
deps), and **e2fsprogs** (`mke2fs` + `debugfs`). macOS: `brew install e2fsprogs` (keg-only; the
script finds it). Debian: `apt-get install e2fsprogs`. No Docker, no root.

**Inputs:** the decrypted firmware ISO `build/XDJRX.iso`
(`crates/xdj-fw/target/release/xdj fw decrypt build/XDJ-RX_v221.zip -o build/XDJRX.iso`; skill
[upd-firmware-format]) and `qemu/cramfs_extract.py` (unpacks `rootfs.cramfs`). Boot blobs
(`vmlinux.bin`, `atags.bin`, `tramp.bin`, `uImage`): `xdj prepare build/XDJRX.iso --out build/qemu`.

## SD layout (`qemu/build-nand.py`)
- **u-boot env** blob at byte **`0xC0000`** → `rootfs=sd`, `mmcdev=/dev/mmcblk0p1`. The SD/NAND
  DIP strap defaults to SD in QEMU so `fw_printenv` reads this env. (env CRC32 done in pure Python.)
- **MBR partition 1 = ext4** → rootfs (`rootfs.cramfs` tree) + app (`pioneer.tar.gz`).
- **MBR partition 2 = ext4** → GUI assets (fonts + `imagedata`), mounted at `/root/gui`.
- ext4 is built with **only features kernel 3.0.35 can mount** — **no `metadata_csum` / `64bit`
  / `orphan_file`**. `/dev` nodes via `debugfs mknod`; uid/gid 0 via `debugfs sif`; MBR + image
  assembly in pure Python (no `sfdisk`/`dd`/root).

## Three rootfs adaptations (firmware binaries untouched)
1. **GUI mount:** partition 2 is mounted at `/root/gui` from `apl_start` **unconditionally** —
   the device's own `mount_ubifs gui … /root/gui` is gated on `$BOOTMODE` (from i.MX6 boot-mode
   straps QEMU doesn't emulate), so neither branch runs on its own.
2. **Root password cleared** so the firmware getty on `ttymxc1`/`ttymxc0` logs straight into a shell.
   This makes **SD boot the root-shell fallback** for debugging. A NAND install keeps the
   **stock** rootfs, whose root password (DES-crypt hash in `/etc/shadow`, salt `bE`) is unknown,
   so a NAND unit's serial getty can't be logged into. When you need a shell on the running system,
   boot SD (`qemu/watch.sh` / `relaunch.sh` default); both run the same `rbp`/kernel. (For a shell
   on a NAND unit, blank root in a custom firmware's rootfs — skill [custom-firmware].)
3. **`rbp_solo` symlink → `rbp`**: `apl_start` runs `rbp` only when it sees a second CPU, else
   `rbp_solo`, which the firmware doesn't ship. The symlink lets the SD bed boot single-core
   (faster under TCG). A stock NAND install has no such link, so NAND boots need `XDJ_SMP=2`.

## Gotchas
- The SD path copies the rootfs into a **fixed 48 MB tmpfs** (hard-coded in `/init`), so the
  rootfs partition must fit (base+app ≈ 40 MB — `gui.tar.gz` is delivered on partition 2, not here).
- `rbp` reads fonts/imagedata from `/root/gui` (`pset/fontdata`, `pset/imagedata/imagedata.dat`,
  `pioneer/fontdata`) — without partition 2 mounted there, the splash/UI won't render.
- Board is **SABRE-SD** (MACH_TYPE 3980, `ATAG_REVISION=0x500`): rootfs controller is uSDHC3
  (`mmc0`), wired by `qemu/patches/03`. See the `qemu-imx6-patches` skill.

## References
`qemu/build-nand.py` (+ `.sh`), `qemu/cramfs_extract.py`, `crates/xdj-fw` (`xdj prepare`), `run.sh`.
Skills: [upd-firmware-format], [qemu-imx6-patches], [run-xdj-emulator].
