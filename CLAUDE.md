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
- `qemu/` — `build-qemu.sh`, `patches/01..11`, `watch.sh`, `build-nand.sh`, `cramfs_extract.py`.
- `qemu_prepare.py` — kernel boot blobs (trampoline/atags/vmlinux). `run.sh` — from-scratch build+boot.
- `build/` (git-ignored) — `qemu-src/` (patched QEMU), `extract/rbp` (the analyzed binary), `qemu/` (boot blobs, nand.img).
- `tools/xdj-panel/` — the virtual web panel (`server.py`, `panel.html`) + `debug/` (RE launcher & helpers: `relaunch.sh`, `rsp.py`, `tables.py`, `mon.py`, `ser.py`, `inj.py`, `sweep_*.py`).
- `docs/` — handovers + the authoritative `subucom-frame-map.md` (control map) and `emulator-status.md` (feature board).
- `test/` — e.g. `test/usb/rekordbox_new.img`; `~/Downloads/rekordbox_test.dmg` also exists.

## Key established facts (so they aren't re-derived)
- **Sub-ucom** = ONE 100-byte SPI frame on `/dev/subucom_spi1.0` (the RX has **no** second
  channel — RX3 does). Frame check = **CRC-16/X-25, poly 0x8408**, per-32-bit-word byte reversal.
- **Source switch** (deck↔line/phono) = frame bits **`rbp[0x33].7` (CH0) / `rbp[0x43].7` (CH1)**,
  `1`=DECK `0`=LINE, injectable + live. The separate **LINE-vs-PHONO** (EXT-TYPE) switch is
  GPIO **119/120** (`switchState +0xa0/+0xb0`); `0x1013`/`LinePhonoSW` = that EXT-TYPE control.
- **GPIO 117/118, 134/135, 162-165 are rbp OUTPUT pins** (mux/control), not switch inputs.
- **Browse encoder:** ROTATE = counter `rbp[0x54]/[0x55]`; PUSH = `rbp[0x5d]` bit 4 (code `0x4205`).
- **Audio** works (patch 11, SDMA/SSI/AUDMUX; sonifier → SDMA ch1 = MasterOut). Audible needs
  routing=DECK + gains up (on-air gate `FUN_00065028`).

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
