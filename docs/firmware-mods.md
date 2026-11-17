# Firmware mods (custom firmware 3.01)

Feature mods for the XDJ-RX on top of the official 2.21 firmware, and the research behind them.
The release is **3.01**, built on the official **2.21** (§1: how to reproduce it, MD5). Building and installing custom
firmware in general: [custom-firmware.md](custom-firmware.md).

Status: ✅ built and verified in the emulator · 🔬 researched (static analysis), not built ·
⬜ not started.

| Mod | Status | Mechanism | Feasibility | Effort (est.) |
|---|---|---|---|---|
| `exfat` — exFAT USB sticks | ✅ | FUSE driver + udev rule | done | — |
| `preload` — the xdjmod framework (`libxdjmod.so`) | ✅ | `LD_PRELOAD` in `apl_start` | done | — |
| UTILITY menu rows for mods (framework) | ✅ | extended item table | done | — |
| Safe start — hold MIDI at power-on: stock rbp, no mods | ✅ emulator | `apl_start` key check (§3) | done | — |
| `fps` — UI frame rate 20 / 30 / 60 (default 60) | ✅ emulator | 2 immediates + timer detour | done (real unit: untested) | real-unit test |
| SCREEN SYNC — copy timed before the refresh (opt-in, for unit testing) | 🔬 built, emulator can't judge | own refresh thread + FBIO_WAITFORVSYNC interposer (§5a) | default OFF | real-unit test |
| `wav` — WAV float / 32-bit / up to 192 kHz | ✅ emulator | reader vtable slots + tag-parser checks | done (AIFF: open) | AIFF |
| `flac` — FLAC 16/24-bit ≤ 192 kHz, with tags | ✅ emulator | suffix + browser hooks, dr_flac via the wav reader | done (real unit: untested) | — |
| `waveform` — RGB / 3-band waveforms (default 3-band) | ✅ emulator | `.EXT` interposer + detail/overview detours | done (real unit: untested) | tuning |
| WAVEFORM SCROLL: ORIGINAL / SMOOTH (default SMOOTH) | ✅ emulator | playhead estimate + sub-pixel canvas shift (§10) | done (real unit: untested) | visual check, CPU |
| Hot cues A–H on the waveforms, in rekordbox colours | ✅ emulator | PCO2 parse + marker bits + overview images (§8a) | done (real unit: untested) | — |
| `beatjump` — BEAT JUMP on the pads (opt-in) | ✅ emulator | PlayerInnards onKey slot + LED detour (§10a) | done (real unit: untested) | — |
| HOT CUE E-H on SHIFT + pads (opt-in) | ✅ emulator | getCue override around the stock hot-cue call (§10a) | done (real unit: untested) | — |
| `keys` — Camelot key display (opt-in) | ✅ emulator (3.01) | name table swap, DB name detours, key-image remap (§10c) | done (real unit: untested) | — |
| `folders` — [FOLDER] lists Contents | ✅ emulator | one branch (§7a) | done | — |
| Large libraries (1000+ tracks) | 🔬 measured (§10b) | — | no memory limit hit | — |
| OneLibrary | 🔬 later (nice to have) | mount-time `export.pdb` synthesis | medium | 2–3 weeks |

**In release 3.01:** `exfat`, `fps` (+ SCREEN SYNC), `wav`, `waveform` (colour, hot cues A–H,
scroll), `flac`, `beatjump` (beat jump, hot cues E–H on the pads), `keys` (Camelot), `folders`
(+ the framework, safe start). 3.00 had the same without SCREEN SYNC and `keys`.
UTILITY rows, in order: UI FRAME RATE (60), SCREEN SYNC (OFF), WAVEFORM COLOR (3-BAND), WAVEFORM
SCROLL (SMOOTH), KEY DISPLAY (CLASSIC), BEAT JUMP (OFF), BEAT JUMP A (4 BEATS), BEAT JUMP B
(16 BEATS), HOT CUE E-H (OFF) — defaults in brackets, restored by a factory reset.

All addresses below are for the 2.21 `rbp` (sha256 `4f62aa71…`, [rbp-binary-internals] skill).

## 1. Release 3.01: reproduce it from the official 2.21

Release **3.01** = the official 2.21 image + the mods `exfat,fps,wav,waveform,flac,beatjump,keys,folders`, in that
order. The build is **reproducible byte for byte**: the same inputs give the same `.UPD`.

| File | MD5 |
|---|---|
| `XDJ-RX_v221.zip` (Pioneer's download, input) | `c1ee67884f120e4cd8d4ffd6b9ad73eb` |
| `XDJRX.UPD` inside it (official 2.21) | `e1facfb82de17b2544e958ca544e00c4` |
| **`XDJRX.UPD` — custom firmware 3.01 (output)** | **`c33963859ac201a5df7e4e0e591ceac6`** |
| `XDJRX.UPD` — custom firmware 3.00 (previous release) | `5492b333f6eb4b853cb7cfa113c48659` |

`xdj fw info` on the output: 48322576 bytes, model `XDJAERO3.01`, footer CRC32 `9d6f4824`,
`images/release.txt = 3.01`. What the mods add (overlay, MD5):

| Overlay file | MD5 |
|---|---|
| `rootfs/sbin/mount.exfat-fuse` | `3d8e08ccb61b480d4e021619f80b0ea1` |
| `rootfs/lib/udev/xdj_is_exfat` | `5c79cf91a5d596af2362bce33ac0d9e0` |
| `rootfs/etc/udev/rules.d/12-usb-memory-auto-mount.rules` (edited) | `310e73ef9f6ee510c229fe64cb41bb3f` |
| `pioneer/pioneer/libxdjmod.so` | `470fd07cf8c347b60f0455b133b515b8` |
| `pioneer/pioneer/apl_start` (edited) | `3583c3c711aa8285eece902669f0b526` |

### 1a. Prerequisites (macOS or Linux host)

- Rust (`cargo`) — builds the `xdj` CLI (`crates/xdj-fw`).
- Docker with `linux/arm/v7` emulation (Docker Desktop has it) — compiles our ARM code.
  Base images are pinned by digest: `debian:bookworm@sha256:268fb24c…` (armel cross-gcc
  `12.2.0-14`, `tools/fw/mods/preload/build.sh`) and `alpine:3.20@sha256:d9e853e8…`
  (`tools/fw/mods/exfat/build-exfat-fuse.sh`).
- `curl` + network for the first build: fuse-exfat 1.4.0 (sha256-checked) and dr_flac (pinned
  commit, sha256-checked) are fetched into `build/mods/`.
- The `.UPD` AES key: `.env` from `tools/fetch_key.py`, `$XDJRX_AES_KEY` or `--key-file`
  ([custom-firmware.md](custom-firmware.md)).
- Pioneer's official 2.21 firmware, `build/XDJ-RX_v221.zip` (check the MD5 above).

### 1b. One command

```sh
tools/fw/build-release.sh                 # -> build/release/XDJRX.UPD + stick.img
# >> base build/XDJ-RX_v221.zip md5 c1ee67884f120e4cd8d4ffd6b9ad73eb
# >> build/release/XDJRX.UPD (release 3.01, mods exfat,fps,wav,waveform,flac,beatjump,keys,folders) md5 c33963859ac201a5df7e4e0e591ceac6
```

`build-release.sh [BASE] [OUTDIR]` fixes the release (3.01), the mod list and
`SOURCE_DATE_EPOCH=1767225600` (2026-01-01 00:00 UTC: the mtime `xdj` gives every new or changed
file), checks the base MD5 and prints the output MD5.

### 1c. Step by step, from an unpacked 2.21

What `build-release.sh` → `build-custom-upd.sh` does; run from the repo root:

```sh
export SOURCE_DATE_EPOCH=1767225600
X=crates/xdj-fw/target/release/xdj
(cd crates/xdj-fw && cargo build --release)

# 1. unpack the official image (iso/ rootfs/ pioneer/ gui/ data/ user/)
$X fw unpack build/XDJ-RX_v221.zip build/release/unpacked

# 2. each mod reads the unpacked tree and writes only into the overlay, in this order
mkdir -p build/release/overlay
for m in exfat fps wav waveform flac beatjump keys folders; do
  tools/fw/mods/$m/apply.sh build/release/unpacked build/release/overlay
done

# 3. rebuild the image from the base + overlay, as release 3.01; then an update stick
$X fw build build/XDJ-RX_v221.zip --overlay build/release/overlay --release 3.01 \
  -o build/release/XDJRX.UPD
$X fw info  build/release/XDJRX.UPD
md5 build/release/XDJRX.UPD               # Linux: md5sum
$X fw stick build/release/XDJRX.UPD build/release/stick.img
```

- The unpacked tree must be fresh: the mods read Pioneer's files from it (`apl_start`, the udev
  rules, `rootfs/lib` to link against) and never modify it. `fw build` also needs the original
  image: owners, device nodes and untouched files come from it.
- `exfat`: builds `mount.exfat-fuse` once (`build/mods/exfat/`) and adds it, `xdj_is_exfat` and
  the udev rule. `fps`, `wav`, `waveform`, `flac`, `beatjump`, `folders`: each calls `preload/apply.sh` with its sources;
  the feature list accumulates in `build/release/xdjmod/features.txt`, and every call rebuilds
  `libxdjmod.so` with all features so far and adds the `LD_PRELOAD` to `apl_start`.
- Mods stack: one that edits a file another mod already put in the overlay starts from that copy.
- Every change is generated: Pioneer files are edited at build time, our code is compiled from
  source, third-party sources are pinned and checksummed. The repo holds no firmware content.
- A different MD5 means a different input: base image, `SOURCE_DATE_EPOCH`, a toolchain image
  (the Debian image installs gcc from the archive at build time; compare `libxdjmod.so`'s MD5
  above) or the mod sources.

### 1d. Test builds and installing

```sh
tools/fw/build-custom-upd.sh REL exfat,fps   # any release number / mod set -> build/custom-upd/
```

- Install: the app's *File → Install Firmware Update…* → `XDJRX.UPD`. The release must be above
  the installed one.
- To test without touching the app's unit, run a private `xdj-host` on a copy of the unit's
  firmware dir (`XDJ_FW_DIR`, `XDJ_RUNTIME_DIR`, `XDJ_PANEL_PORT`, `XDJ_VNC_WS_PORT`) and POST the
  `.UPD` to `/install-update`. Installed = `release=` in the NAND env (`xdj nand env`). Booted =
  `### xdjmod: loaded into rbp … N module(s)` on the console. Skill: `firmware-mods`.
- Verified for 3.00: installed over a wiped, freshly provisioned stock 2.21 unit (twice) and over
  a used unit; defaults as listed above. One of the two fresh installs came up black on its first
  boot (rbp running, no frames drawn) and was fine after a power cycle; the repeat booted
  normally. Treated as a load-sensitive emulator flake (TCG, a busy host), not reproduced — worth
  watching on the real unit.

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
- Verified: `test/usb/rekordbox_new_exfat.img` on USB1 shows the library, and a track loads
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
- Verified: the constructor's console marker appears and rbp boots normally. **It appears in
  every process rbp starts** (LD_PRELOAD is inherited by `sh`, `mount`, …), so hooks must only
  install inside rbp: check `/proc/self/exe` and rbp's sha256 / the original bytes at every patch
  site, and do nothing on a mismatch (another firmware version, or `rbp_solo`).

### Safe start ✅ (emulator)

Hold **MIDI** (top panel) from power-on until the splash: rbp starts **without `LD_PRELOAD`** —
the stock rbp, no mods at all (no mod rows in UTILITY; VERSION No. still shows the installed
release). For when a mod misbehaves; the settings in `xdjmod.conf` are untouched. `apl_start`
(edited by `preload/apply.sh`) reads the key with the firmware's own `gpio -i 6 1` (GPIO 161 =
GPIO6_IO01; prints 1 idle, 0 pressed) right before it starts rbp — after the USB hub setup
(`+++ attach_usbhub +++`), about 19 s after power-on in the emulator — and logs `### xdjmod: MIDI
held: starting without mods (safe start) ###`. The key must still be down at that moment: a
release before it boots the mods as usual. MIDI is part of neither test mode (MEMORY + TEMPO RANGE,
read by rbp) nor update mode (USB STOP + RELOOP/EXIT, read by the initramfs). Verified in the
emulator: key held → stock rbp; not held → mods load (also `tools/test/stress.py --safe-starts N`).

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
- Verified: the row shows in rbp's style before VERSION No., edits with the browse encoder
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
- **Emulator benchmark** (Apple Silicon host, a track loaded): rbp's GUI loop runs
  **19.2 fps** at 20 (interval avg 52 ms), **28.8 fps** at 30 (34 ms) and **40–46 fps** at 60
  (21–24 ms, min 20–22 ms): under TCG a frame costs ~20–22 ms, so the emulator tops out near 45 fps.
  The QEMU meter (frames whose content changed) shows ~4–8 changes/s at every setting on a static
  or slowly changing screen — a still screen redraws identical frames.
- Open: a scrolling-waveform benchmark needs real-time playback (the app's audio backend; the test
  instance's `XDJAUDIO=none,…` did not advance the track); confirming the timer fix needs an
  on-screen timer (pop-up timeout, blink) at 20 vs 60 — the panel LED blinks (1.0 / 0.5 s) are
  unchanged at 60 but run on another clock (they don't slow down at stock's 52 ms frames either);
  CPU headroom and `ALSA: Resolve xrun` with audio; the real unit last.

## 5a. SCREEN SYNC — against tearing 🔬 (opt-in; needs a real unit)

`sync.c` (module `sync`, built with the `fps` mod), UTILITY → **SCREEN SYNC = OFF (default) /
1.5 MS / 3.0 MS / 4.5 MS / 6.0 MS**. The idea is JaNeinHub/XDJ-RX-Mod's (their measurement on a
unit: lead 4.5 ms, refresh 16.80 ms); this is our own implementation.

- **Why:** rbp draws into a front-only framebuffer. DirectFB's flip waits for the panel's end of
  frame (`ioctl(fb, FBIO_WAITFORVSYNC = 0x40044620)` from gui_task) and then copies the frame in
  (~2 ms on a unit); the panel reads top to bottom over each 16.8 ms refresh, and the copy -
  started just after a refresh began - overtakes that readout near row 93: a tear through the
  waveform. More frames per second, more tears.
- **How:** an `ioctl` interposer turns gui_task's wait into a sleep until *lead* before the next
  refresh. A thread of ours (SCHED_FIFO 30) does the real waits on `/dev/fb0` - entering the
  driver only 3 ms before each expected refresh, since the kernel holds the framebuffer lock
  during a wait - and tracks period and phase (seqlock-published model, 8 good refreshes before
  use). No model, a failing or non-waiting driver, or OFF → Pioneer's own wait. Raw syscalls
  for the clocks (glibc 2.13: librt), no TLS. Every 10 s: `sync: on, lead .., period .. ms, gui
  waits .. (synced ..), driver waits .., resets ..`.
- **Emulator (works, can't judge the lead):** the thread finds the emulated panel's 16.81 ms and
  syncs ~97 % of waits. But the tear meter (`docs/emulator-status.md`, 25 s, a track playing,
  60 fps) reads OFF 0.6 torn refreshes/s, 1 inside the waveform; 4.5 MS 27.8/s, ~9/s inside the
  waveform. The emulated guest wakes and copies far earlier after the interrupt than a unit (so
  OFF barely tears there), and a lead tuned to the unit's copy lands on the emulator's readout.
  Hence opt-in, with the lead selectable, until a real unit shows which lead removes the tear.
- **On a unit:** play a track at UI FRAME RATE 60 and watch the scrolling waveform for a
  horizontal break; try each lead; note CPU (`ALSA: Resolve xrun`) - JaNeinHub also cap at 30
  fps (a frame costs ~17 ms there, so 60 can't be reached).

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
- **Verified (emulator)** with `tools/fw/mods/wav/make-wav-test-image.sh` (tones in 6 formats
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

- The per-reader table (16 entries) is freed from the reader's destructors (vtable slots 0/1,
  `0x378d0` / `0x3794c`) and takes free entries first, so a busy table never recycles a live
  reader's entry.

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
- **Verified (emulator):** 44.1/16, 48/24, 96/24, 192/24 with and without a seek table,
  from [FOLDER] and from a rekordbox playlist on an exFAT stick; the recorded output has the exact
  test tones (1000 Hz L / 2000 Hz R at the WAV levels) and the ultrasonic tone at −96 … −134 dBFS;
  length, artist, album and comment shown in the folder view.
- Test sticks: `tools/fw/mods/flac/make-flac-test-image.sh` (tagged tones, no library) and
  `tools/fw/mods/make-feature-test-image.sh XML [PLAYLIST]` — an exFAT stick with a real rekordbox
  export of a playlist (baken), the one-stick test of every mod.
- **Artwork:** not passed on. The [FOLDER] info panel draws no embedded art in stock firmware,
  not even an MP3's ID3 cover, so a FLAC PICTURE block would have no visible effect.
- Open: FLAC decode CPU on the real unit.

## 7a. `folders` — [FOLDER] lists Contents ✅ (emulator)

- rbp's folder lister `0x1f1ed0` (readdir-based: FAT32, exFAT via FUSE and HFS+ all go through
  it) skips two root folders by name: `PIONEER` (always on FAT32/exFAT; HFS+ uses `.PIONEER`, a
  dot name) and `Contents` — the latter only when the stick has a rekordbox library (flag
  `0x2954d40[slot]`, set at mount when `PIONEER/rekordbox/export.pdb` opens). The Contents test
  starts with that flag: `0x1f2498` `bne 0x1f2154` (`0x1affff2d`; flag ≠ 1 → list the entry).
  `folders.c` makes it unconditional (`b`, `0xeaffff2d`), so Contents is always listed; PIONEER
  (rekordbox's own files: USBANLZ, Artwork, … — no audio) stays hidden.
- Tracks under `Contents/<artist>/<album>/` load as plain files (no library cues or grid — the
  library entry has those). `PIONEER REC` (MASTER REC, `0x1f298c`) keeps its own handling.
- Verified (emulator): an exFAT library stick lists `Contents` and an extra folder.

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
  - **Data:** `fopen`/`fopen64`/`open`/`open64` interposers see rbp open a track's `ANLZ*.DAT`
    (the first file it reads on LOAD) or `.EXT` and cache the track from its `.EXT`, `.2EX` and
    `.DAT` (≤ 8 tracks, keyed by path/size/mtime): PWV3 (to recognise the track), PWV5 → RGB565
    per entry, PWV7 → 3-band bar rows per entry (low/mid/high bytes, `band_rows()`, below), PCO2
    hot cues, and the `.DAT`'s PWAV overview (to recognise it early). Inert outside rbp.
  - **Decks:** deck d's block is `**(0x10f020c + d*32)` (count at `+0`, entries `+0x2ee18` = rbp's
    `{(b & 31) * 46 / 31, b >> 5}` per PWV3 byte, marker flags `+0x2ee10`); rbp's array runs ~20
    entries past the file's PWV3. A deck matches a cached track when 48 sampled entries agree.
    rbp fills that array last - at the end of a load (seconds on a unit, ~20 s in the emulator) -
    but loads the **overview** first: deck d's `0x018a2ee4 + d*900` holds the `.DAT`'s 400 PWAV
    entries as `{height, b >> 5}` within ~0.2 s of LOAD (track length `0x18a4510[d]` too); the
    height is the file's `b & 31` on some exports and scaled up (`* 35 / 31`) on others. Until
    the detail matches, a deck matches the cached track whose PWAV fits that array (all 400
    entries: whiteness equal, height from h to h * 36 / 31 + 1; most recently cached first), and a detail match is refused only when the overview identifies
    another cached track (an array left over from the previous track can't win). So the chosen colours and hot cues A–H show from the
    first frame of a load - no stock blue / green A–D first. Re-checked at once when the deck's
    length, overview or detail array changes, else every 0.5 s. Unmatched decks (no colour data)
    draw stock.
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
- **PWV7 byte order and scale.** The three bytes are physically **low, mid, high** (measured
  against the audio of 16 rekordbox-analysed tracks: byte 0 follows < 400 Hz content, r = 0.87;
  byte 2 > 2.5 kHz, r = 0.89), each 0..127. beat-link reads them as mid/high/low; with that order
  bass was drawn in amber and the blue layer stayed thin, unlike rekordbox. `band_rows()` maps
  low → blue × 0.6, mid → amber × 0.45, high → cream (248, 238, 218) × 0.75 rows per unit — fitted
  to rekordbox's 3-band view of the same track and moment (blue kicks near full height, a cream
  core with full-height transients, a thin amber/brown band between).
- Verified (emulator): RGB and 3-band detail and overview on the rekordbox test
  stick; 3-band confirmed by eye against a CDJ-3000, with tracks on both decks and while zooming.
- Open: RGB saturation tuning against rekordbox (one constant), CPU on the real unit.

## 8a. Hot cues A–H on the waveforms, in colour ✅ (emulator)

rekordbox stores up to 8 hot cues (A–H) with a colour each; the XDJ-RX shows A–D, always green
(hot loops orange). Part of the `waveform` mod (`waveform.c`), always on.
- **Where the cues are.** A rekordbox export keeps A–C in the `.DAT` (PCOB), D–H in the `.EXT`
  (PCOB) and all eight in the `.EXT`'s **PCO2**: entries `PCP2` with hot cue number `+0x0c`
  (1 = A), type `+0x10` (1 point, 2 loop), time ms `+0x14`, loop end `+0x18`, comment length
  `+0x28` + UTF-16BE comment, then colour code, R, G, B (rekordbox's palette). rbp's parser
  (`0x1c8804`) reads all of them; its getters keep hot cues 1–4 and nothing displays E–H or the
  colours (PCO2 is only written back when the unit saves cues). The waveform mod's `.EXT`
  interposer parses PCO2 into its per-track cache.
- **Detail waveform.** rbp draws markers from a per-entry array (block `+0x2ee10`, 12 bytes
  per 1/150 s entry; byte `+2`: 1 / 2 = top triangle (images `0x285` / `0x286`), 4 / 8 = bottom
  one (`0x272` / `0x273`); A–D also set `u16 +0` bits `0x100 << i`, used by the overview). We
  set the byte `+2` bits for E–H — after rbp's rebuild (detour of the last builder `0x12792c`)
  and whenever a deck's track or waveform changes — so rbp draws the same triangles and smooth
  scroll carries them. Colours: after rbp overlays the icon strips (`0x1cb3c4`, hooked by
  `scroll.c`), `wf_detail_icons()` recolours the triangle pixels (`0x0700`) at each cue's column,
  rows 0–4 and 119–123.
- **Overview.** rbp's cue layer `0x1f8a50` blits images with `0x129024(surface, x, y, id)`:
  triangles `0x285` (top, y 4) / `0x272` (bottom, y `0x33`), letters A `0x274`, B `0x27e`, C
  `0x280`, D `0x282` (8 × 9 px RGB565, `0xf81f` transparent, `+1` = orange) from
  `gui/pset/imagedata/imagedata.dat` (records of 0x2c bytes: size `+4`, format `+0x18`, data
  offset `+0x20`). It has no E–H images. While the layer runs, a blit detour drops the A–D
  letters; afterwards we draw every hot cue's triangles (over rbp's) and letters — left to
  right, without overlaps — in its colour. The images are ours: ids from `0x400`, answered by a
  detour of the image lookup `0x1694bc` with copies of rbp's records whose pixels we recolour:
  rbp's triangle and A–D shapes, and E–H bitmaps drawn in the same 8 × 9 style.
- Cues without a colour (older exports) keep rbp's green / orange.
- **Verified (emulator):** a rekordbox export with A–H in eight colours (A red, B blue, C
  green, D orange, E magenta, F cyan, G lime, H purple): detail top and bottom and the
  overview letters and triangles in those colours, E–H included; stock 2.21 (safe start) shows
  A–D in green only. Test stick: `tools/build-rb-usb.sh <rekordbox.xml> <playlist>` (baken writes
  PCO2 with the colours).
- **Planned (after 3.01): letters on the detail waveform.** The cue's letter (A–H) next to each
  triangle in the detail waveform, in the cue's colour, as on later Pioneer players. Groundwork
  in place: the recoloured letter images (ids from `0x400`, `IMG_OURS`, 8 × 9 px, A–D from rbp,
  E–H ours) and each cue's detail column, found by `wf_detail_icons()` when it recolours the
  triangles. Plan: in `wf_detail_icons()` (after rbp's icon strips, so smooth scroll carries
  them), blit the letter right of the top triangle (rows 0–8) and of the bottom one
  (rows 115–123) with the blit stub (`0x129024`) or by copying the image pixels into the strip;
  skip a letter that would overlap the next cue's triangle or letter (close cues at high zoom).
  Check: both zoom ends, cues at the very start / end, hot loops, SMOOTH and ORIGINAL scroll.

## 10. WAVEFORM SCROLL: ORIGINAL / SMOOTH ✅ (emulator)

UTILITY → **WAVEFORM SCROLL** = ORIGINAL / **SMOOTH (default)**. `tools/fw/mods/waveform/scroll.c`.
**Credit:** the feature comes from [JaNeinHub/XDJ-RX-Mod](https://github.com/JaNeinHub/XDJ-RX-Mod)
(GPL-3.0, "WAVEFORM SCROLL: ORIGINAL / SMOOTH"); ours is an independent implementation.

- **Stock.** Each GUI frame `0x1cd0f8` clears the 536 × 250 canvas (`*0x10f0248`, RGB565, pitch
  0x430, deck d at `d*0x20fa0` bytes) and draws one column per pixel; the column under the
  playhead line (x = 268) is `trunc(ms · 0.15 / zoom)` (`0x1cb58c`, state block
  `0x10f0208 + d*32`: `+0x0c` that column, `+0x18` zoom = entries per column, 16/8/4/2 by
  level). `ms` comes from a snapshot (`0x1824000`, copied by Ui_CycleTask `0x17814c` every
  ≥ 16 ms), so the waveform moves in whole pixels at irregular moments — judder, more visible at
  60 fps (at zoom 16 a 1-pixel step every ~107 ms).
- **SMOOTH.**
  1. `0x177cfc(d)` (Ui_CycleTask's per-deck playhead update) is detoured to timestamp each new
     playhead (`*(0x294e03c + d*0xb4)`, ms) and keep a position/velocity estimate (snaps on
     jumps > 60 ms, stops after three unchanged updates, extrapolates ≤ 100 ms).
  2. `0x1cb58c(d)` is detoured: after it, the column is recomputed from the estimate at the
     current time, P = clamp(ms · 0.15, 0, count) / zoom; its whole part goes to `+0x0c` (within
     8 columns of rbp's own value), the fraction is kept.
  3. `0x1cb3c4(d)` (the cue-icon overlay, the last step before the playhead line) is detoured:
     after it, the deck's rows are shifted left by the fraction (1/32 steps, RGB565 blend of
     neighbours). rbp mirrors rows 61..5 into 62..118, so only rows 0–61 and 119–123 are blended
     and the mirror is copied. Waveform, beat ticks and cue icons glide together; the playhead
     line stays sharp.
- **Cost (emulator, both decks playing, waveform view):** ~36 fps vs ~39 fps with ORIGINAL, xruns
  about the same (4 vs 3 a minute). A first, unoptimised version (blending all 124 rows) cost
  8 xruns a minute.
- Tearing: SCREEN SYNC (§5a, opt-in) is the attempt. The canvas is copied into a front-only framebuffer (`0x13d884` →
  `0x140ef8`) right after the wait for the panel's end of frame; on a real unit that copy
  overtakes the panel's readout near row 93, through the waveform, on most frames (measured by
  JaNeinHub/XDJ-RX-Mod, who time the copy 4.5 ms before the refresh with a thread that tracks
  the refresh phase). More frames per second = more chances to tear. The emulator models the
  readout (raster, `xdj-tears`; `docs/emulator-status.md`) and shows ~1 torn refresh/s at 60 fps,
  mostly below the waveform - its copy timing is TCG's, not the hardware's.

## 10a. `beatjump` — BEAT JUMP on the pads ✅ (emulator)

UTILITY → **BEAT JUMP** = **OFF (default)** / ON; **BEAT JUMP A** (4 BEATS), **BEAT JUMP B**
(16 BEATS): 1–32. **Credit:** beat jump on the XDJ-RX's pads (SHIFT + AUTO BEAT LOOP) comes from
[JaNeinHub/XDJ-RX-Mod](https://github.com/JaNeinHub/XDJ-RX-Mod) (GPL-3.0); independent
implementation.

- **Use.** SHIFT + BEAT LOOP switches a deck's pads to beat jump (BEAT LOOP blinks, the pads
  light); HOT CUE, BEAT LOOP or LOOP SLICE switch back to rbp's pad modes. Pad A / B = back /
  forward by A, pad C / D = back / forward by B. The jump keeps the position within the beat and
  keeps playing.
- **Keys.** Every deck key reaches `ui::PlayerInnards::onKey` `0x236784` (vtable slot
  `0x421f08`; KeyEvent `+8` code, `+0xb` low nibble 0 press / 2 release; PlayerInnards `+0x26`
  deck 1/2, `+0x34` SHIFT held, `+0x80` held-pad list). The slot points at our handler (checked
  word patch), which takes SHIFT + BEAT LOOP and, in beat jump mode, the pads (press and
  release, so rbp's held-pad list stays consistent) and passes the rest on. SHIFT + BEAT LOOP
  does nothing in stock firmware, and the RX has no other beat jump control.
- **Jump.** Position `PlayEngine::getPosition 0x4410c` (samples at 44.1 kHz); target from the
  deck's beat grid (waveform block `0x28d78a0 + d*0x2ee1c`: `+8` has grid, `+0xc` beats,
  `{u32 ms, u32 beat#}` from `+0x10` — the engine's own grid `0x6cb38(player)` agrees: beat
  1 at 6 ms, beat 2 at 326 ms on the test track), else rbp's detected BPM (`0x5e1e8`/`0x5e200`),
  else 120 BPM; then `Player::jump 0x6b274(player, samples)` — the call a hot cue makes.
- **LEDs.** The panel LED encoder `0x21e6fc` is detoured; for a deck in beat jump mode its TX
  byte `0x02 + 4d` gets pads A–D on and BEAT LOOP blinking (`.1`), the other pad-mode LEDs off.
- **HOT CUE E-H** (UTILITY → **HOT CUE E-H** = **OFF (default)** / SHIFT+PAD), in the same
  key handler: in HOT CUE pad mode (not in beat jump mode), SHIFT + pad A–D plays hot cue E–H
  of the loaded track, exactly as a hot cue press (quantize, start from a pause, hot loops,
  SLIP). The engine has room for hot cues 1–4 only (64-byte cue records, LoopQuantize slots
  1–4; ids 5..8 would overrun them), so E–H borrow the pad's slot for the duration of one call:
  the PcmReader's getCue (vtable slot `0x35db78`, stock `0x3d384` = `*(pcm+0xb4) + id*64`) is
  pointed at our version, which — only on the calling thread, only during the call, only for
  that reader and slot — returns a copy of the slot's record with the E–H position (`+0x20`
  samples at 44.1 kHz, `+0x24` loop end or −1). The stock `DjEngineIF::playHotCue 0x544f0(dj,
  deck, id, 1, 0)` does the rest; on release, SLIP's return (`0x55ea8`) runs in the same
  window. Press and release are swallowed, so rbp's held-pad list stays consistent. While SHIFT
  is held, the pads that have an E–H cue light (TX byte `0x02 + 4d`, `.7` = pad A). A pad
  whose E–H cue doesn't exist keeps the stock SHIFT + pad (delete that hot cue). Positions come
  from the waveform mod's PCO2 cache (`wf_hot_cue()`).
- Beat jump and E–H share the onKey slot and the LED hook (one module): SHIFT + BEAT LOOP
  toggles beat jump; in beat jump mode the pads are beat jump's, otherwise SHIFT + pads are
  E–H's.
- **Verified (emulator):** with a grid, 4 beats = exactly 1280 ms at 187.5 BPM; without one
  (rekordbox exports with an empty PQTZ), the detected BPM (1512 ms per 4 beats at ~159 BPM);
  back and forward on both sizes; LED byte `0xfa` in the TX frame. Open: SYNC/loop/SLIP
  interplay, which beat-jumps like a hot cue call (a loop isn't moved). E–H: SHIFT + pad A /
  D jumped to E (0:09.4) / H (2:24.8) while playing; with SHIFT held the pad LEDs read `0xf4`
  (four E–H cues + the HOT CUE LED).

## 10c. `keys` — Camelot key display (since 3.01)

UTILITY → **KEY DISPLAY** = **CLASSIC (default)** / CAMELOT ("8A" instead of "Am"). Nothing on
the stick changes; real exports store classic names ("Am") in `export.pdb`.
- rbp's key index (1..24) is already in Camelot order: n → number (n + 1) / 2, A for odd n, B
  for even (1 = 1A = Abm … 24 = 12B = E); `0x10687c` (the harmonic query builder) computes the
  same codes.
- **Index → UTF-16 name table** `0x462dd8` (24 pointers in `.data`; the track-info builder
  `0x1e0df8`, the harmonic "matching keys" labels `0x1065a8`): CAMELOT points it at our strings,
  CLASSIC restores rbp's.
- **Library key names** `0x139c4c(category, id, u16 **out, flag)` (category 10 = djdbKey; browser
  KEY lists and key columns): detoured, the name goes through rbp's own parser (`0x105e84` →
  `0x106e68`, every spelling it knows, Camelot included) and is replaced by the Camelot string
  (the pool free `0x1158f8` ignores non-pool pointers). Its 16-entry name cache (`0x1080f2c`,
  stride `0x110`) is cleared on change.
- **Database reads** of a djdbKey row's name `0x31a538(row, 1)` (track lists' KEY column
  `0x1134bc` case 0xC / `0x113d24`, the KEY breadcrumb, the sort builder): cursors opened on
  djdbKey (`0x319b8c`, `0x319e1c`) and the rows they fetch (`0x31a4a0`) are tagged, closing
  (`0x31a24c`) untags; a tagged row's name comes back as the Camelot string, which rbp's parser
  turns back into the same index (harmonic matching keeps working).
- **Sorting by KEY** (browser MENU → SORT → KEY): the list builder `0x1134bc` (mode & 0x3f = 12)
  sorts the rows by the key name it reads (`0x31a538(row, 1)` at `0x113d28`); while it runs
  (CAMELOT), names come back zero-padded ("08A") so the list reads 1A, 1B, 2A … 12B. The KEY
  column itself is drawn from `0x139c4c` ("8A"). The sort-table builder `0x1128c8` (rows of
  `djdbTrackSort` through `0x319488(table, 7, values)`: track id, title, key name, &key id, …)
  gets the padded name and the key index (Camelot order) as the number.
- **"Needs free" flag:** a name read with the flag clear counts as no name for rbp's callers
  (the list builder sorts it as empty), so the hook leaves the flag as the read set it; rbp's
  pool free `0x1158f8` → `0x11ce80` returns for any pointer outside its pool (our strings).
- **The deck panel's key is an image**, not text: imagedata.dat records `0x28e`–`0x2df` (72 × 24)
  hold every key name, one group per Camelot key in Camelot order — "1A", "01A", then the classic
  spelling(s) ("Abm", "G#m"), "2A", "02A", "Ebm", "D#m", … "12B", "12B", "E" (group starts in
  `keys.c` `KEY_IMG`; rbp's image list `0x41d45c` has them in this order). With CAMELOT the image
  lookup `0x1694bc` answers a classic spelling with its group's first image ("Am" `0x2a8` → "8A"
  `0x2a6`) through the framework's `xm_img_remap` (the `waveform` mod owns that detour and applies
  the remap; without it `keys` detours the lookup itself). Neither the text renderer `0x13d0f0`
  (it draws few texts) nor DirectFB's string drawing is involved.
- **Verified (emulator, 3.01):** browser KEY category 1A, 1B, 2A, … with the harmonic lists; the
  deck panel shows "8A" for an Am track; track lists' KEY column in Camelot; SORT → KEY orders
  2A, 5A, 8A, 9A, 11B (CLASSIC: A, Am, Cm, Ebm, Em, as stock); harmonic highlights unchanged.
- GUI images can be dumped to PNG (and one replaced) with `tools/fw/gui-images.py` (§2 of
  docs/custom-firmware.md).

## 10b. Large libraries (1000+ tracks) 🔬

Report: rbp unstable / glitchy with 1000+ track playlists on big sticks (static analysis; to be
measured live).
- **How the library is read.** DeviceSQL opens `export.pdb` with plain `open` (`0x321558`) and
  reads 4 KB pages with lseek/read on demand — no mmap, no whole-file read. Config
  `"INIT_CLOSED=T,PGA=SINGLE,PAGESZ=4096,NBUFS=1536"` (`0x40f2d4`): a page cache of up to ~6 MB
  per open database.
- **One fixed 32 MiB pool for all of it.** DeviceSQL and ~444 app call sites allocate from
  `edbmalloc` (`0x1157a8`), a first-fit pool `malloc(0x2000000)`ed once (`0x1159b8`; control
  block `0x18266ec`: size, free bytes `+0x10`, …). **When it runs out the engine calls a NULL
  handler — a jump to address 0, a crash** (`0x3158fc`), after printing `edbmalloc NG! memory_allocate error`.
- **Browse lists** are built by the DB tasks, not the GUI thread: a 50,000-row list (20 B/row,
  ~1 MB, allocated up front; cap 65,000), O(N log N) heap sort; sorting by artist/album/genre/…
  does an index lookup per row (random 4 KB page reads). No 1000/2000/5000 limits.
- **System.** 256 MB, no swap; rbp's BSS is ~37 MB (12 MB of it memset, always resident); the DB
  tasks are real-time threads and `apl_start` disables RT throttling
  (`sched_rt_runtime_us=-1`), so long per-row loops over big playlists can starve other threads
  (a likely source of "glitchy"); no `oom_score_adj`, no watchdog; `/tmp` is a 512 KB tmpfs.
- **Obvious, low-risk changes (not made yet — measure first):** an engine out-of-memory handler
  instead of the NULL jump (`0x315994`'s setter, re-implemented); a larger pool (`0x1159b8`
  `mov r3, #0x2000000` → 48/64 MiB) if the pool is the limit; pre-reading `export.pdb` at mount
  (warm page cache); `vm.min_free_kbytes` 6–8 MB in `apl_start`; `-o kernel_cache` for exFAT.
  Not recommended without evidence: demoting the DB threads, dropping `O_SYNC`, a different
  allocator.
- **Measured (emulator, 256 MB, 3.00 + a temporary logger inside rbp):** synthetic exports
  made with baken (3,000 tracks / 1.3 MB `export.pdb`; 10,000 tracks / 4.7 MB with playlists of
  100–10,000), browsed: playlists, TRACK / ARTIST / ALBUM / KEY lists, scrolling all of them,
  loads on both decks.
  - Memory: idle rbp 134 MB resident, system 20 MB free + 53 MB page cache. With the 10,000-track
    stick mounted and every list opened and scrolled: rbp 143 MB, pool 10.8 MB used of 32 MiB
    (3,000 tracks: 7 MB), 3–4 MB free + 58 MB page cache, no `edbmalloc NG`. **The pool is
    nowhere near full and the system isn't out of memory**, so a larger pool buys nothing and
    would only take RAM from the page cache.
  - CPU: idle ~9 xruns/min (emulator baseline). The first fast scroll through the cold
    10,000-row TRACK list: 76 xruns in 100 s; the same scroll over cached rows: 16 (20 fps UI)
    and 21 (60 fps UI) — the 60 fps mod isn't the cause. During list browsing gui_task uses
    ~18–29 % of a core and the DB tasks (`DBComm_Tsk`, `DBSMain_Tsk`, RR 18–21) ~4 % each; the
    audio thread `JuceALSA` is FIFO 99. The glitches come with **first-time `export.pdb` page
    reads** (in the emulator the USB stack itself costs host CPU, so the real unit may differ).
  - Obvious next step: pre-read `export.pdb` into the page cache at mount (a few MB), so the
    first browse reads from RAM. Copying a 2 GB image with ~40,000 small files is slow on macOS
    (`cp` walks the sparse extents): use `cp -c` (clone).

## 11. OneLibrary 🔬 (low priority)

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

## 12. Third-party code in the images

Firmware images built with mods contain binaries from third-party source; distributing an image
means following their licences (source offer / notices):

| Mod | Component | Licence |
|---|---|---|
| `exfat` | [relan/exfat](https://github.com/relan/exfat) 1.4.0 (`mount.exfat-fuse`) | GPL-2.0-or-later |
| `exfat` | libfuse 2.9 (static, Alpine `fuse-static`) | LGPL-2.1 |
| `exfat` | musl libc (static) | MIT |
| `flac` | [dr_flac](https://github.com/mackron/dr_libs) v0.13.5 | public domain / MIT-0 |

## 13. Distribution: a patch file + `xdjrx-patcher`

Users get the release from Pioneer's official 2.21 firmware, a small patch file and a
command-line tool (or its desktop app, below); Pioneer's firmware is never redistributed.

```
xdjrx-patcher  ORIGINAL.UPD  PATCH.xdjpatch  [--key KEY]  [-o OUT.UPD]
```

- **ORIGINAL**: the official `XDJRX.UPD` 2.21, or the zip Pioneer distributes it in; checked by
  MD5 (`e1facfb82de17b2544e958ca544e00c4`, zip `c1ee67884f120e4cd8d4ffd6b9ad73eb`).
- **PATCH** (`xdjrx-<release>.xdjpatch`, ~310 KB): an uncompressed tar of `manifest.json` +
  `files/<part>/<path>`. Made by `tools/fw/make-patch.py` from `build/release` (after
  `tools/fw/build-release.sh`): the release's new files as they are (ours: `libxdjmod.so`,
  `mount.exfat-fuse`, `xdj_is_exfat`, with their modes) and, for the two Pioneer files the mods
  edit (`apl_start`, the USB mount rules), **line edits that carry only our text** — whole lines
  inserted, or text inserted inside a line — with the SHA-256 of the original and of the result;
  a change that isn't an insertion is refused, so no Pioneer text ends up in the patch. The
  manifest also has the release (3.01), `SOURCE_DATE_EPOCH`, the input MD5s and the expected
  output MD5.
- **KEY** (optional): 64 hex characters, or a key file. Without it the patcher downloads
  Pioneer's public GPL source release for the XDJ-RX (two deflate64 ZIPs, ~280 MB) and derives
  the key from `initramfs/usr/local/pioneer/aes256.key` (`xdj_fw::gplkey`, also used by the
  desktop app's key prompt; same result as `tools/fetch_key.py`), asking for it only if that fails. `$XDJRX_AES_KEY` also works.
- **What it does:** decrypts the original (the key is checked: ISO9660 must appear), unpacks the
  two edited files, applies the edits and checks their hashes, writes the overlay (new files'
  modes in each part's `.xdj-modes`, so Windows builds the same bytes as macOS/Linux), sets
  `SOURCE_DATE_EPOCH` and rebuilds with `xdj_fw::fwbuild::build` + `firmware::pack` — the same
  code as `build-release.sh`. The result is written only if its MD5 is the manifest's
  (`c33963859ac201a5df7e4e0e591ceac6` for 3.01); default output `XDJ-RX 3.01/XDJRX.UPD` next
  to the original. Run without arguments, it asks for the two files (drag and drop) and waits
  for Enter at the end (double-click use on Windows).
- **Code / builds:** `crates/xdjrx-patcher` (Rust): the library `src/lib.rs` does the work
  (`Job::open` reads the patch and checks the official MD5, `key_from_gpl` / `key_from_arg`,
  `Job::build` with step and download progress through a callback) and `src/main.rs` is the CLI
  on top (`xdj-fw` with `gpl-key`, `tar`, `md-5`, `sha2`, `serde_json`).
  `tools/fw/build-patcher.sh` → `dist/patcher/`: `xdjrx-patcher-macos` (universal),
  `-linux-x86_64` (static musl, Docker), `-windows-x86_64.exe` (MinGW, Docker).
- **Desktop app** (`app/patcher`, "XDJ-RX Firmware Patcher"): Tauri, like the emulator's shell,
  and styled like its setup screen (`ui/index.html`). Two slots - official firmware, patch file -
  filled by dropping files on the window or clicking a slot (native picker); nothing is
  pre-filled or looked for on disk. The pair is checked in the background (`Job::open`: green ✓
  or the reason in red). The key is downloaded from Pioneer's GPL source by default (kept in
  memory for the session); "I have the key" takes 64 hex or a key file. Output defaults to
  `XDJ-RX <release>/XDJRX.UPD` next to the official firmware ("Change…" = save dialog). Build
  shows a progress bar (download 0-70 %, build steps after), then the path, the verified MD5,
  "Show in folder" and the FAT32 next step. Page ↔ Rust: `render(state)`, `progress(pct, step,
  detail)`, `done(result)`, `failed(msg)`, `dragging(on)` / commands `ready`, `browse(kind)`,
  `build(key)`, `reveal`. Build and run: `cd app/patcher && cargo build --release &&
  target/release/xdjrx-patcher-app`. Release bundles (macOS universal `.dmg`, Windows NSIS
  installer, Linux AppImage / `.deb`): workflow `.github/workflows/patcher-app.yml`, built
  natively per OS (Tauri needs each system's webview) and attached to the releases.
- **Why not a standard delta format** (xdelta3 / VCDIFF, BPS): the `.UPD` is AES-256-CBC per
  512-byte sector (IV = sector number), so unchanged sectors at unchanged positions encrypt
  identically — but the rebuild repacks and shifts the root filesystem and the app archive.
  Measured: an xdelta3 patch 2.21 → 3.00 is 48.3 MB, 99.9 % of it the new image's bytes
  (48,254,319 ADD vs 64,161 copied): the patch would be the whole firmware, decryptable with the
  publicly derivable key. A delta-friendly rebuild (Pioneer's sectors kept in place) is possible
  in principle but the gzip archive and cramfs make it uncertain; the patcher avoids the
  question.

## 14. Plan

1. ✅ xdjmod framework: gate, patch helpers, `xdjmod.conf`, UTILITY rows (before VERSION No.,
   in a fixed module order: `core.c` `MODULE_ORDER`), factory-reset hook.
2. ✅ FPS mod with real-time timers + UTILITY row; emulator benchmark.
3. ✅ WAV float / 32-bit / hi-res + the 24-bit fix. Open: AIFF beyond stock.
4. ✅ RGB / 3-band waveforms (inspired by JaNeinHub/XDJ-RX-Mod) + UTILITY row; 3-band byte order
   and weights matched to rekordbox.
5. ✅ FLAC, with Vorbis-comment tags.
6. ✅ WAVEFORM SCROLL (SMOOTH), BEAT JUMP (opt-in), [FOLDER] lists Contents, hot cues A–H in
   colour, HOT CUE E-H on SHIFT + pads (opt-in), safe start.
   3.01: KEY DISPLAY Camelot (§10c), SCREEN SYNC (§5a), colours from the first frame of a load.
7. ✅ Releases 3.00 and 3.01, reproducible (§1).
9. Open: smooth scroll checked by eye; pre-reading `export.pdb` at mount for big libraries (§10b);
   cue letters on the detail waveform (§8a, planned).
10. On the real unit (after the emulator): CPU of SMOOTH and FLAC decode, tearing, big libraries.
11. OneLibrary — nice to have; not now.
12. ✅ Patch file + `xdjrx-patcher` for macOS, Linux and Windows (§13).
13. ✅ Patcher desktop app (`app/patcher`, §13): bundles for macOS, Windows and Linux on the
    3.00 and 3.01 releases. Open: code signing (macOS / Windows), a 1024 px app icon.

Defaults (and factory-reset values) are the improvements — 60 fps, 3-band, smooth scrolling —
so they show right after the update; behaviour-changing features (BEAT JUMP) are opt-in.
Everything is proven in the emulator before anything is installed on a real unit.
