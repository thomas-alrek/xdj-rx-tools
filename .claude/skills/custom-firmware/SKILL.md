---
name: custom-firmware
description: Build, install and verify a modified XDJ-RX firmware (.UPD) - edit the root filesystem (rootfs.cramfs), the pioneer payload (rbp, apl_start), gui/data tarballs or raw image files via an overlay, set a new release number, pack it with the `xdj` CLI, and install it on the emulated unit through Pioneer's own updater. Use when making a firmware change (device features like exFAT/FLAC, boot-script edits, adding files/tools to the unit), testing a custom image, or debugging why a rebuilt image doesn't install or boot.
---

# Custom firmware (`xdj fw build`)

Full reference: **`docs/custom-firmware.md`**. Format details: skill [upd-firmware-format].
Verified end to end: a rebuilt image installs through Pioneer's updater and boots (test mode
SYSTEM Ver. shows the new release).

## Build the tool
```
cd crates/xdj-fw && cargo build --release      # -> crates/xdj-fw/target/release/xdj
cargo test --release                            # codec round trips (needs build/XDJRX.iso)
```
Key: `--key-file`, else `$XDJRX_AES_KEY`, else `.env` (repo, from `tools/fetch_key.py`), else the app's key.

## Feature mods (scripted, regenerated from scratch)
Writing/testing mods and the 3.00 release build: skill **[firmware-mods]**.
```
tools/fw/build-custom-upd.sh REL exfat,fps        # -> build/custom-upd/XDJRX.UPD + stick.img
```
Each mod is `tools/fw/mods/<name>/apply.sh UNPACKED OVERLAY` (edits generated from the unpacked
official image; our binaries built from source in Docker; mods stack). `exfat` = static
fuse-exfat + udev rule; `preload` = the **xdjmod framework** (`libxdjmod.so`, LD_PRELOADed into
stock rbp; API `tools/fw/mods/preload/src/xdjmod.h`: `XM_MODULE`, `xm_expect32/patch32/detour`,
`xm_cfg_get/set` -> /root/rbp/xdjmod.conf, `xm_menu_add` = a native UTILITY row before VERSION
No. with save/apply/factory-reset handled by the core); `fps` = UI FRAME RATE row; `wav` = WAV float/32-bit/≤192 kHz reader (vtable slots +
the browser tag parser's format checks; test sticks: `tools/fw/mods/wav/make-wav-test-image.sh`,
`tools/make-fat32-image.sh`; check the output by recording it: `XDJAUDIO=wav,path=…,timer-period=1451`).
`flac` = FLAC via the wav reader (suffix hook 0x401d8, browser classifier 0x1f1ce4 -> WAV,
tag parser 0x1a7544 -> STREAMINFO + Vorbis comments; dr_flac pinned). One-stick test of every
mod: `tools/fw/mods/make-feature-test-image.sh <rekordbox.xml> [playlist]` (exFAT + baken export).
`waveform` = WAVEFORM COLOR row (RGB/3-band detail + overview; `.EXT` interposer). Defaults: 60 fps,
3-band. **libxdjmod.so is linked against the unpacked firmware's own libs** (build.sh FWLIB) - never
the toolchain's glibc; test every build on a copy of the unit (it must reach `loaded into rbp`)
before anyone installs it: a library that fails to load stops rbp from starting.
New rbp feature: `tools/fw/mods/<f>/<f>.c` with `XM_MODULE(f)` + a one-line `apply.sh`
calling `../preload/apply.sh UNPACKED OVERLAY <f>.c`. Gate every patch on the expected words.
Distribution goal: a delta patch vs stock 2.21 + patcher (`xdj fw mkpatch/patch`, docs/firmware-mods.md §13).
Research, addresses and the plan per feature: **docs/firmware-mods.md**. The release is 3.00
(on the official 2.21). Test on a private `xdj-host` with a copy of the unit's fw dir, not the app's unit.
Nothing goes on a real unit until proven in the emulator. The waveform mod is our own code,
inspired by JaNeinHub/XDJ-RX-Mod (GPL-3, credited in docs/README) - don't copy its code.

## Quickest: unpack, edit in place, repack
```
X=crates/xdj-fw/target/release/xdj
$X fw unpack build/XDJ-RX_v221.zip build/unpacked     # edit files in build/unpacked/<part>/...
$X fw diff   build/XDJ-RX_v221.zip build/unpacked     # + new ~ changed - removed link mode
$X fw repack build/XDJ-RX_v221.zip build/unpacked --release 2.31 -o build/MY.UPD
```
Carries content, new/removed files+dirs, symlink edits, exec-bit changes (owners/devices always
from the base). Verify: unpack the result and `fw diff` it against itself → 0 changes.

## Best practices (full list: docs/custom-firmware.md §2a)
Start from the official image every time · keep the change a small overlay **outside the repo**
(never commit firmware files or patches containing them) · bump `--release` · put changes in parts a
USER update flashes (rootfs/pioneer/gui; data/user need ALL) and within partition sizes (build
enforces 30/8/10 MiB) · BusyBox ash, ARM glibc-2.13 binaries, LF, `chmod +x` new scripts · leave an
`echo` marker on the console · verify with `fw info` + unpack/diff · install on a throwaway unit ·
keep a way back (higher-release rebuild of stock, or reprovision).

## Curated overlay (for a change you keep)
```
X=crates/xdj-fw/target/release/xdj
$X fw unpack build/XDJ-RX_v221.zip build/unpacked       # iso/ rootfs/ pioneer/ gui/ data/ user/
mkdir -p ~/fw-ov/rootfs/etc && cp build/unpacked/rootfs/etc/<file> ~/fw-ov/rootfs/etc/
$EDITOR ~/fw-ov/rootfs/etc/<file>
$X fw build build/XDJ-RX_v221.zip --overlay ~/fw-ov --release 2.31 -o build/MY.UPD
$X fw info build/MY.UPD && $X fw stick build/MY.UPD build/my-stick.img
```
Overlay parts: `iso/` raw image files · `rootfs/` → rootfs.cramfs (mtd5) · `pioneer/` →
pioneer.tar.gz (mtd7; paths start `pioneer/`, e.g. `pioneer/pioneer/rbp`) · `gui/` · `data/` ·
`user/`. Replace keeps mode/owner; new files get host perms, root:root; `.xdj-delete` lists
removals. **data/user only reach the unit with an ALL update** (a normal USER update writes
PANELCOM, a kernel bank, mtd5, mtd7, gui).

## Install + verify
- App: **File > Install Firmware Update…** → pick MY.UPD (it builds the stick, boots update mode,
  inserts it on USB1) → "Firmware update is complete" → power off/on. Manual: File > Enter Firmware
  Update Mode → power on → USB1 LOAD `xdj fw stick` output.
- Scripted on a throwaway unit: `tools/test/regress.py --only custom` (or with `--unit <installed
  fw dir>` to skip provisioning). Example image + markers: `tools/fw/make-test-upd.sh`.
- Evidence to look for (console / `xdj nand info <nand-flash.img>`): `signature ok. version =
  X`, `### USER UPDATE ###`, `### UPDATE : 2.21 to X ###`, per partition `Success` + `check sum
  OK!!`, then env `release=X`; after the power cycle your change; test mode Mode 1 SYSTEM Ver.X.

## Facts that matter
- **No signature.** `check_sig` = footer CRC32 + `XDJAERO` model prefix; the updater's md5 only
  compares each written partition with its own source. `xdj fw build` sets the model to
  `XDJAERO<release>` itself.
- `--release` must be a number (the updater compares floats). It becomes the env `release` →
  UTILITY > VERSION No. / test mode SYSTEM. Downgrade behaviour unverified — go up, or reprovision.
- The updater ends on "turn the power off/on" without a console success line; "installed" is the
  NAND env `release=` (what the app and regress.py check).
- cramfs rules the writer enforces: root children at byte 76 (kernel check), files ≤ 16 MiB,
  uid ≤ 65535, gid ≤ 255. The rebuilt rootfs is a little bigger (no dedup); mtd5 is 30 MB.
- ISO: Rock Ridge (names, modes, symlinks) is what the guest reads; `update`/`mot2bin` must stay
  0555 — replacing them keeps the mode.

## Rules
- **Never commit firmware contents** (Pioneer-owned): keep overlays outside the repo or generate
  them from `xdj fw unpack` output at build time (see `make-test-upd.sh`).
- The emulator's own workflow still runs **stock** firmware; changes live only in images you build
  and install. Don't patch `rbp` to make the emulator work.
- Not yet supported: rebuilding uImage/initramfs (raw replace via `iso/images/uImage` only),
  u-boot, PANELCOM, binary-patch helpers for rbp.

## Code map
`crates/xdj-fw/src/`: `fwbuild.rs` (unpack/build, overlay parts), `tree.rs` (model + overlay
rules), `iso.rs`, `cramfs.rs`, `tarball.rs`, `firmware.rs` (UPD crypto/pack, key, blobs, stick),
`bin/xdj.rs` (CLI). Tests: `crates/xdj-fw/tests/roundtrip.rs`.
