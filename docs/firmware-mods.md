# Firmware mods (custom firmware 3.00)

Feature mods for the XDJ-RX on top of the official 2.21 firmware, and the research behind them.
Test builds count up from **2.22**; the release is **3.00**. Building and installing custom
firmware in general: [custom-firmware.md](custom-firmware.md).

Status: ✅ built and verified in the emulator · 🔬 researched (static analysis), not built ·
⬜ not started.

| Mod | Status | Mechanism | Feasibility | Effort (est.) |
|---|---|---|---|---|
| `exfat` — exFAT USB sticks | ✅ (2.22) | FUSE driver + udev rule | done | — |
| `preload` — the xdjmod framework (`libxdjmod.so`) | ✅ (2.24) | `LD_PRELOAD` in `apl_start` | done | — |
| UTILITY menu rows for mods (framework) | ✅ (2.24) | extended item table | done | — |
| `fps` — UI frame rate 20 / 30 / 60 (default 60) | ✅ (2.33) emulator | 2 immediates + timer detour | done (real unit: untested) | real-unit test |
| `wav` — WAV float / 32-bit / up to 192 kHz | ✅ (2.27) emulator | reader vtable slots + tag-parser checks | done (AIFF: open) | AIFF |
| `flac` — FLAC 16/24-bit ≤ 192 kHz, with tags | ✅ (2.41) emulator | suffix + browser hooks, dr_flac via the wav reader | done (real unit: untested) | artwork |
| `waveform` — RGB / 3-band waveforms (default 3-band) | ✅ (2.33) emulator | `.EXT` interposer + detail/overview detours | done (real unit: untested) | tuning |
| OneLibrary | 🔬 later (nice to have) | mount-time `export.pdb` synthesis | medium | 2–3 weeks |
| Waveforms generated for folder tracks | ⬜ idea | reader computes entries into rbp's deck array | likely | — |

All addresses below are for the 2.21 `rbp` (sha256 `4f62aa71…`, [rbp-binary-internals] skill).

## 1. Building: regenerated from scratch by scripts

```sh
tools/fw/build-custom-upd.sh 2.24 exfat,fps          # -> build/custom-upd/XDJRX.UPD + stick.img
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

## 3. `preload` — the xdjmod framework ✅

`libxdjmod.so` is a small framework every rbp feature mod is built on. API: `tools/fw/mods/preload/src/xdjmod.h`.

- **Core** (`src/`): `core.c` (process gate, module registry, `xm_log`, `xm_now_ms`), `patch.c`
  (`xm_expect32`, `xm_patch32`, `xm_detour`), `config.c` (`xm_cfg_get/set` →
  `/root/rbp/xdjmod.conf`), `menu.c` (UTILITY rows + the factory-reset hook).
- **A feature** is one `.c` with `XM_MODULE(name) { … }`: check its patch sites with
  `xm_expect32`, install hooks, and add a UTILITY row with
  `xm_menu_add(label, values, n, key, default, apply)`. The core saves the choice and calls
  `apply(choice)` at startup, on every change and after a factory reset. Its `apply.sh` is one
  line: `../preload/apply.sh UNPACKED OVERLAY feature.c` (see `tools/fw/mods/fps/`).
- Startup (constructor priority 300, after every module registered at 200): gate (is this rbp? do
  the core's patch sites hold 2.21's words?) → load settings → each module's init → install the
  UTILITY table → hook factory reset. Any mismatch: the library stays inert.
- `build.sh` compiles core + features with Debian's armel gcc in Docker (image `xdjmod-armel-cc`,
  built once) and **links against the firmware's own libraries** (the unpacked rootfs's `lib/`:
  glibc 2.13 `libc`/`libdl`/`libpthread`/`libm`, `libstdc++`) with `-nostdlib --no-undefined`, so
  every symbol binds to the library and version the unit really has — a symbol it lacks fails the
  build, not rbp's start. (Linking against the toolchain's glibc once bound `dlsym@GLIBC_2.4` to
  `libc.so.6`; on the unit it lives in `libdl.so.2`, and rbp died with a relocation error.) Symbols
  that are header wrappers or newer in 2.13 are avoided: `xm_now_ms` and the waveform module use raw
  syscalls (`clock_gettime`, `stat64`).
- `xm_detour` relocates a PC-relative literal load (`ldr rX, [pc, #imm]`) among the two moved
  instructions into the stub (with a copy of the literal); branches are refused. `apply.sh` installs it in `/root/pioneer/`
  and prefixes both rbp launch lines in `apl_start` with `LD_PRELOAD=`.
- rbp is non-PIE and links libdirectfb, libc, libmpg123, libasound… dynamically, so a mod can
  interpose those calls, and patch rbp's own code in memory from the constructor (`mprotect`,
  write, `__builtin___clear_cache`; entry hooks are `ldr pc,[pc,#-4]` + address — `bl` can't reach
  the library). rbp on disk is never modified.
- Verified (2.23): the constructor's console marker appears and rbp boots normally. **It appears in
  every process rbp starts** (LD_PRELOAD is inherited by `sh`, `mount`, …), so hooks must only
  install inside rbp: check `/proc/self/exe` and rbp's sha256 / the original bytes at every patch
  site, and do nothing on a mismatch (another firmware version, or `rbp_solo`).

## 4. UTILITY menu rows ✅ (framework)

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
- `menu.c` copies the table with the mod rows inserted before VERSION No. plus a zeroed sentinel
  row, and repoints the 7 words (each checked first). Rows use rbp's generic callbacks (`0xe7f3c`,
  `0xe7f40`, `0xe7f68`, `0xe7fa0`, `0xe7fb0`) and our own `render`/`commit`, which follow rbp's
  contract: `render(item, cells[2])` fills the label/value cells (u16 length at `+0x20`, text at
  `+0x22`) with the edit choice while editing, else the saved one; `commit` returns 1 on change.
- Verified (2.24): the row shows in rbp's style before VERSION No., edits with the browse encoder
  (push, rotate, push), saves, applies live, and comes back after a restart.
- Persistence: `/root/rbp/xdjmod.conf` on the data volume, read in the constructor; `commit`
  rewrites it (temp + rename). Not `XdjSettings.dat` (header `0x2a7b9e8f`, version `0x00010003`,
  `0x8c`-byte records): inserting an id reallocs rbp's settings vector while other threads search
  it unlocked. Factory reset: `0x282630` (rbp's first-frame boot-key handler) is
  detoured; on TEMPO RANGE + DELETE (not MEMORY) it clears `xdjmod.conf` and re-applies every
  row's default before the stock reset runs (not yet exercised live). An ALL
  update reformats the data volume, which also clears it.
- Callbacks run on the display thread: keep `commit` short. Keep labels ≤ ~18 characters, values
  ≤ ~15 (clipping not verified).

## 5. `fps` — UI frame rate ✅ (emulator)

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
- `fps.c`: UTILITY → UI FRAME RATE = 20 (STOCK) / 30 / **60 (default)** → period P = 50 / 33 / 17 ms, written
  live into the two immediates. The detour on `0x1655b0` passes the real elapsed milliseconds to the
  UI timers at 30/60; at 20 (STOCK) nothing changes (stock immediates, timers count frames).
  Every 10 s it logs `fps: P=.. ms  NN.N fps  interval avg/min/max` (time between GUI frames).
- **First, measure — in the emulator.** Nothing is installed on a real unit until the firmware is
  proven in the emulator. A detour on `0x12922c` logs frame-time statistics (min/avg/p95 per few
  seconds); the app's View → FPS (`GET /fps`) shows the presented rate. Under TCG the absolute
  numbers are not the hardware's (the stock UI presents ~4 fps in the emulator), so compare
  relative to stock: the cost of one frame, the effect of each period P, timer accuracy, and
  `ALSA: Resolve xrun` counts. A real-unit measurement comes last, once the build is proven.
- Risks: GUI CPU time scales with the rate (two Cortex-A9 cores shared with audio, USB/DB loading;
  watch `ALSA: Resolve xrun`); anything animated per frame instead of by timer speeds up; front-only
  drawing tears a little more.
- **Visually confirmed** in the emulator: waveform playback is much smoother at 60 than at 20.
- **Emulator benchmark** (2.24, Apple Silicon host, a track loaded): rbp's GUI loop runs
  **19.2 fps** at 20 (interval avg 52 ms), **28.8 fps** at 30 (34 ms) and **40–46 fps** at 60
  (21–24 ms, min 20–22 ms): under TCG a frame costs ~20–22 ms, so the emulator tops out near 45 fps.
  The QEMU meter (frames whose content changed) shows ~4–8 changes/s at every setting on a static
  or slowly changing screen — a still screen redraws identical frames.
- Open: a scrolling-waveform benchmark needs real-time playback (the app's audio backend; the test
  instance's `XDJAUDIO=none,…` did not advance the track); confirming the timer fix needs an
  on-screen timer (pop-up timeout, blink) at 20 vs 60 — the panel LED blinks (1.0 / 0.5 s) are
  unchanged at 60 but run on another clock (they don't slow down at stock's 52 ms frames either);
  CPU headroom and `ALSA: Resolve xrun` with audio; the real unit last.

## 6. `wav` — WAV float, 32-bit, up to 192 kHz ✅ (emulator)

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
- **A second check, in the browser.** The folder view and track info take a WAV's length from
  rbp's tag parser `0x1a7544` (the DBCM task), which has the same stock limits and returns −1 for
  anything else — the track then loads with no length. Its duration formula (data bytes × 600 /
  (ch × rate × bits), in 1/75 s) holds for every format we accept, so the mod turns its three
  format checks into no-ops: tag ≠ 1 (`0x1a77f8`), rate not 44.1/48 kHz (`0x1a7864`), bits not
  16/24 (`0x1a78d8`). The channel check (≤ 2) stays.
- `wav.c` (module `wav`) points WAV vtable slots 2/3/7 (`0x35d590`, `0x35d594`, `0x35d5a4`) at its
  own parse / readPcm / rate check, mirroring the stock functions' use of the object and rbp's
  InputStream (seek/read/ready, the first-read wait, the error −6 path, the 0.1 ms yield per
  512 frames):
  - PCM 16/24/32 and float 32/64, `WAVE_FORMAT_EXTENSIBLE`, mono/stereo, 8–192 kHz, word-aligned
    chunk walking; full precision (fixes stock's 24-bit truncation); float clamped to ±1.
  - ≥ 88.2 kHz: a windowed-sinc low-pass (Blackman, 63 taps for 2:1, 127 for 4:1, cutoff ≈ 0.46 ×
    the output rate) and decimation in the reader, reporting 44.1/48 kHz — rbp's own resampler
    only sees ≤ 48 kHz. Per-reader state lives in a small side table (rbp's object has no room).
  - The library uses VFPv3 internally (`-mfloat-abi=softfp`), keeping rbp's soft-float calls.
  - Each parse logs one line (`wav: 96000 Hz 24-bit pcm 2 ch, decim 2 -> 48000 Hz, …`).
- **Verified (2.27, emulator)** with `tools/fw/mods/wav/make-wav-test-image.sh` (tones in 6 formats
  under `XDJ WAV TEST`, browsed via [FOLDER]) and two real-world samples (96 kHz/24 and
  48 kHz/float, extensible with `fact`/`LIST` chunks) on a library-less FAT32 stick
  (`tools/make-fat32-image.sh`). All play with the right length. The unit's output, recorded with
  QEMU's `wav` audio driver (`XDJAUDIO=wav,path=…,timer-period=1451`) and analysed: 1000 Hz left /
  2000 Hz right exact in every format (pitch and channel order right through decimation + SRC),
  levels as generated; the −12 dBFS ultrasonic tone (30 kHz / 60 kHz) shows at its alias
  frequencies only at −92 … −110 dBFS (≈ 80–100 dB suppression).
- "END" on a deck after loading in the emulator just means the (short) test file played out —
  a deck that was playing keeps playing the new track.
- Open: the AIFF reader (slots at `0x35cfa0`/`0x35cfa4`, tag parser `0x1a7fe0`) — 32-bit/float
  AIFC, and the stock rate bug (96 kHz AIFF read as 48 kHz); a rekordbox-exported hi-res track
  (export.pdb path, not just [FOLDER]); CPU headroom on the real unit.

## 7. `flac` — FLAC ✅ (emulator)

- **Loading.** rbp picks a reader by suffix in its factory (`0x3dac8`), asking `0x401d8(path,
  suffix)` once per known suffix; `.flac` matches none. `flac.c` detours the helper: asked about
  `".WAV"` (literal `0x35dd04`) for a path ending `.flac`, it says yes, so rbp builds a
  `FileReadWav` — whose parse/read slots belong to the `wav` mod. `wav.c` sees `fLaC` (or `ID3`)
  and decodes through `flac.c`'s codec (dr_flac v0.13.5, pinned + checksummed by
  `fetch-dr_flac.sh`, reading via rbp's InputStream), sharing the wav reader's decimation for
  > 48 kHz. Decoding stays sequential: the decimator's overlapping windows reuse what was decoded.
- **Browser.** The folder view classifies names in `0x1f1ce4(dirent, utf16 name, flag)`: MP3 1, AAC 2,
  MP4 3, M4A 4, WAV 11, AIF(F) 12, JPG 200, dir 100, else −1 (hidden). Type 5 (export.pdb's FLAC)
  makes the whole folder come up EMPTY, so `.flac` is classified as WAV (11). The WAV tag parser
  `0x1a7544(ctx, info)` then rejects it (no `RIFF`) after reading the first 2 KB into `ctx[7]`;
  our detour fills the track info from STREAMINFO (length in 1/75 s at `+8`, channels/format
  `+0x4d`, rate code `+0x4e`, kbps `+0x46`, "length valid" `+0x4a`, parsed `+0x4c`) and the text
  fields from the VORBIS_COMMENT block, read through the parser's `FILE *` (`ctx[1]`; `0x131634` /
  `0x131708` are fread/fseek wrappers): TITLE `+0x50`, ARTIST `+0x250`, COMMENT `+0x350`, ALBUM
  `+0x550` (the row with the disc icon), GENRE `+0x150`. Text is converted UTF-8 → Latin-1 (rbp's
  8-bit text; beyond U+00FF → `?`).
- **Library.** export.pdb tracks of type 5 load through the same suffix hook; their waveforms come
  from the rekordbox analysis (waveform mod).
- **Verified (2.38–2.41, emulator):** 44.1/16, 48/24, 96/24, 192/24 with and without a seek table,
  from [FOLDER] and from a rekordbox playlist on an exFAT stick; the recorded output has the exact
  test tones (1000 Hz L / 2000 Hz R at the WAV levels) and the ultrasonic tone at −96 … −134 dBFS;
  length, artist, album and comment shown in the folder view.
- Test sticks: `tools/fw/mods/flac/make-flac-test-image.sh` (tagged tones, no library) and
  `tools/fw/mods/make-feature-test-image.sh XML [PLAYLIST]` — an exFAT stick with a real rekordbox
  export of a playlist (baken), the one-stick test of every mod.
- Open: embedded artwork (PICTURE block), FLAC decode CPU on the real unit.

## 8. `waveform` — RGB / 3-band waveforms ✅ (emulator)

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
- `waveform.c` (module `waveform`), UTILITY → **WAVEFORM COLOR** = BLUE (STOCK) / RGB /
  **3-BAND (default)**:
  - **Data:** `fopen`/`fopen64`/`open`/`open64` interposers see rbp open a track's `ANLZ*.EXT`
    and parse it and its `.2EX` (≤ 8 tracks cached, keyed by path/size/mtime): PWV3 (to recognise
    the track), PWV5 → RGB565 per entry, PWV7 → 3-band bar heights per entry (beat-link's
    half-heights low × 0.4 / mid × 0.3 / high × 0.06, × 1.5 for rbp's 45 rows). Inert outside rbp.
  - **Decks:** deck d's block is `**(0x10f020c + d*32)` (count at `+0`, entries `+0x2ee18` = rbp's
    `{(b & 31) * 46 / 31, b >> 5}` per PWV3 byte, marker flags `+0x2ee10`); rbp's array runs ~20
    entries past the file's PWV3. A deck matches a cached track when 48 sampled entries agree;
    re-checked every 0.5 s. Unmatched decks (no colour data) draw stock.
  - **Detail:** `0x1caf58` (zoom in `s0`; first instruction `ldr ip, [pc, #0x19c]`, relocated by
    `xm_detour`) is detoured through an assembly shim. BLUE or unmatched: the original runs. RGB:
    rbp's bar height and markers, colour of the tallest entry in the column. 3-BAND: layered from
    the foot — white high on top, brown low+mid overlap, then the taller of blue low / amber mid
    (beat-link / CDJ-3000 colours), markers above the bar.
  - **Overview:** `0x1f8518` is detoured; after the original draws, the surface is locked again
    (`0x13ca98`/`0x13cb00`) and each of the 300 bars is repainted from the PWV5/PWV7 entries of
    its slice of the track (column x = PWAV entry x + x/3 of 400). The palette's "+6" variant isn't
    played/unplayed (both sides use the same entries), so no played state is shown.
- RGB colour: PWV5's 3-bit channels are mostly high on all three (e.g. 7,6,5), so a linear map is
  near-white pastel, while rekordbox shows saturated hues. `pwv5_color` normalises the strongest
  channel to 255 and stretches the others away from it ×2.5 (7,6,5 → orange-salmon, 5,7,6 → green,
  6,6,7 → lavender); standard R/G/B order (beat-link swaps G/B).
- Verified (emulator): RGB (2.34) and 3-band (2.33) detail and overview on the rekordbox test
  stick; 3-band confirmed by eye against a CDJ-3000, with tracks on both decks and while zooming.
- Open: RGB saturation tuning against rekordbox (one constant), CPU on the real unit.

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

## 11. Distribution: a delta patch, not a firmware image

The 3.00 release is distributed as a **patch against the stock 2.21 `.UPD`** plus a small patcher:
the user supplies Pioneer's official image, the patcher outputs 3.00. Pioneer's firmware is never
redistributed. Design:

- **Patch = only our content.** The mods already produce an overlay (§1). `xdj fw mkpatch BASE
  OVERLAY` turns it into a patch file: new files verbatim (ours), and every edited Pioneer file as
  line operations against the base (insert / replace / delete at line N) that carry only our new
  lines plus the sha256 of the base file — no Pioneer text. Binary Pioneer files can't be edited by
  a patch (none are). Plus metadata: base `.UPD` sha256, release `3.00`, the expected output sha256.
- **Patcher = `xdj fw patch STOCK_2.21.UPD MODS.xdjpatch -o XDJRX.UPD`** (and a menu item in the
  desktop app): verify the base, decrypt, apply, rebuild, encrypt, verify the output hash. No
  Docker or shell needed on the user's side.
- The `.UPD` is AES-encrypted with Pioneer's key, which we can't ship either: the patcher gets it
  the way the app does (the user's key file, or derived from Pioneer's published GPL source by
  `tools/fetch_key.py`).
- Needs a deterministic build (same output bytes on every machine): fixed mtimes for new files, a
  stable order in cramfs/tar/ISO; checked by the output hash.

## 12. Plan

1. ✅ xdjmod framework: gate, patch helpers, `xdjmod.conf`, UTILITY rows (before VERSION No.),
   factory-reset hook.
2. ✅ FPS mod with real-time timers + UTILITY row; emulator benchmark. Open: real-time-playback
   benchmark, on-screen timer check, factory reset live test.
3. ✅ WAV float / 32-bit / hi-res + the 24-bit fix (emulator). Open: AIFF, a rekordbox export.
4. ✅ RGB / 3-band waveforms (own implementation, inspired by JaNeinHub/XDJ-RX-Mod) + UTILITY row (emulator).
5. ✅ FLAC, with Vorbis-comment tags (emulator). Open: embedded artwork.
6. Waveforms generated on the fly for folder tracks (no rekordbox analysis): the reader decodes the
   audio anyway, so it could compute rbp's per-deck `{height, whiteness}` entries (and 3-band
   colours with simple band filters) when such a track loads.
7. OneLibrary — nice to have, for when export.pdb goes away; not now.
8. Delta patch + patcher (§11), after the mod research.

Defaults (and factory-reset values) are the improvements — 60 fps and 3-band — so they show right
after the update; the stock behaviour is one UTILITY setting away. Release as 3.00 once the set is
complete; test builds 2.22, 2.23, …. Everything is proven in the
emulator before anything is installed on a real unit.
