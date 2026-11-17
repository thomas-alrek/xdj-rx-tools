---
name: release-patcher
description: Cut and publish an XDJ-RX custom firmware release (3.00 and later) - the reproducible release build, boot test on a wiped emulator unit, release-note screenshots, the patch file (.xdjpatch) and the cross-platform xdjrx-patcher (macOS / Linux / Windows) that turns Pioneer's official 2.21 into the release and verifies it by MD5. Use when preparing or verifying a release, changing the patcher or the patch format, rebuilding the patcher binaries, or answering how users install the custom firmware.
---

# Releases and the patcher

Users never get Pioneer's firmware from us: they download the official 2.21 from Pioneer and
turn it into the release with **`xdjrx-patcher ORIGINAL.UPD PATCH.xdjpatch [--key KEY] [-o OUT]`**.
Design and format: `docs/firmware-mods.md` §13. User guide: `docs/release-notes-<rel>.md`,
index `docs/releases.md` (linked at the top of the README).

## Cutting a release
1. `tools/fw/build-release.sh` (release number + mod list are in the script) → `build/release/`;
   run it twice: the MD5 must be identical (`SOURCE_DATE_EPOCH`, pinned Docker images).
2. Boot test on a **wiped** emulator unit (delete the test unit's `nand-flash.img` +
   `provisioned.stamp`, power on → stock 2.21 is provisioned, then install the release).
   Must show `### xdjmod: loaded into rbp … N module(s)` and UTILITY → VERSION No. = the release.
2a. Stress test that unit for hours: `tools/test/stress.py --until HH:MM` (skill
   `stress-test-firmware`); every `build/stress/crashes/NNN/` must be explained before release.
3. Screenshots for the release notes → `docs/img/` (from that unit, so they show the release
   number; no test builds; no "EMERGENCY LOOP" — reboot or don't swap sticks with a track loaded).
4. `python3 tools/fw/make-patch.py` → `build/patch/xdjrx-<rel>.xdjpatch` (~300 KB: our files +
   insert-only line edits of Pioneer files; it refuses anything else, so no Pioneer text leaks).
5. `tools/fw/build-patcher.sh` → `dist/patcher/`: `xdjrx-patcher-macos` (universal),
   `-linux-x86_64` (static musl, Docker), `-windows-x86_64.exe` (MinGW, Docker).
6. Verify: run the patcher on the **official zip** with no key (it downloads Pioneer's GPL
   source and derives the key) → MD5 must equal step 1; also a Linux binary in Docker with
   `--key`. Wrong input / wrong key must be refused.
7. Record the MD5s (docs §1 table, release notes Install, README box) and publish:
   the `.xdjpatch` + the three patcher binaries as release assets. Never the `.UPD`, never an
   xdelta of it (measured: 99.9 % of an xdelta 2.21→3.00 is the new image's bytes).

## Patcher internals (`crates/xdjrx-patcher`)
- `src/lib.rs` = the work, shared with the desktop app: `Job::open(orig, patch)` (patch +
  official MD5), `key_from_gpl(on)` / `key_from_arg`, `job.build(&key, out, on)`; progress as
  `Msg::Step` / `Msg::Download`. `src/main.rs` = the CLI (prints, prompts) on top.
- Input `.UPD` or `.zip` (`firmware::read_upd_bytes`), MD5 vs manifest `input.upd_md5`.
- Key: `--key` (hex or file) → `$XDJRX_AES_KEY` → GPL download (`xdj_fw::gplkey::fetch_key`,
  crate feature `gpl-key`, shared with the desktop app: two deflate64 ZIPs → nested tar.gz →
  `initramfs/usr/local/pioneer/aes256.key` → `key_from_passphrase`) → prompt.
- Overlay in a temp dir: new files + `.xdj-modes` (portable modes; `tree.rs` reads it), edited
  Pioneer files from `fwbuild::unpack` + ops (lines split after `\n` only, latin-1), each
  checked by SHA-256; `SOURCE_DATE_EPOCH` from the manifest; `fwbuild::build` +
  `firmware::pack`; writes only on an MD5 match. No arguments → interactive (drag and drop,
  "Press Enter to close").
- A new release = a new `.xdjpatch` for the same patcher, unless the manifest `format` changes.

## Desktop app (`app/patcher`, "XDJ-RX Firmware Patcher")
- Tauri (same stack as the emulator shell), page `app/patcher/ui/index.html` styled like the
  shell's setup screen (`app/ui/index.html`) - keep the two visually in step.
- Two slots (official firmware, patch) filled only by drop or click-to-pick - **never scan the
  user's folders to pre-fill**. Background `Job::open` check → green ✓ / red reason; key
  downloaded by default or "I have the key"; output `XDJ-RX <rel>/XDJRX.UPD` next to the
  firmware or "Change…"; progress bar; done = path + verified MD5 + Show in folder.
- Run: `cd app/patcher && cargo build --release && target/release/xdjrx-patcher-app`.
  Verify with a real build: MD5 must equal the release's (skip the download with "I have the
  key" + `build/aes256.key`).
- Not yet packaged: macOS `.app`; Windows / Linux need their own OS to build (Tauri webviews) -
  CI runners, not Docker on the Mac.

## Gotchas
- Users: firmware updates need a **FAT32** stick (also after 3.00: the boot-time updater has no
  exFAT). Mention only the official 2.21 and the release number, never test builds.
- Unsigned binaries: Windows SmartScreen (More info → Run anyway), macOS Gatekeeper
  (right-click → Open). The release notes explain both.
- macOS: `cp -c` for big disk images (plain `cp` of a sparse image crawls).
