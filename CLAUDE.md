# CLAUDE.md — XDJ-RX emulator & firmware toolkit

Orientation for an AI session working in this repo. Depth lives in `README.md`, `docs/` and the
**skills in `.claude/skills/`** (listed under *Workflows*).

## What this is
A QEMU-based emulator + reverse-engineering of the **Pioneer XDJ-RX** all-in-one DJ system
(Freescale **i.MX6**, a **SABRE-SD** board; Linux 3.0.35; main app **`rbp`**, ARM32 ELF), plus a
desktop app and a firmware toolkit. Stock firmware boots to the live rekordbox UI; the front panel,
LEDs, audio, USB, NAND flash, firmware updates and custom firmware all work. Interoperability and
preservation on owned hardware.

## Standing constraints (do not violate)
- **Never patch `rbp` (or the kernel/rootfs) to make the emulator work.** Stock firmware must stay
  runnable so *custom* firmware can be tested by booting it. Fix things **QEMU-side** (device models
  in `qemu/xdj/*.c`, small stock-file edits in `qemu/patches/`). Modified firmware is built and
  installed with `xdj` — it is never what the emulator depends on.
- **Launch the emulator as the user, never `sudo`.**
- Accuracy matters (this is a firmware testbed) — no shortcuts; verify live where possible.
- **Never commit Pioneer-owned material**: firmware, its contents (incl. patches that contain its
  lines), `.env`, `aes256.key`, the service manual (manualslib 1307531 — cite page numbers only).
- **License: GPL-2.0-or-later** (`LICENSE`). Bundled third-party software: `THIRD_PARTY_NOTICES.md`;
  its license texts are packaged by `packaging/collect-licenses.py`.
- Docs and comments describe the **current** state — no history, dates or "was …" notes.

## Layout
- `app/` — the desktop app: `shell/` (Tauri) and `host/` (`xdj-host`: runs QEMU, the in-process
  virtual-panel server `src/panel.rs`, the MCP server `src/mcp.rs` (`POST :8765/mcp`), firmware
  prep, provisioning/NAND boot, power). No Python.
- `crates/xdj-fw/` — Rust library + **`xdj` CLI**: `fw info|decrypt|unpack|diff|repack|build|pack|stick`,
  `prepare` (kernel boot blobs), `nand init|env|info|boot`. Codecs: `.UPD` crypto, ISO9660+Rock Ridge,
  cramfs, payload tarballs. Tests: `cargo test --release`.
- `crates/xdjrx-patcher/` — the end-user **`xdjrx-patcher`** (official 2.21 `.UPD` + a `.xdjpatch` →
  the custom firmware release, MD5-verified; key from Pioneer's GPL source). Patch files:
  `tools/fw/make-patch.py`; binaries for macOS/Linux/Windows: `tools/fw/build-patcher.sh`.
- `qemu/` — `xdj/*.c` (device models: board, display, audio, panel harness, NAND, MFi, USBNC — **edit
  these**; `build-qemu.sh` syncs them into the build tree), `patches/01..05` (stock QEMU files only:
  SoC hooks, i.MX peripherals, SABRE-SD board, ChipIdea USB/hub, macOS timers), `build-qemu.sh`
  (QEMU 11.1.0, `--disable-libssh`). SD-card test bed: `build-nand.py`/`.sh`, `cramfs_extract.py`, `watch.sh`.
- `tools/` — `xdj-panel/` (`panel.html`, `logs.html`, `server.py` = the dev panel server kept identical
  to `panel.rs`, `test_panel_parity.py`, `debug/` RE helpers: `relaunch.sh`, `rsp.py`, `tables.py`,
  `mon.py`, `ser.py`, `inj.py`, `ledprobe.py`, `sweep_*.py`), `test/regress.py`, `test/stress.py`,
  `test/make-test-sticks.sh` (generated test sticks `test/usb/gen_*.img`), `fw/make-test-upd.sh`, `fw/build-custom-upd.sh` + `fw/mods/<mod>/apply.sh` (feature mods),
  `fetch_key.py` (the AES key), `net/install-vmnet-bridge.sh` (optional LAN bridge), `build-rb-usb.sh`, `requirements.txt` (dev-tool Python deps).
- `packaging/` — `build-macos-app.sh` (→ `dist/XDJ-RX.app`), `collect-licenses.py`.
- `docs/` — `subucom-frame-map.md` (**authoritative control map**), `subucom-led-map.md`,
  `service-modes.md`, `firmware-update-and-nand.md`, `usb.md`, `custom-firmware.md`, `firmware-mods.md`,
  `standalone-app.md`, `emulator-status.md` (feature board), `testing.md`, `rbp-and-panel-protocol.md`,
  `debug-console-protocol.md`, `version-display-and-override.md`.
- `build/` (git-ignored) — `qemu-src/` (patched QEMU), `extract/rbp` (the analysed binary), `qemu/`
  (boot blobs, SD image), firmware downloads. `run.sh` — dev quick start (SD test bed).
- The app's per-firmware data: `~/Library/Application Support/XDJ-RX/<hash>/` (prep output,
  `nand-flash.img`, `panelcom-flash.bin`, `provisioned.stamp`).

## Key established facts (so they aren't re-derived)
- **Sub-ucom** = ONE 100-byte SPI frame on `/dev/subucom_spi1.0` (no second channel). Frame check =
  **CRC-16/X-25, poly 0x8408**, per-32-bit-word byte reversal; frame bytes 2–3 = the panel MCU's
  firmware version (parsed from the emulated MCU flash, `XDJ_PANELCOM_IMG`). The ECSPI burst hook
  (`02-imx6-peripherals` → `xdj_subucom_frame_start`) builds each frame; the control chardevs
  (`xdjctrl`/`xdjkey`) bind at realize so a boot-time snapshot is in rbp's first frame.
- **LEDs/meters** = the MOSI half of the same burst (`SubTxData`, 20 bytes, CRC at `[0x16]`, deck 2 =
  deck 1 + 4 bytes), published as `T <hex>` lines; byte `0x14` = the source-key LEDs (GPIO 162–165).
  TX byte `0x11 = 'S'` = rbp ready for standby.
- **POWER key** = `0x5d.0` (code `0x8001`), **active-low** (the harness inverts it). Power off =
  standby handshake: press → rbp prepares → TX `'S'` → the panel cuts power (also rbp's AUTO STANDBY).
- **MASTER REC** = `0x5e.2` (code `0x401`), records the master to **USB2**; LED `0x0a.7` blinks.
  **MENU** must be **held** to open UTILITY. The browser shows **USB1**.
- **Source switch** (deck↔line) = `rbp[0x33].7` / `rbp[0x43].7`, 1 = DECK. LINE/PHONO = GPIO 119/120
  (not emulated). **GPIO 117/118, 134/135, 162–165 are rbp outputs.** Top-panel keys are GPIO
  inputs: rekordbox 159, MIDI 161, USB1 160, USB2 158, DECK 1/2 149, TIME MODE 146.
- **GPIO4_IO30 / GPIO7_IO12 are active-low USB FAULT flags**, not insert-detect. Insert USB with
  `device_add` only, after rbp is polling; never assert the fault files.
- **USB slots are read/write** (no `snapshot`): QEMU write-locks the backing, so one image/stick is
  in one slot at a time (a load moves it from the other slot), an image opened in macOS is
  ejected there first, and failed loads raise a native error dialog. `docs/usb.md` §4.
- **Frame rate**: `qom-get /machine xdj-fps` = the guest's presented frames/s (120 Hz framebuffer
  sampler in `xdj-display.c`, on demand); View → FPS shows it. The rekordbox UI runs ~4 fps.
- **Browse encoder:** ROTATE = 16-bit counter `rbp[0x54]/[0x55]`; PUSH = `rbp[0x5d].4`.
  **Jog** = period `rbp[0x0c]` + counter `rbp[0x0e]` + flags `rbp[0x10]` (.0 touch, .1 reverse).
- **Test mode** = MEMORY + TEMPO RANGE (`B 13 04` + `B 12 08`) held at rbp's first frame
  (*File → Power On in Test Mode*). Known open flake: ~1 boot in 8 comes up normal with correct frames.
- **The app is a NAND unit**: a blank unit is provisioned by the firmware's own **ALL** update (stick
  built by prep, inserted at `### USB update mount.`), then boots from NAND (`XDJ_BOOT_MEDIA=nand`).
  Installed = `release=` in the NAND env (the host sees it, then boots the unit from NAND).
  UTILITY → VERSION No. = that env `release`.
- **No firmware signature**: `check_sig` = footer CRC32 + `XDJAERO` model prefix; the updater md5-checks
  each written partition against its own source. Rebuilt `.UPD`s install like official ones.
- **USB speed**: front slots enumerate at **480 Mbit/s** (`04-usb-chipidea-hub`); macOS QEMU timers need
  `05-macos-submillisecond-poll`.
- **E-7206 AUTH CHIP ERROR** = the MFi coprocessor on **I2C4** (`0x021F8000`, slave 0x10) — `xdj-mfi.c`.
- **Audio** (`xdj-audio.c`, SDMA/SSI/AUDMUX; sonifier → SDMA ch1 = MasterOut). Audible needs routing
  = DECK + gains up. Stutter = guest ALSA xruns (rbp's ring is 2×64 frames); the pacer thread completes
  one period per 1.45 ms. Check `ALSA: Resolve xrun` on the console; `XDJ_AUDIO_STATS=1`.
- **Network**: eth0 = user NAT, or the LAN / a Mac-only network (`… mac`, for rekordbox on the
  same Mac) via the optional socket_vmnet daemon (`tools/net/install-vmnet-bridge.sh`; `-netdev stream` to `/var/run/socket_vmnet.bridged.<if>`),
  picked per power-on (`XDJ_NET` app / `XDJNIC` watch.sh). Never QEMU's `vmnet-*` (needs root).
- **Root shell**: boot the SD test bed (it blanks root's password); a NAND unit keeps the stock hash.

## Workflows (→ skill)
- Drive a running app from an agent: the MCP server (`claude mcp add --transport http xdj-rx
  http://127.0.0.1:8765/mcp`; tools in `docs/standalone-app.md` §7a).
- Run / boot / USB / inject / screenshot / audio / service modes → **`run-xdj-emulator`**.
- Map a control: GPIO → **`map-gpio-control`**; sub-ucom → **`map-subucom-{button,fader,knob,rotary}`**.
- Reverse-engineer `rbp` → **`rbp-binary-internals`**.
- `.UPD` format → **`upd-firmware-format`**; build/install custom firmware → **`custom-firmware`**;
  feature mods (xdjmod framework, test loop, the reproducible 3.00 release `tools/fw/build-release.sh`)
  → **`firmware-mods`** (`docs/firmware-mods.md`); hours-long stress test of a firmware + the generated
  test sticks → **`stress-test-firmware`**; releases, the patch file and the patcher →
  **`release-patcher`** (`docs/releases.md`).
- QEMU device models and patches → **`qemu-imx6-patches`**; the SD test-bed image → **`build-xdj-sd-image`**.
- Desktop app (shell, host, packaging) → **`build-xdj-app`**; the virtual panel → **`xdj-virtual-panel`**.
- Tests (`docs/testing.md`): `cargo test --release` (crates/xdj-fw), `tools/xdj-panel/test_panel_parity.py`
  (after touching `panel.rs` or `server.py`), `tools/test/regress.py` (end to end; `--app` for the bundle), `tools/test/stress.py` (before a firmware release).

## Gotchas
- TCG boots are load-sensitive: close REA/Ghidra before booting; don't run two emulators at once
  unless needed (`regress.py` uses private ports/runtime dirs for that).
- **Never hammer the gdbstub** (~25 rapid RSP halts → rbp watchdog crash). Space reads; RSP reads of
  rbp globals EFAULT unless the halt lands in rbp's context. Resume with the monitor `cont`.
- File-mode injection: a one-shot `b`/`a` left in `/tmp/subucom_ctrl` next to holds re-fires every 30
  frames — `inj.py pulse` rewrites the holds alone right after.

## Git
- Commit only when asked; branch off `main` first. End commit messages with the attribution line given
  in the session's system reminder.
