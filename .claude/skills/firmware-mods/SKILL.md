---
name: firmware-mods
description: Write, build, test and release XDJ-RX firmware feature mods on the xdjmod framework (libxdjmod.so LD_PRELOADed into stock rbp) - hooking rbp with checked word patches and detours, adding native UTILITY menu rows with defaults and factory reset, interposing libc calls, and the emulator test loop (private xdj-host, test sticks, recorded audio, screenshots). Also the reproducible 3.00 release build (tools/fw/build-release.sh, its MD5) and the mods' research (fps, wav, flac, waveform, folder-track waveforms, waveform scroll, beat jump). Use when adding or changing a mod in tools/fw/mods/, debugging a mod that doesn't load or doesn't take effect, or rebuilding/verifying the release.
---

# Firmware mods (xdjmod)

Reference: **`docs/firmware-mods.md`** (per-mod research, addresses, status, plan).
Container/packing: skill [custom-firmware]. rbp analysis: skill [rbp-binary-internals].

## Release 3.00 (reproducible)
```
tools/fw/build-release.sh          # base build/XDJ-RX_v221.zip -> build/release/XDJRX.UPD
```
= official 2.21 + `exfat,fps,wav,waveform,flac`, `SOURCE_DATE_EPOCH=1767225600`. Expected
MD5s (base zip `c1ee6788…`, output `14b0a47aa78aa1a48243364e65ccdd4a`, per overlay file) and
the step-by-step from an unpacked 2.21: docs §1. **Any change to a mod source changes the
output MD5 — update docs §1 (table + example output) in the same commit**, after boot-testing.
`xdj` uses `$SOURCE_DATE_EPOCH` for new/changed files' mtimes (`tree::now`); Docker base
images are pinned by digest (`preload/build.sh`, `exfat/build-exfat-fuse.sh`).

## Layout
- `tools/fw/build-custom-upd.sh REL MODS [BASE] [OUT]` — unpack → each `mods/<m>/apply.sh
  UNPACKED OVERLAY` in order → `xdj fw build` → stick. Test builds: any release above the
  installed one (2.22+ … ; release builds 3.00).
- `mods/preload/` — the framework. `apply.sh UNPACKED OVERLAY [sources…]` accumulates feature
  sources in `OUT/xdjmod/features.txt`, rebuilds `libxdjmod.so` with all of them (`build.sh`:
  Debian armel gcc in Docker, `-nostdlib --no-undefined` against the **unpacked firmware's own
  `rootfs/lib`**, glibc ≤ 2.13 check) and adds `LD_PRELOAD` to both rbp lines of `apl_start`.
- A feature = `mods/<f>/<f>.c` + `apply.sh` that calls `../preload/apply.sh "$1" "$2" <f>.c`.

## API (`preload/src/xdjmod.h`)
- `XM_MODULE(name) { … }` — init, runs once in rbp after the gate (process basename `rbp`).
- `xm_expect32(addr, word)`, `xm_patch32(addr, expect, val)` — **gate every patch on the
  expected words**: a mismatch (other firmware) logs and skips, rbp stays stock.
- `xm_detour(addr, w0, w1, hook)` → trampoline to the original. Relocates a PC-relative
  `ldr` literal in the first two words; refuses branches. VFP-argument functions (e.g.
  `0x1caf58`, zoom in `s0`) need an asm shim (see `waveform.c`).
- `xm_menu_add(label, values, n, key, def, apply_fn)` — a native UTILITY row before VERSION
  No.; the choice saved under `key` in `/root/rbp/xdjmod.conf`, `apply_fn(choice)` at boot and on change, factory reset
  (boot-key handler `0x282630`) restores `def`. `xm_cfg_get/set` for other keys.
- `xm_log` → console `### xdjmod: … ###`; `xm_now_ms`.
- libc interposers (fopen/open…): define the symbol, forward with `dlsym(RTLD_NEXT)`; inert
  outside rbp. Raw syscalls for anything newer than glibc 2.13 (`clock_gettime`).

## Test loop (never on the app's unit, never on a real unit first)
1. Build: `tools/fw/build-custom-upd.sh 2.NN <mods>`.
2. Private host on a copy of the unit (`cp -R ~/Library/Application\ Support/XDJ-RX/<hash>
   build/T/fw`): `XDJ_FW_DIR=build/T/fw XDJ_RUNTIME_DIR=build/T/run XDJ_PANEL_PORT=18765
   XDJ_VNC_WS_PORT=16080 [XDJAUDIO=wav,path=build/T/out.wav,timer-period=1451]
   app/host/target/release/xdj-host`.
3. `curl -X POST --data "$PWD/build/custom-upd/XDJRX.UPD" :18765/install-update`; wait for
   `xdj nand env build/T/fw/nand-flash.img` → `release=2.NN`, let the updater finish (~1 min),
   then `POST /power` `off` / `on`.
4. Must see `### xdjmod: loaded into rbp (pid …): N module(s), M menu row(s) ###`. A
   `relocation error` = the library linked something the firmware lacks → rbp doesn't start.
5. USB: `POST /usb "usb1 <copy of img>"`; drive via MCP (`POST :18765/mcp`: `browse`,
   `press browse_push`, `screenshot`, `console`). Audio: analyse the recorded `out.wav`.
6. Only then leave the build in `build/custom-upd` / tell the user which release is safe.

Test sticks: `mods/flac/make-flac-test-image.sh` (tagged FLAC tones + WAV/MP3 controls),
`mods/wav/make-wav-test-image.sh`, `mods/exfat/make-exfat-image.sh`,
`mods/make-feature-test-image.sh XML [PLAYLIST]` (exFAT + rekordbox export via baken).

## Gotchas
- Stock rbp is never patched on disk; everything is in memory, gated, from libxdjmod.
- [FOLDER] FLAC is classified as WAV (11): type 5 empties the folder.
- The folder info panel never draws embedded art (stock MP3 APIC neither) — not a FLAC bug.
- rbp's deck waveform count runs ~20 entries past the file's PWV3: match tracks loosely.
- Credit: waveform mod, and the planned WAVEFORM SCROLL / BEAT JUMP, are our own code inspired
  by JaNeinHub/XDJ-RX-Mod (GPL-3.0) — credit in docs, module source and README; don't copy code.
