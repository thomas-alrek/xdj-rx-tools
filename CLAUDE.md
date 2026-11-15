# CLAUDE.md — XDJ-RX emulator / reverse-engineering project

Orientation for an AI session working in this repo. For depth, see `README.md`, `QEMU.md`,
`docs/emulator-status.md`, and the **skills in `.claude/skills/`** (listed below).

## What this is
A QEMU-based emulator + reverse-engineering of the **Pioneer XDJ-RX** all-in-one DJ system
(Freescale **i.MX6Q**, really a **SABRE-SD** board; Linux 3.0.35; app = **`rbp`**, ARM32 ELF).
Stock firmware boots to the **live rekordbox UI**; the front-panel ("sub-ucom") protocol is
decoded and injectable; USB mounts; and **real deck audio reaches the host**. Work is
interoperability/preservation on owned hardware.

## Standing constraints (do not violate)
- **Never patch `rbp` (or the kernel/rootfs) in the emulator workflow.** Stock firmware must stay
  runnable so *custom* firmware can be tested by booting it. Fix things **QEMU-side**
  (`qemu/patches/`); kernel/rootfs edits are a last resort.
- **Launch the emulator as the user, never `sudo`** (a failed sudo/ptrace attach once killed qemu).
- Accuracy matters (this is a firmware testbed) — no shortcuts; verify live where possible.
- Pioneer-owned material (`.env`, `aes256.key`) is git-ignored; never commit it.

## Layout
- `xdjrx_upd.py`, `fetch_key.py` — `.UPD` firmware decrypt/extract/pack + AES key fetch → skill `upd-firmware-format`.
- `qemu/` — `build-qemu.sh`, `patches/01..19`, `watch.sh`, `build-nand.sh`, `cramfs_extract.py`.
- `qemu_prepare.py` — kernel boot blobs (trampoline/atags/vmlinux). `run.sh` — from-scratch build+boot.
- `build/` (git-ignored) — `qemu-src/` (patched QEMU), `extract/rbp` (the analyzed binary), `qemu/` (boot blobs, nand.img).
- `tools/xdj-panel/` — the virtual web panel (`server.py`, `panel.html`, `logs.html`) + `debug/` (RE launcher & helpers: `relaunch.sh`, `rsp.py`, `tables.py`, `mon.py`, `ser.py`, `inj.py`, `sweep_*.py`).
- `docs/` — handovers + the authoritative `subucom-frame-map.md` (control map), `subucom-led-map.md`
  (LED/meter map), `service-modes.md` (test/update/factory-reset modes, auth chip) and
  `emulator-status.md` (feature board). The XDJ-RX **service manual** (manualslib 1307531) is the
  hardware reference; it's Pioneer-owned, so keep it out of the repo and cite page numbers.
- `test/` — e.g. `test/usb/rekordbox_new.img`; `~/Downloads/rekordbox_test.dmg` also exists.

## Key established facts (so they aren't re-derived)
- **Sub-ucom** = ONE 100-byte SPI frame on `/dev/subucom_spi1.0` (the RX has **no** second
  channel — RX3 does). Frame check = **CRC-16/X-25, poly 0x8408**, per-32-bit-word byte reversal.
- **Source switch** (deck↔line/phono) = frame bits **`rbp[0x33].7` (CH0) / `rbp[0x43].7` (CH1)**,
  `1`=DECK `0`=LINE, injectable + live. The separate **LINE-vs-PHONO** (EXT-TYPE) switch is
  GPIO **119/120** (`switchState +0xa0/+0xb0`); `0x1013`/`LinePhonoSW` = that EXT-TYPE control.
- **GPIO 117/118, 134/135, 162-165 are rbp OUTPUT pins**, not switch inputs. **162-165 = the
  rekordbox/MIDI/USB1/USB2 key LEDs** (active-high, one-hot); patch 17 appends them to the LED line
  as byte `0x14`. GPIO 119/120 = rear LINE/PHONO switch (HIGH=PHONO), **not emulated** (no ext audio).
- **GPIO4_IO30 / GPIO7_IO12 (ids 0x7e/0xcc) are active-low USB FAULT flags, not insert-detect.**
  Insert a USB with `device_add` only; `touch /tmp/usb*_insert` (patch 06) asserts the fault →
  "USB Error. Remove the device." (that was the long-standing "USB Error on a correct insert").
- **Browse encoder:** ROTATE = counter `rbp[0x54]/[0x55]`; PUSH = `rbp[0x5d]` bit 4 (code `0x4205`).
- **Panel input needs QEMU patch 14** (ECSPI burst → `xdj_subucom_frame_start`); without it the
  slave sends all-zero frames and *no* control reaches rbp. The **runtime-verified control map** is
  the top table of `docs/subucom-frame-map.md` (captured at dispatcher `0x2a47c0`, named via the
  debug-console vocabulary). **Jog** = period `rbp[0x0c]` + 16-bit counter `rbp[0x0e]` + flags
  `rbp[0x10]` .0 touch/.1 reverse (deck2 +0x10).
- **LEDs/meters = the MOSI half of the same burst** (`SubTxData`): 20-byte payload, CRC-16/X-25
  at `[0x16]`, deck2 = deck1 + 4 bytes. Patch 15 publishes it (`T <hex>` up `xdjctrl`, or
  `/tmp/subucom_tx`); the panel lights from `GET /leds`. Map: `docs/subucom-led-map.md`.
- A one-shot `b`/`a` left in `/tmp/subucom_ctrl` **next to holds re-fires every 30 frames** (file
  mode) — rewrite the holds alone right after (`inj.py pulse`), or toggles flip back and forth.
- **Service modes** (`docs/service-modes.md`, from the service manual + RE): **test mode** = hold
  `B 13 04`+`B 12 08` (MEMORY+TEMPO RANGE) in `/tmp/subucom_ctrl` *before* boot (rbp checks only
  the first frame; Mode 5/6/7 = LED/switch/pot oracles); **update mode** = `XDJ_BOOT_UPDATE=1`
  (GPIO3_IO30, patch 19). Frame bytes 2–3 = sub-ucom version (0.35).
- **E-7206 AUTH CHIP ERROR** = MFi auth coprocessor missing: it's on **I2C4** (`0x021F8000`,
  `/dev/i2c-3`, slave 0x10), which stock QEMU lacks. Patch 18 adds it (identity regs only).
- **Audio** works (patch 11, SDMA/SSI/AUDMUX; sonifier → SDMA ch1 = MasterOut). Audible needs
  routing=DECK + gains up (on-air gate `FUN_00065028`). **Stutter = guest ALSA xruns** (rbp's ring is
  2×64 frames): periods must complete evenly, one per 1.45 ms — patch 16's pacer thread does that
  (378 → 2 xruns/30 s under load). Check: `ALSA: Resolve xrun` on the console; `XDJ_AUDIO_STATS=1`.

## Common workflows (→ skill)
- Run/boot/USB/inject/screenshot/audio → **`run-xdj-emulator`**.
- Map a control: GPIO → **`map-gpio-control`**; sub-ucom button/fader/knob/rotary →
  **`map-subucom-{button,fader,knob,rotary}`**.
- Reverse-engineer `rbp` internals / REA workflow → **`rbp-binary-internals`**.
- Firmware `.UPD` format → **`upd-firmware-format`**. QEMU patch set → **`qemu-imx6-patches`**.
- Standalone desktop app (Tauri shell + `xdj-host` orchestrator + packaging) → **`build-xdj-app`**;
  the virtual web panel (controls UI, layout, USB LOAD) → **`xdj-virtual-panel`**.

## Gotchas (bite repeatedly)
- Single-core TCG boot is load-sensitive: **close REA/Ghidra before booting** (Ghidra +
  rekordbox-on-host hang the boot). `iPodMgr` has a load-sensitive startup SIGSEGV.
- **Never hammer the gdbstub** (~25 rapid RSP halts → rbp watchdog crash + splash restart).
  Space reads; RSP reads of rbp VA globals EFAULT unless the halt lands in rbp's context.
- Resume via the monitor `cont` after any inferior call. `inj.py pulse` to one-shot without
  clearing the analog-gain holds.

## Git / docs hygiene
- Commit only when asked; branch off `main` first. End commit messages with the attribution line
  given in the session's system reminder.
- `docs/subucom-frame-map.md` = authoritative control map; `docs/emulator-status.md` = feature
  board. Several handovers are **superseded** (see their banners): `deck-line-switch-handover.md`
  (GPIO-switch premise wrong), `rotary-encoder-handover.md` (rotate/push solved).
