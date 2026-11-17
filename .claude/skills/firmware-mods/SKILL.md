---
name: firmware-mods
description: Write, build, test and release XDJ-RX firmware feature mods on the xdjmod framework (libxdjmod.so LD_PRELOADed into stock rbp) - hooking rbp with checked word patches and detours, adding native UTILITY menu rows with defaults and factory reset, interposing libc calls, and the emulator test loop (private xdj-host, test sticks, recorded audio, screenshots). Also the reproducible 3.00 release build (tools/fw/build-release.sh, its MD5) and the mods' research (fps, wav, flac, waveform colour / on-device analysis / smooth scroll, beat jump, [FOLDER] filter, large libraries). Use when adding or changing a mod in tools/fw/mods/, debugging a mod that doesn't load or doesn't take effect, or rebuilding/verifying the release.
---

# Firmware mods (xdjmod)

Cutting a release, the patch file and `xdjrx-patcher`: skill **[release-patcher]**.

Reference: **`docs/firmware-mods.md`** (per-mod research, addresses, status, plan).
Container/packing: skill [custom-firmware]. rbp analysis: skill [rbp-binary-internals].

## Release 3.00 (reproducible)
```
tools/fw/build-release.sh          # base build/XDJ-RX_v221.zip -> build/release/XDJRX.UPD
```
= official 2.21 + `exfat,fps,wav,waveform,flac,beatjump,folders`, `SOURCE_DATE_EPOCH=1767225600`. Expected
MD5s (base zip `c1ee6788…`, output `5492b333f6eb4b853cb7cfa113c48659`, per overlay file) and
the step-by-step from an unpacked 2.21: docs §1. **Any change to a mod source changes the
output MD5 — update docs §1 (table + example output) in the same commit**, after boot-testing.
`xdj` uses `$SOURCE_DATE_EPOCH` for new/changed files' mtimes (`tree::now`); Docker base
images are pinned by digest (`preload/build.sh`, `exfat/build-exfat-fuse.sh`).

## Layout
- `tools/fw/build-custom-upd.sh REL MODS [BASE] [OUT]` — unpack → each `mods/<m>/apply.sh
  UNPACKED OVERLAY` in order → `xdj fw build` → stick. Test builds: any release above the
  installed one (the release is 3.00; on a test unit, wipe its NAND to start over).
- `mods/preload/` — the framework. `apply.sh UNPACKED OVERLAY [sources…]` accumulates feature
  sources in `OUT/xdjmod/features.txt`, rebuilds `libxdjmod.so` with all of them (`build.sh`:
  Debian armel gcc in Docker, `-nostdlib --no-undefined` against the **unpacked firmware's own
  `rootfs/lib`**, glibc ≤ 2.13 check) and adds `LD_PRELOAD` to both rbp lines of `apl_start`.
- A feature = `mods/<f>/<f>.c` + `apply.sh` that calls `../preload/apply.sh "$1" "$2" <f>.c`
  (more sources / headers: list them; only `*.c` are compiled, headers are on the include path).
- `keys` (KEY DISPLAY Camelot) exists but isn't in the release: the deck panel's key isn't covered (docs §10c).
- Mods in 3.00: `exfat`, `fps` (UI FRAME RATE), `wav`, `waveform` (`waveform.c` WAVEFORM COLOR +
  hot cues A–H in colour, `scroll.c` WAVEFORM SCROLL), `flac`,
  `beatjump` (BEAT JUMP + HOT CUE E-H on SHIFT + pads — one module: they share rbp's deck key
  handler slot and the LED detour, which can each be taken only once)
  (BEAT JUMP / A / B), `folders`. Row order = `MODULE_ORDER` in `preload/src/core.c` (add a
  new module's name there).

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

## Safe start
Holding MIDI (top panel) from power-on starts rbp without `LD_PRELOAD` (`apl_start`, edited by
`preload/apply.sh`, reads `gpio -i 6 1` right after `+++ attach_usbhub +++`). Emulator: power on,
once the console shows output `POST /mainkey 161` (held), release with `POST /mainkey_release`
after `#### Application Start!! ###` (a hold sent while powered off never reaches the board; a
fixed hold time is not enough - the unmodelled USB hub I2C setup can stall a boot for minutes).

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
7. Before a release: `tools/test/stress.py --until HH:MM` on that host (skill
   `stress-test-firmware`, docs/testing.md).

Test sticks: **`tools/test/make-test-sticks.sh [--big]`** builds all of them from synthetic audio
(`test/usb/gen_*.img`: exFAT + FAT32 rekordbox libraries with grids and coloured hot cues A–H,
FLAC and WAV folders, `--big` a 10,000-track library; ~1 min, ~4 with `--big`). Per mod:
`mods/flac/make-flac-test-image.sh` (tagged FLAC tones + WAV/MP3 controls),
`mods/wav/make-wav-test-image.sh`, `mods/exfat/make-exfat-image.sh`,
`mods/make-feature-test-image.sh XML [PLAYLIST]` (exFAT + rekordbox export via baken).

## Patterns that worked
- Second reader on a file: rbp's own reader classes (ctor by suffix as the factory `0x3dac8`,
  open `0x3037c`, `readPcm` vtable slot 3, delete slot 1) from a worker thread — every format.
- Threads: `pthread_attr_setinheritsched(PTHREAD_EXPLICIT_SCHED)` + SCHED_OTHER (+ nice) —
  else a thread created from an RT rbp thread inherits its RT policy.
- Write rbp's GUI data only from gui_task (detour `0x12922c`), count/flags last.
- Vtable-slot patches (`xm_patch32` on the slot) beat detours when there's no direct caller.
- Calibrate against real data: rekordbox's analysis cache on the Mac
  (`~/Library/Pioneer/rekordbox/share/PIONEER/USBANLZ`) holds many analysed tracks, with their
  audio paths in PPTH (e.g. how the PWV7 byte order was established, docs §8).
- Measure memory/CPU from inside rbp with a throwaway module (/proc/meminfo, /proc/self/status,
  /proc/self/task/*/stat; DeviceSQL pool at `0x18266ec`) — the NAND unit has no shell.
- Measure cost: `fps: P=… fps` lines (fps mod, every 10 s) and `Resolve xrun` counts per minute,
  same state, row ON vs OFF.

- Engine calls with an out-of-range id: borrow an in-range slot and swap the data in only for
  the call (E–H: getCue vtable slot override, thread + reader + slot checked) instead of
  passing ids rbp's arrays don't have.
- New GUI images: answer extra ids in a detour of the image lookup `0x1694bc` with copies of
  rbp's own records (same format) pointing at your pixels; rbp's blit `0x129024` does the rest.
  Image file: `gui/pset/imagedata/imagedata.dat` (0x2c-byte records, RGB565, `0xf81f` = key).
- One detour per site: two mods can't both `xm_detour` the same address or patch the same slot —
  share it inside one module, or call between modules (`wf_detail_icons`, `wf_hot_cue`, weak).

## Gotchas
- Stock rbp is never patched on disk; everything is in memory, gated, from libxdjmod.
- [FOLDER] FLAC is classified as WAV (11): type 5 empties the folder.
- The folder info panel never draws embedded art (stock MP3 APIC neither) — not a FLAC bug.
- rbp's deck waveform count runs ~20 entries past the file's PWV3: match tracks loosely.
- [FOLDER] hides PIONEER (always) and Contents (with a library) — the `folders` mod shows Contents.
- Hot cues: rekordbox puts A–C in the `.DAT`, D–H in the `.EXT` PCOB, all 8 + colours in PCO2;
  stock shows A–D in green. Test stick with 8 coloured cues: `tools/build-rb-usb.sh` from a
  rekordbox XML (baken writes PCO2 colours).
- Screenshots for the user go to `build/screens/` (the scratchpad isn't visible to them).
- Big test images: `cp -c` (APFS clone); plain `cp` of a sparse image with many files crawls.
- rekordbox exports can have an empty beat grid (PQTZ with 0 beats): beat features must fall
  back to rbp's detected BPM.
- UI driving: a `browse` press after LOAD re-opens the browser; MENU must be held (1.5 s) for
  UTILITY; SHIFT + key = `send_raw "B 12 01"`, press, `release "B 12 01"`.
- First boot right after installing on a freshly provisioned (wiped) unit: see docs §1d.
- Credit: waveform mod, and the planned WAVEFORM SCROLL / BEAT JUMP, are our own code inspired
  by JaNeinHub/XDJ-RX-Mod (GPL-3.0) — credit in docs, module source and README; don't copy code.
