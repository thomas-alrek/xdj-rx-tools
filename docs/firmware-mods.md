# Firmware mods (custom firmware 3.00)

Feature mods for the XDJ-RX on top of the official 2.21 firmware, and the research behind them.
Test builds count up from **2.22**; the release is **3.00**. Building and installing custom
firmware in general: [custom-firmware.md](custom-firmware.md).

Status: ✅ built and verified in the emulator · 🔬 researched (static analysis), not built ·
⬜ not started.

| Mod | Status | Mechanism | Feasibility | Effort (est.) |
|---|---|---|---|---|
| `exfat` — exFAT USB sticks | ✅ (2.22) | FUSE driver + udev rule | done | — |
| `preload` — the hook library base | ✅ (2.23) | `LD_PRELOAD` in `apl_start` | done | — |
| UTILITY menu items for mods | 🔬 | preload: extended item table | high | ~1 day |
| UI frame rate 20 → 60 fps | 🔬 | preload: 3 immediates + 1 detour | high (cap) / unknown (real 60) | ~2 days + device test |
| WAV 32-bit float, ≤ 96 kHz | 🔬 | preload: WAV/AIFF reader vtable slots | high | 2–4 days |
| FLAC | 🔬 | preload: reader factory + dr_flac | medium-high | 1–2 weeks |
| RGB / 3-band waveforms | 🔬 | preload: ANLZ PWV5/PWV7 + renderers | high / medium-high | 1–3 weeks |
| OneLibrary | 🔬 | mount-time `export.pdb` synthesis | medium, low value | 2–3 weeks |

All addresses below are for the 2.21 `rbp` (sha256 `4f62aa71…`, [rbp-binary-internals] skill).

## 1. Building: regenerated from scratch by scripts

```sh
tools/fw/build-custom-upd.sh 2.23 exfat,preload      # -> build/custom-upd/XDJRX.UPD + stick.img
```

- `tools/fw/build-custom-upd.sh RELEASE MODS [BASE] [OUTDIR]` unpacks the official image
  (`build/XDJ-RX_v221.zip`), runs `tools/fw/mods/<mod>/apply.sh UNPACKED OVERLAY` for each mod in
  order, and builds the `.UPD` and an update stick. Mods stack: one that edits a file another mod
  already put in the overlay starts from that copy.
- Every change is generated: Pioneer files are edited at build time from the unpacked image, our
  own code is compiled from source (Docker), third-party sources are fetched by pinned version and
  checksum. The repo holds no firmware content.
- Install: the app's *File → Install Firmware Update…* → `XDJRX.UPD`. To test without touching
  the app's unit, run a private `xdj-host` on a copy of the unit's firmware dir (`XDJ_FW_DIR`,
  `XDJ_RUNTIME_DIR`, `XDJ_PANEL_PORT`, `XDJ_VNC_WS_PORT`) and POST the `.UPD` to
  `/install-update`; installed = `release=` in the NAND env (`xdj nand env`).

## 2. `exfat` — exFAT USB sticks ✅

- The rootfs already ships `fuse.ko` (apl_start loads it), libfuse 2.9.3 and `mount.fuse`.
- `build-exfat-fuse.sh`: relan's fuse-exfat 1.4.0 as a **static** ARMv7 binary (musl + libfuse,
  Alpine `linux/arm/v7` in Docker), so it doesn't depend on the firmware's glibc 2.13.
- `apply.sh` adds `/sbin/mount.exfat-fuse`, `/lib/udev/xdj_is_exfat` (boot-sector probe; the
  firmware's `vol_id` predates exFAT) and one rule after the HFS+ rule in
  `12-usb-memory-auto-mount.rules`: same mount point, `decrypt_autoexec.sh` and
  `/proc/udev_<slot>` notification as FAT32.
- rbp classifies a mount by its `/proc/mounts` type (`0x132b0c`: `vfat` or no entry → 3,
  `hfsplus` → 4, `fuse` → 7) and stores it per slot (`0x132c28`). exfat-fuse mounts as `fuseblk`,
  which falls through to 3 — rbp handles the stick exactly like FAT32.
- Verified (2.22): `test/usb/rekordbox_new_exfat.img` on USB1 shows the library, and a track loads
  with its waveform, key and length. Make the image with `tools/fw/mods/exfat/make-exfat-image.sh`.

## 3. `preload` — the hook library ✅

- `libxdjmod.so` (`tools/fw/mods/preload/xdjmod.c`), cross-built with Debian's armel gcc; `build.sh`
  fails if it needs a glibc symbol newer than 2.13. `apply.sh` installs it in `/root/pioneer/` and
  prefixes both rbp launch lines in `apl_start` with `LD_PRELOAD=`.
- rbp is non-PIE and links libdirectfb, libc, libmpg123, libasound… dynamically, so a mod can
  interpose those calls, and patch rbp's own code in memory from the constructor (`mprotect`,
  write, `__builtin___clear_cache`; entry hooks are `ldr pc,[pc,#-4]` + address — `bl` can't reach
  the library). rbp on disk is never modified.
- Verified (2.23): the constructor's console marker appears and rbp boots normally. **It appears in
  every process rbp starts** (LD_PRELOAD is inherited by `sh`, `mount`, …), so hooks must only
  install inside rbp: check `/proc/self/exe` and rbp's sha256 / the original bytes at every patch
  site, and do nothing on a mismatch (another firmware version, or `rbp_solo`).

## 4. UTILITY menu items 🔬

Mods that can be switched on and off get native UTILITY rows (hold MENU), **placed just before
VERSION No.**; a **factory reset restores their defaults**.

- One static table drives UTILITY: count `u32` at `0x4621a4` (16), items at `0x4621a8`, `0x34` bytes
  each — label (UTF-16LE), value-string list, value count, current/edit index, enabled, and 7
  callbacks (init, render, beginEdit, change, commit, isDisabled, endEdit). Table order = screen
  order. The screen code reaches it through exactly 7 literal-pool words (`0xe9014`, `0xe91c0`,
  `0xe92fc`, `0xe949c`, `0xe9500`, `0xe9c5c` → count; `0xe9c54` → items). Scrolling uses the count;
  no item indexes are hard-coded.
- Labels are UTF-16 text in rbp drawn with the bitmap font — not images — and UTILITY is
  English-only. `{OFF, ON}` already exists (`0x462040`).
- Plan: the library copies the table, inserts its rows before VERSION No., adds a zeroed sentinel
  row, repoints the 7 words (after checking their old values). Reuse the generic callbacks
  (`0xe7f3c`, `0xe7f40`, `0xe7f68`, `0xe7fa0`, `0xe7fb0`); write our own `render`/`commit`.
- Persistence: `/root/rbp/xdjmod.conf` on the data volume, read in the constructor; `commit`
  rewrites it (temp + rename). Not `XdjSettings.dat` (header `0x2a7b9e8f`, version `0x00010003`,
  `0x8c`-byte records): inserting an id reallocs rbp's settings vector while other threads search
  it unlocked. Factory reset: hook the reset path (`0x282630`) to rewrite the defaults. An ALL
  update reformats the data volume, which also clears it.
- Callbacks run on the display thread: keep `commit` short. Keep labels ≤ ~18 characters, values
  ≤ ~15 (clipping not verified).

## 5. UI frame rate 🔬

- **The cap is a 50 ms loop period, not vsync or hardware.** `gui_task` (`0xb3a68`) draws a frame
  (`0x12922c`) and sleeps 50 ms minus the frame time (`0xb3aac cmp r4,#0x31`,
  `0xb3ad8 rsb r0,r4,#0x32`, sleep `0x125a50`): at most 20 fps, fewer when a frame overruns.
- The screen is DirectFB `DLBM_FRONTONLY` (fbdev default; no `directfbrc`), so `Flip(…,WAITFORSYNC)`
  never waits for vsync; rbp sets `no-hardware`, so the UI is drawn by the CPU. The data it shows
  (play position, waveform scroll) is updated every 16 ms by `Ui_CycleTask` — a faster GUI would
  show genuinely new frames.
- **UI timers count frames.** Each frame calls the timer manager `0x1655b0` with a fixed 50
  (`0x12a314`); every UI timer (blinks, pop-up timeouts, auto-return, possibly hold detection) would
  run 3× fast at 60 fps — and already runs slow when a frame overruns 50 ms.
- Mod: patch the two immediates for period P (16/17 ms ≈ 60 fps), and detour `0x1655b0` to pass the
  real elapsed milliseconds (`CLOCK_MONOTONIC`), so timers keep real time at any rate.
- **First, measure — in the emulator.** Nothing is installed on a real unit until the firmware is
  proven in the emulator. A detour on `0x12922c` logs frame-time statistics (min/avg/p95 per few
  seconds); the app's View → FPS (`GET /fps`) shows the presented rate. Under TCG the absolute
  numbers are not the hardware's (the stock UI presents ~4 fps in the emulator), so compare
  relative to stock: the cost of one frame, the effect of each period P, timer accuracy, and
  `ALSA: Resolve xrun` counts. A real-unit measurement comes last, once the build is proven.
- Risks: GUI CPU time scales with the rate (two Cortex-A9 cores shared with audio, USB/DB loading;
  watch `ALSA: Resolve xrun`); anything animated per frame instead of by timer speeds up; front-only
  drawing tears a little more.
- UTILITY: "UI FRAME RATE" (20 / 30 / 60).

## 6. WAV 32-bit float and up to 96 kHz 🔬

- Readers are chosen by filename suffix only (`PcmReader::open` `0x3dac8`; `.WAV` → `FileReadWav`,
  vtable `0x35d588`; `.AIF/.AIFF` → `FileReadAiff`, vtable `0x35cf98`). Header parse = vtable slot
  2, PCM read = slot 3, rate check = slot 7 (`0x2f278`: 44100/48000 only).
- WAV parse (`0x375e0`): format tag 1 only (no float 3 / extensible `0xFFFE`), ≤ 2 channels,
  16/24-bit. **Stock plays 24-bit files at 16-bit**: the read (`0x37298`) keeps only the top two
  bytes. **Stock AIFF rate bug**: only the top mantissa bits of the 80-bit rate are read, so 96 kHz
  reads as 48000 (plays an octave down).
- The engine runs at 44.1 kHz; any other file rate goes through `common::SampleRateConverter`
  (`dsp::LagrangeSRC`, 4-point Lagrange, **no anti-alias filter**). Frames > 529,200,000 are
  rejected (≈ 92 min at 96 kHz).
- Mod: replace slots 2/3 of both vtables (pointers `0x35d590`/`0x35d594`, `0x35cfa0`/`0x35cfa4`):
  PCM 16/24/32 and float 32/64, extensible, 32–192 kHz, full 24-bit precision; for ≥ 88.2 kHz do a
  half-band 2:1 (or 4:1) decimation in the reader and report 44.1/48 kHz, so rbp's resampler only
  ever sees ≤ 48 kHz.
- Open: whether the browser hides rows by export.pdb rate/depth — no such check was found; one
  USB test with a 96 kHz/float WAV export settles it.

## 7. FLAC 🔬

- Rejected only at load: `.flac` matches no suffix in `0x3dac8`. rekordbox writes `file_type = 5`
  for FLAC in export.pdb; rbp's tag task treats 5 like other non-WAV/AIFF types (no error).
- Reader interface (8 vtable slots): parseHeader fills frames `+0x14`, rate `+0x18`, channels
  `+0x1c`, bits `+0x1e`; `readPcm(this, startFrame, float *outLR, n)` is random access, stereo
  float ±1.0, zero-fill past the end. The file is read through rbp's InputStream at `+4` (length,
  read, tell, seek, ready), so a decoder can use rbp's USB page cache.
- Mod: hook the suffix helper `0x401d8` so `.flac` builds a `FileReadWav` shell, and
  `FileReader::open` `0x3037c` to swap that object's vtable to FLAC slots backed by dr_flac
  (public domain) reading via the InputStream. Same ≥ 88.2 kHz decimation as WAV.
- Risks: decode CPU (estimated a few % of a core per deck at 44.1/16, 2–3× at 96/24), slow seeks in
  files without a SEEKTABLE (load and hot-cue pre-cache), the browser question above.

## 8. RGB / 3-band waveforms 🔬

- **Credit:** [JaNeinHub/XDJ-RX-Mod](https://github.com/JaNeinHub/XDJ-RX-Mod) (GPL-3.0) was first
  to add BLUE/RGB/3-BAND waveforms to 2.21, with an LD_PRELOAD `libxdjmod.so` hooking the detail
  column routine `0x1caf58` and the overview call `0x1fb3e8` — the same points our research found.
  Ours is an **independent implementation inspired by it** (its approach and hook points, not its
  code), so this repo stays GPL-2.0-or-later; it is credited here, in the mod's source and in the
  README's acknowledgements.
- rbp knows 12 ANLZ tags (table `0x4199e0`: PMAI PPTH PVBR PQTZ PWAV PWV2 PCOB PCPT PWV3 PKEY PCO2
  PCP2) and skips the rest. Current rekordbox exports still carry PWV4/PWV5 (`.EXT`) and PWV6/PWV7
  (`.2EX`) — present on our test sticks; PWV3, PWV5 and PWV7 have one entry per 1/150 s.
- PWV3 is decoded into per-deck `{height, whiteness}` entries in `0x129234` (loop `0x12a034`; buffer
  `0x28d78a0 + deck*0x2ee1c`). Detail columns: `0x1caf58` (deck, x, range; **zoom in `s0`**, a
  non-standard VFP argument) into an RGB565 canvas, 45 rows, colour = 8-entry palette `0x419d20`
  by whiteness. Overview: `0x1f8518` (called at `0x1fb3e8`), 300 columns, palette `0x41cf20`.
- A palette change can't show RGB (8 entries vs 9-bit colour per entry). Mod: catch the `.EXT` /
  `.2EX` reads (fopen/fread hooks), decode PWV5 (+ PWV4) or PWV7 (+ PWV6) into a side buffer per
  deck (≈ 2–3 MB), and replace the column and overview routines with colour versions (same
  geometry and marker overlay). CPU cost about as today.
- UTILITY: "WAVEFORM COLOR" (BLUE / RGB / 3-BAND).

## 9. OneLibrary 🔬 (low priority)

- OneLibrary (formerly Device Library Plus) is `PIONEER/rekordbox/exportLibrary.db`, an SQLCipher 4
  database with one fixed key (Pioneer-derived — never commit it). **rekordbox 7 still writes
  `export.pdb` alongside it**, and both use the same ANLZ files — our test stick has both — so the
  XDJ-RX already reads such sticks. The extras (hot-cue banks, cue comments, …) have no UI on the RX.
- rbp embeds the DeviceSQL engine (`djdbContent`, `djdbPlaylist`, …; open `0x319b8c`, fetch
  `0x31a4a0`, ~460 call sites) behind its own dbserver (dispatcher `0xf75f0`). Hooking it to read
  SQLite instead is months of work.
- Only sticks without `export.pdb` (other apps' exports, edits made on a CDJ-3000X) need anything:
  convert `exportLibrary.db` → `export.pdb` at mount time (`decrypt_autoexec.sh` runs after mount,
  before rbp is notified), into tmpfs, redirected by an `open` hook so the stick isn't written.
  Needs SQLite3MultipleCiphers and a pdb writer (e.g. rbxport's `rbl-pdb`) for ARM; key derivation
  takes ~1–3 s on the A9 (estimate).

## 10. Third-party code in the images

Firmware images built with mods contain binaries from third-party source; distributing an image
means following their licences (source offer / notices):

| Mod | Component | Licence |
|---|---|---|
| `exfat` | [relan/exfat](https://github.com/relan/exfat) 1.4.0 (`mount.exfat-fuse`) | GPL-2.0-or-later |
| `exfat` | libfuse 2.9 (static, Alpine `fuse-static`) | LGPL-2.1 |
| `exfat` | musl libc (static) | MIT |
| FLAC (planned) | [dr_flac](https://github.com/mackron/dr_libs) | public domain / MIT-0 |

## 11. Plan

1. `preload` hardening: process + checksum gate, patch helpers, `xdjmod.conf`, UTILITY rows
   (before VERSION No.), factory-reset hook.
2. Frame-time measurement build (emulator) → FPS mod with real-time timers + UTILITY row.
3. WAV float / hi-res (+ the 24-bit and AIFF-rate fixes).
4. RGB / 3-band waveforms (own implementation, inspired by JaNeinHub/XDJ-RX-Mod) + UTILITY row.
5. FLAC.
6. OneLibrary converter, only if OneLibrary-only sticks turn up.

Release as 3.00 once the set is complete; test builds 2.22, 2.23, …. Everything is proven in the
emulator before anything is installed on a real unit.
