# Standalone XDJ-RX app — developer guide

Enough to work on the standalone desktop app with no prior context: what the app is, how the
pieces fit, the runtime data flow, how to build / run / debug / package it, and the pitfalls to
avoid.

For the operational cheat-sheets, see the skills `build-xdj-app` and `xdj-virtual-panel` in
`.claude/skills/`. The hard constraint from [../CLAUDE.md](../CLAUDE.md) applies throughout:
**never modify the guest (`rbp`, kernel, rootfs)** — everything here is host-side.

> **Keep this doc in sync.** It states facts that live in code and will drift. Update the relevant
> section (and the `build-xdj-app` / `xdj-virtual-panel` skills) when you change any of:
> - **ports / sockets** (8765, 6080, `*.sock`) — §6, §11, `qemu_args` / `Config::from_env`;
> - **env vars** — §11, `Config::from_env` + the shell;
> - the **prep pipeline / cache key / stdout contract** — §4, `prep_firmware`;
> - **QEMU args** (machine, memory, loader addresses, devices) — §6, `qemu_args`;
> - the **`xdj-host prep` → stdout → `XDJ_FW_DIR`** contract — §4;
> - **dialog / capability / ACL** wiring or the Tauri major version — §9, `capabilities/default.json`;
> - **packaging** (bundle layout, signing, relocation) — §13, `packaging/build-macos-app.sh`;
> - **startup/teardown** behaviour — §3, §10.
> Version-specific claims (e.g. "remote capabilities are stable on Tauri 2.12") must be
> re-verified on a Tauri upgrade.

---

## 1. What it is (mental model)

A user drops a Pioneer `.UPD` firmware onto the app. The app decrypts and converts it into a
bootable image, boots it under a patched QEMU, and shows the live device screen + a virtual control
panel in a native window. No terminal, no `/tmp` rendezvous files, no bundled firmware.

It is **three cooperating processes**, not one:

```
┌──────────────────────────────────────────────────────────────────────┐
│ XDJ-RX.app  (the thing the user double-clicks)                         │
│                                                                        │
│  app/shell  ── Tauri (Rust + OS webview) ───────────────────────────┐ │
│    • window "setup"  = app/ui/index.html  (drop firmware / key UI)   │ │
│    • window "main"   = external http://127.0.0.1:8765/  (the panel)  │ │
│    • spawns + supervises:                                            │ │
│        prep:  `xdj-host prep <upd>`   (synchronous, returns a dir)   │ │
│        run:   `xdj-host`              (long-lived orchestrator)      │ │
│  ───────────────────────────────────────────────────────────────────┘ │
│                                                                        │
│  app/host  ── `xdj-host`  (Rust orchestrator) ──────────────────────┐  │
│    • preflight, then spawns + supervises:                           │  │
│        - qemu-system-arm  (-M sabrelite, headless, VNC+QMP+chardevs)│  │
│        - src/panel.rs  (the virtual panel, in-process, HTTP :8765)  │  │
│        - an in-process VNC(unix) → WebSocket(:6080) bridge          │  │
│    • tears all of them down together on exit/parent-death           │  │
│  ───────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────┘
```

Why split shell vs host? The **shell** is the GUI (windows, drag-drop, dialogs, key prompt). The
**host** is a headless orchestrator that could also run from a terminal (`xdj-host` with no GUI).
Keeping them separate means the emulator plumbing has no Tauri dependency and is testable alone.

---

## 2. Repository layout

| Path | What |
|---|---|
| `app/shell/` | The Tauri app. `src/main.rs` = windows, commands, firmware/key flow. `tauri.conf.json`, `capabilities/default.json`. |
| `app/host/` | `xdj-host` orchestrator. `src/main.rs` = Config, preflight, prep, spawn/supervise, USB insert, VNC-WS bridge. |
| `app/ui/` | `index.html` (the setup page), vendored `novnc/`, `node_modules/audio-knobs` (knobs lib). |
| `tools/xdj-panel/` | `panel.html` / `logs.html` (the control UI, served by `app/host/src/panel.rs`) + `server.py` (the same server in Python, for the dev/debug launcher; `test_panel_parity.py` keeps the two in step). See skill `xdj-virtual-panel`. |
| `crates/xdj-fw` (`firmware.rs`, `nand.rs`, ...) | Firmware prep (decrypt, ISO read, kernel blobs, update stick) + the unit's NAND (init / installed? / load kernel), in-process. Shared library of `xdj-host` and the `xdj` CLI (`xdj fw …`, `xdj prepare`, `xdj nand …`; custom-firmware builds: [custom-firmware.md](custom-firmware.md)). |
| `qemu/` | The patched-QEMU build script (`build-qemu.sh`) + `patches/01..05` (stock QEMU files) + `qemu/xdj/` (the device models). |
| `packaging/` | `build-macos-app.sh` (assembles `dist/XDJ-RX.app`), `collect-licenses.py`. |

App-data (runtime, never in the repo): `~/Library/Application Support/XDJ-RX/` on macOS
(`$XDG_DATA_HOME/xdj-rx` elsewhere). Contains `key/aes256.key`, per-firmware cache dirs keyed by
hash, `run/` (sockets + logs when packaged), `host.log`, `last-firmware`.

---

## 3. Startup sequence (what happens, in order)

1. **Shell launches** (`app/shell/src/main.rs::main`). `resolve_startup_fw` decides the first screen:
   boot straight into a ready firmware if `XDJ_FW_DIR` or the `last-firmware` pointer names a
   complete dir (and, in dev only, `build/qemu`); otherwise open the **setup** window.
2. **User provides firmware** — drag-drops a `.UPD`/`.zip` onto the window, or clicks the card
   (`browse_firmware` opens a native dialog). A non-firmware drop, or the key **Browse…** button
   (`browse_key`), is treated as the decryption key.
3. `on_firmware_dropped` → **`prep_firmware`** (shell) runs `xdj-host prep <upd>` synchronously and
   reads the prepared dir from its **stdout** (see §4).
4. → **`start_host`** spawns `xdj-host` with `XDJ_FW_DIR=<dir>`, redirects its stdio to
   `app-data/host.log`, and waits up to 60 s for the panel port (8765) to open (`wait_port`).
5. `xdj-host` (run mode): `preflight` → start the in-process panel server (`panel.rs`) → the VNC→WS bridge and
   the `power.sock` listener → supervise. **A blank unit is provisioned at once** (§4a): a
   firmware just dropped powers on into the firmware's own ALL update, then boots from NAND. An
   installed unit: the shell passes `XDJ_AUTOSTART=0`, so QEMU waits for the panel's POWER switch /
   File > Power, or the *Power On at Launch* preference (**on by default**), to send `on`.
   Without `XDJ_AUTOSTART=0` (CLI use) it powers on at once and runs `maybe_insert_usb` (§8).
   The panel binds :8765 at once, so `wait_port` returns and the shell opens the **main**
   window at `http://127.0.0.1:8765/?vnc=ws://127.0.0.1:6080/`.
6. The panel (`panel.html`) connects to the screen over noVNC (the `?vnc=` WebSocket) and drives
   controls via the server's HTTP endpoints.

If step 4 times out, the shell shows *"the emulator panel didn't come up — see host.log"*. That
almost always means `xdj-host` exited at preflight (so nothing bound the port) — check `host.log`.

---

## 4. Firmware prep & caching (`xdj-host prep`)

`app/host/src/main.rs::prep_firmware`. Cache key = first 16 hex of the `.UPD` SHA-256 →
`app-data/<hash>/`. If `vmlinux.bin + tramp.bin + atags.bin + fw.iso + update-stick.img +
prep-v3.stamp` already exist there, it's a **cache hit** (instant). Otherwise, in-process
(`xdj_fw::firmware`, ~1 s):

1. decrypt the `.UPD`/`.zip` → `<dir>/fw.iso` — needs the AES key (§5).
2. the firmware's kernel + SABRE-SD boot blobs (what provisioning boots, host-loaded).
3. `<dir>/update-stick.img` — a FAT16 superfloppy with `XDJRX.UPD`, the stick the provisioning
   update reads.

`PREP_STAMP` (`prep-vN.stamp`) versions the layout: bump it (and the shell's `fw_complete`) when
prep's output changes, so older caches fall back to the drop screen and get rebuilt.

**Contract that matters:** `xdj-host prep` prints **only the prepared dir path** to stdout (the
shell reads stdout as `XDJ_FW_DIR`); all diagnostics go to **stderr**. The shell additionally
reads the **last non-empty stdout line** defensively. Anything else on stdout on a cache miss
becomes a bad `XDJ_FW_DIR`, preflight then fails, and the app shows "panel didn't come up" on the
first run only.

## 4a. The unit: provisioning and NAND boot (`BootKind`)

The app emulates the real unit: **no SD card**, everything lives in the emulated NAND
(`<dir>/nand-flash.img`, `XDJ_NAND_IMG`) + the panel MCU flash (`<dir>/panelcom-flash.bin`,
`XDJ_PANELCOM_IMG`), always with `XDJ_BOOT_MEDIA=nand` (SBMR1 straps → fw_printenv reads mtd2).

- **Provision** (no installed system; every power-on until one succeeds): `nand::init`
  (erased NAND + the firmware's `fw_env.txt` = u-boot's default env), erased MCU flash, boot the
  prep blobs with `XDJ_BOOT_UPDATE=1 XDJ_UPDATE_TYPE=20` (ALL). `provision_watch` inserts the stick
  on USB1 2 s after the console's `### USB update mount. ###`, then waits for
  `### USB update success.` → kill QEMU, write `provisioned.stamp`, boot **Normal**. On
  `### USB update error.` QEMU is left running (the screen shows the updater's error). The ALL
  update also writes `release`/`rev_*` into the env, which UTILITY → VERSION No. shows.
  Details of the updater, the MCU bootloader and the NAND model:
  [firmware-update-and-nand.md](firmware-update-and-nand.md).
- **Installed?** `provisioned.stamp`, else the NAND env has `release=` (written by the ALL update's
  last step; the factory env lacks it) — the updater ends on "turn off the power", so the console
  success line usually never prints and this is what catches a finished install.
- **Normal / Update**: each power-on runs `nand::boot` → `<dir>/nand-boot` (u-boot's
  `bootcmd_nand` host-side: `kernel_bank` → that bank's uImage → blobs + bootargs), so a firmware
  update done inside the emulator boots the new kernel. Update = `XDJ_BOOT_UPDATE=1`.
- `power.sock` status: `on` (Normal), `on update` (Provision/Update), `off`.
- **POWER off = the unit's standby handshake** while rbp runs: the panel presses the POWER key
  (`0x5d.0`, active-low), rbp prepares and sends TX `0x11='S'`, the panel cuts power; rbp's own AUTO
  STANDBY ends the same way. Otherwise (booting, update mode, no answer in 10 s) a hard cut
  (`docs/service-modes.md`).
- **Install Firmware Update** (File > Install Firmware Update…): native file dialog for a `.UPD`/`.zip`
  → panel `POST /install-update <path>` builds `<runtime>/install-update.img` (FAT16, `XDJRX.UPD`),
  power-cycles into update mode (`on update`, the NAND kernel) and inserts it on USB1 when the
  initramfs prints `USB update mount` — the provisioning choreography, but a USER update. The user
  follows the updater and power-cycles at "Turn the power off/on". Works for official and
  `xdj fw build` images (`docs/custom-firmware.md`).
- **Test mode** (File > Power On in Test Mode): panel `POST /power on test` holds DECK 1 MEMORY +
  TEMPO RANGE (`B 13 04` + `B 12 08`), power-cycles if on, boots Normal; the holds reach the fresh
  QEMU on the ctrl-socket connect, before rbp's first frame. Released 5 s after rbp's first
  non-blank LED frame (`_test_mode_boot`).

---

## 5. Firmware decryption key

Pioneer `.UPD` files are AES-encrypted. The key is **user-supplied and never bundled**. Stored at
`app-data/key/aes256.key`, content-sniffed at use (`resolve_firmware_key`): a 64-hex string is the
key itself; anything else is treated as a losetup passphrase file. The setup page's key box (paste
64-hex → `set_key_hex`, or drop/Browse a file → `store_key_file`) writes it. In dev, without that
file, `XDJRX_AES_KEY` in the environment or `XDJRX_AES_KEY=` in the repo's `.env` (written by
`tools/fetch_key.py`) is used. No key anywhere → a clear "add your key" error.

---

## 6. QEMU invocation (`qemu_args`)

Headless `-M sabrelite` (the XDJ-RX is a SABRE-SD board), `-m 256`, `-smp 2` (override `XDJSMP`),
`-display none`. Boot blobs are loaded with `-device loader,...` (vmlinux @0x10008000, atags
@0x10000100, tramp @0x10000000) from the prep dir (Provision) or `nand-boot/` (§4a); no SD
drive. A **USB-2.0 hub** is added but the
storage is **not** cold-plugged — it's inserted after `rbp` starts polling (§8), avoiding a mount
race. Per-instance channels live in the runtime dir:

| flag | socket | consumer |
|---|---|---|
| `-chardev socket,id=xdjctrl` | `ctrl.sock` | sub-ucom control snapshots + LED lines back (panel) |
| `-chardev socket,id=xdjkey` | `key.sock` | MAIN-UCOM GPIO keys (panel) |
| `-qmp unix:` | `qmp.sock` | lifecycle + USB `drive_add`/`device_add` (host and panel) |
| `-vnc unix:` | `vnc.sock` | framebuffer → the WS bridge → noVNC |
| `-serial file:` | `console.log` | kernel console |

Audio backend is chosen by `pick_audio` (coreaudio on macOS, etc.; `XDJAUDIO`/`none` override).
Packaged, `-L <qemu-bios>` points the relocated qemu at the bundled keymaps (else `-vnc` fails).

---

## 7. The panel UI

The **main** window loads `http://127.0.0.1:8765/` — served in-process by `xdj-host`
(`app/host/src/panel.rs`, axum): the pages, noVNC, knobs, the control routes, the LED and log SSE
streams. It speaks to QEMU's `xdjctrl`/`xdjkey` chardevs directly (one connection task each;
snapshots are composed under one state lock, so writes stay ordered) and powers the unit through
the host's own power channel. It is a port of `tools/xdj-panel/server.py`'s socket mode, which
stays as the dev/debug-launcher server (`/tmp` file mode, HMP screendump, calibration); after
changing either, run `tools/xdj-panel/test_panel_parity.py` (same scripted session against both,
every snapshot / reply / LED / log line compared). The UI (`panel.html`) is an original,
copyright-free design (no device photography). Full detail is in the
**`xdj-virtual-panel`** skill; the essentials:
- Screen = noVNC over the `?vnc=` WebSocket (the host's VNC-unix→WS bridge on :6080).
- Controls = HTTP POSTs (`/ctrl`, `/browse`, `/input`, `/mainkey`, `/usb`) → the control channels.
- Layout is absolute (grid in design units); widgets are the `audio-knobs` npm lib + custom jog/faders.
- The window holds only the deck (aspect-locked 2110:1152); everything else is the **native menu
  bar** the page builds (File / Edit / Mixer / Deck 1 / Deck 2 / Color FX / FX / Master / View /
  Window), the pop-out screen (double-click) and the Sub uCom / System log windows (`logs.html`).
- **Power**: top-left POWER switch with a red LED; `POST /power` → host `power.sock`. USB images
  loaded while off are queued and inserted once the guest is ready; *Enter Firmware Update Mode*
  powers on with `XDJ_BOOT_UPDATE=1`.

---

## 8. USB media

**At boot:** `maybe_insert_usb` (host) optionally inserts `XDJ_USB` on `XDJ_USB_SLOT` once `rbp` is
polling — the console's `Resolve xrun` plus `XDJ_USB_DELAY` (default 6 s) — at hub port 1.2 = USB1
or 1.1 = USB2. The attach alone wakes HOST1 (`xdj-usbnc.c`). It never creates `/tmp/usb_insert` /
`/tmp/usb2_insert` (it deletes them): `xdj-panel.c`'s files drive `rbp`'s active-low USB fault flags,
and asserting one causes "USB Error. Remove the device.". **At runtime:** the panel's USB LOAD
buttons POST `/usb <slot> <path>` and the server does the same `drive_add`/`device_add` over QMP,
**ejecting any previous insertion first** (USB STOP only unmounts in the firmware; the QEMU device
stays plugged). Loads made while powered off are queued and inserted once the guest is ready.
Read-only (`snapshot=on`) unless `XDJ_USB_WRITE=1` (boot-time insert only). The browser shows
USB1; MASTER REC records to USB2. Full detail: [usb.md](usb.md).

---

## 9. Native file dialogs (`tauri-plugin-dialog`) — mind the window origin

There are two correct ways to open a dialog, and **which one you must use depends on the window**:

- **Local window** (`setup`, loaded from embedded `index.html`): app/custom commands work. Use a
  `#[tauri::command]` that calls the dialog plugin's **Rust** API — but with the **non-blocking**
  `pick_file(|path| …)` form. `blocking_pick_file()` deadlocks the macOS main thread and freezes the app.
- **Remote window** (`main`, an external `http://127.0.0.1:8765` page): app/custom commands are
  **blocked by the ACL for remote origins** ("Command X not allowed by ACL"). Use the dialog
  plugin's **JS** API (`window.__TAURI__.dialog.open`), and in `capabilities/default.json` grant
  `dialog:allow-open` **and** a `remote` block listing `http://127.0.0.1:*` / `http://localhost:*`
  (without `remote`, the external page gets no IPC at all). `remote` capabilities are stable on
  Tauri 2.12 (no `unstable` feature needed).

---

## 10. Lifecycle & teardown

The host supervises with a `tokio::select!` loop over: power commands (`power.sock`), qemu exit,
the panel server failing, Ctrl-C, SIGTERM and **parent death** (it polls `XDJ_PARENT_PID` with
`libc::kill(pid,0)`). **QEMU exiting only means "powered off"** (the loop continues; the panel can
power on again); any of the others tears everything down. The
shell stops the host on the emulator window's `CloseRequested`/`Destroyed` and on `ExitRequested`,
via **SIGTERM** (lets the host clean up; SIGKILL as a backstop). Net effect: **no orphaned QEMU**,
however the app dies. Only one instance runs at a time (ports 8765/6080 are fixed; `start_host`
refuses if 8765 is busy, which is a clearer error than a 60 s timeout).

---

## 11. Configuration (env vars)

Resolved in `app/host/src/main.rs::Config::from_env` (and a few in the shell). All optional unless
noted; sensible defaults for both dev (repo) and packaged (`XDJ_RESOURCES`) layouts.

| Var | Meaning |
|---|---|
| `XDJ_RESOURCES` | Packaged resources root (the `.app`'s `Contents/Resources`). Its presence switches dev↔packaged defaults. |
| `XDJ_REPO` | Dev repo root (when not packaged). |
| `XDJ_FW_DIR` | Prepared firmware dir (prep output + the unit's flash, §4a). Set by the shell after prep. |
| `XDJ_NAND` | Override the NAND flash image (default `<fw_dir>/nand-flash.img`). |
| `XDJ_QEMU` | Override the qemu binary. |
| `XDJ_RUNTIME_DIR` | Sockets/logs dir (default: app-data `run/` packaged, `build/run` dev). Keep it short — macOS unix socket paths ≤ 104 chars. |
| `XDJ_PARENT_PID` | Shell PID; the host exits if it dies (orphan cleanup). |
| `XDJ_PANEL_PORT` / `XDJ_VNC_WS_PORT` | Default 8765 / 6080. |
| `XDJ_USB` / `XDJ_USB_SLOT` / `XDJ_USB_DELAY` / `XDJ_USB_WRITE` | Boot-time USB auto-insert: image or `/dev/diskN`, `usb1`/`usb2` (default `usb2`), settle seconds (default 6), `1` = persist writes. |
| `XDJAUDIO` / `XDJSMP` | Audio backend / vCPU count (default 2; a NAND boot needs ≥ 2). |
| `XDJ_AUTOSTART` | `0` = start powered off (the shell sets this); default/other = power on at start. |
| `XDJ_CAL_REF` | Dev-only: reference photo for the panel's `?cal` calibration overlay (served at `/cal-ref`; 404 if unset). |
| `XDJ_FORCE_SETUP` | Shell: always show the drop screen (testing). |

The host sets the QEMU-side variables itself per power-on: `XDJ_BOOT_MEDIA=nand`, `XDJ_NAND_IMG`,
`XDJ_PANELCOM_IMG`, and `XDJ_BOOT_UPDATE` / `XDJ_UPDATE_TYPE` for update mode and provisioning
([firmware-update-and-nand.md](firmware-update-and-nand.md) §6).

---

## 12. Build, run, debug

```bash
# Dev build + run (shell uses app/host/target/debug/xdj-host via XDJ_REPO)
( cd app/host && cargo build )
( cd app/shell && cargo build && cargo run )

# Run the orchestrator headless (no GUI), against a prepared firmware:
XDJ_REPO=$PWD XDJ_FW_DIR=<dir> app/host/target/debug/xdj-host
# then open http://127.0.0.1:8765/?vnc=ws://127.0.0.1:6080/

# Prep a firmware by hand (prints the dir):
XDJ_REPO=$PWD app/host/target/debug/xdj-host prep /path/to/firmware.UPD
```

Logs: the shell writes `app-data/host.log` (the orchestrator's stdio); the host writes
`<runtime>/qemu.log`, `<runtime>/console.log`. **When something "doesn't
come up", read these first.** Iterate on the panel layout with headless Chrome screenshots (see the
`xdj-virtual-panel` skill) — but always re-verify in the real app, because **WebKit (WKWebView) and
Chrome render the layout differently** (flex-fill + SVG collapses in WebKit).

---

## 13. Packaging (macOS `.app`)

`packaging/build-macos-app.sh` → `dist/XDJ-RX.app`. It builds both release binaries and bundles:
the patched `qemu-system-arm` with its Homebrew dylibs **relocated** (`dylibbundler -p
@executable_path/libs/`, duplicate `LC_RPATH`s stripped), the keymaps, the panel pages +
noVNC + knobs dist. **No Python**: prep, the NAND and the panel server all run inside `xdj-host`.
**Licensing:** the repo is GPL-2.0-or-later; `packaging/collect-licenses.py` copies every bundled
component's license text into `Resources/licenses/` (QEMU, each Homebrew dylib keg, noVNC, knobs, all
Rust crates) — see `THIRD_PARTY_NOTICES.md`. QEMU is built `--disable-libssh` so OpenSSL (Apache-2.0,
GPLv2-incompatible) is not bundled. Test the bundle with `tools/test/regress.py --app dist/XDJ-RX.app`. Everything is **ad-hoc code-signed inside-out** (`codesign --force -s -`) because Apple
Silicon won't run a binary whose signature dylibbundler invalidated. Prereqs: Rust, a built patched
qemu (`qemu/build-qemu.sh`), `dylibbundler`.

**Fast iteration on an already-built bundle** (no full repackage): copy the changed artifact into
`Contents/MacOS/XDJ-RX` (shell), `Contents/Resources/xdj-host` (host), or
`Contents/Resources/panel/*` (panel pages), then re-sign the binary and `--deep` the app.
`index.html` and the capability are **embedded in the shell binary** at build time — rebuild the
shell to change them.

This is **ad-hoc**, not Developer-ID signed or notarized: it runs locally but Gatekeeper will warn
other users (§15).

---

## 14. Known pitfalls (don't regress)

- **prep stdout must be only the dir path** (§4). Anything else → bad `XDJ_FW_DIR` → "panel didn't come up", first run only.
- **`blocking_pick_file` freezes the app** on macOS — use the non-blocking callback form (§9).
- **App commands are ACL-blocked for remote-origin windows** — use the dialog plugin's JS API + a `remote` capability for the panel (§9).
- **WebKit ≠ Chrome** for the panel layout — grid `fr` tracks, not flex-fill + SVG, in WebKit (§12).
- **Orphaned QEMU** if teardown isn't wired — parent-death watch + window-close + SIGTERM (§10).
- **Packaged shell must not auto-boot the dev `build/qemu`** — `resolve_startup_fw` gates that behind `app_resources().is_none()`.
- **macOS unix socket path ≤ 104 chars** — keep `XDJ_RUNTIME_DIR` short.
- **USB re-load must eject first** — USB STOP is only a firmware unmount (§8).

---

## 15. Not yet

- **Notarization + Developer-ID signing**, so other users can run it without Gatekeeper warnings.
- **Windows and Linux packaging** (the orchestrator is mostly portable; QEMU bundling and the
  dialog/socket bits need per-OS work).
- **An Intel (x86_64) macOS build** — the bundle contains native binaries of the machine that
  built it, which in practice is Apple Silicon.

## 16. See also
Skills: `build-xdj-app`, `xdj-virtual-panel`, `run-xdj-emulator`, `qemu-imx6-patches`,
`upd-firmware-format`, `custom-firmware`. Docs: [usb.md](usb.md),
[firmware-update-and-nand.md](firmware-update-and-nand.md), [custom-firmware.md](custom-firmware.md),
[service-modes.md](service-modes.md), [subucom-led-map.md](subucom-led-map.md),
[testing.md](testing.md), [emulator-status.md](emulator-status.md).
