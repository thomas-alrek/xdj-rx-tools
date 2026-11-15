---
name: build-xdj-app
description: Build, run, package and debug the standalone XDJ-RX desktop app - the Tauri shell + xdj-host orchestrator (with the in-process panel server) that lets a user drop a .UPD firmware and run the emulator with no /tmp rendezvous. Use when working on app/shell (Tauri/Rust), app/host (the orchestrator), firmware prep/caching, the key prompt, native file dialogs, USB mount from the app, packaging (dist/XDJ-RX.app), or any "the app won't boot / panel didn't come up" issue.
---

# Standalone XDJ-RX app (`app/`)

A single desktop app: drop a `.UPD` → it prepares the boot image and runs the emulator in a native
window. Governed by `docs/standalone-app-roadmap.md`. **Never patches `rbp`/kernel/rootfs** — all
host-side. See also skills [xdj-virtual-panel], [run-xdj-emulator], [qemu-imx6-patches],
[upd-firmware-format], [build-xdj-sd-image].

## Pieces
- **`app/shell/`** — the Tauri app (Rust, OS webview). Two windows: **`setup`** (local
  `app/ui/index.html`: drop/click a firmware, key prompt) and **`main`** (external URL to the panel
  served by `xdj-host`). `src/main.rs` has the commands, window lifecycle, and firmware/key flow.
- **`app/host/`** — **`xdj-host`**, the orchestrator (Rust). Two modes:
  `xdj-host prep <upd>` (prepare+cache, prints the dir) and `xdj-host` (run: preflight → panel server +
  VNC↔WS bridge + QEMU, supervise, tear everything down together).
- **`app/host/src/panel.rs`** — the virtual-panel server, in-process (port of `tools/xdj-panel/server.py`, which stays for dev; keep them identical with `tools/xdj-panel/test_panel_parity.py`). See [xdj-virtual-panel].
- **`app/ui/`** — `index.html` (setup page), vendored `novnc/`, `node_modules/audio-knobs`.
- **`packaging/build-macos-app.sh`** — assembles `dist/XDJ-RX.app`.

## Flow (drop a firmware → running emulator)
1. `on_firmware_dropped` (drag, or `browse_firmware` command) → `prep_firmware` → `start_host` →
   `show_emulator` (opens the `main` window at `http://127.0.0.1:8765/?vnc=ws://127.0.0.1:6080/`).
2. **prep** (`xdj-host prep`, ~1 s, in-process `xdj_fw::firmware`, crate `crates/xdj-fw`): decrypt → `fw.iso` → boot
   blobs (the firmware's kernel) → `update-stick.img` (FAT16, `XDJRX.UPD`). Ports of
   `xdjrx_upd.py`/`qemu_prepare.py`/`xdj_update_stick.py` (kept as dev tools); after touching either
   side run `qemu/test_rust_parity.sh <fw.zip> [provisioned-nand.img]` (byte-for-byte). Cached in **app-data by firmware hash** (`~/Library/Application Support/XDJ-RX/<hash>/`)
   with `prep-vN.stamp` (bump it + the shell's `fw_complete` when prep output changes).
3. **run** (`xdj-host`): `preflight` → panel server (`panel.rs`) → VNC↔WS bridge → `tokio::select!`
   supervising qemu/panel/ctrl-c/parent-death/provisioning.
4. **The unit is NAND-only (no SD card)** — `BootKind` in `app/host/src/main.rs`, details in
   `docs/standalone-app.md` §4a. No `provisioned.stamp` → **Provision** at once (regardless of
   `XDJ_AUTOSTART`): `nand::init` (`crates/xdj-fw/src/nand.rs`, port of `xdj_nand.py`) + update mode with `XDJ_UPDATE_TYPE=20` (ALL), stick on USB1
   after `### USB update mount.`, `### USB update success.` → stamp → **Normal**; "installed" is
   also read from the NAND env (`release=`), since the updater stops on "turn off the power" and the
   success line usually never prints. Normal/Update power-ons run `nand::boot` first (kernel_bank → `nand-boot/` blobs). Always
   `XDJ_BOOT_MEDIA=nand XDJ_NAND_IMG=<dir>/nand-flash.img XDJ_PANELCOM_IMG=<dir>/panelcom-flash.bin`.
   The SD image (`qemu/build-nand.py`) is dev-only now (watch.sh / relaunch.sh / root shell).

## Sockets, ports, env
- Per-instance runtime dir sockets: `qmp.sock` (QMP lifecycle + USB), `vnc.sock` (framebuffer),
  `ctrl.sock`/`key.sock` (Phase-1 control chardevs). **No HMP monitor in the app** — USB mount uses
  QMP `human-monitor-command`.
- Ports (hardcoded ⇒ one instance): panel **8765**, VNC-WS bridge **6080**. `start_host` refuses if
  8765 is busy (clearer than a 60 s timeout).
- Env: `XDJ_RESOURCES` (packaged root) / `XDJ_REPO` (dev root); `XDJ_FW_DIR` (prepared dir),
  `XDJ_NAND`, `XDJ_QEMU`, `XDJ_PANEL_PORT`; `XDJ_PARENT_PID` (shell PID, for orphan cleanup);
  `XDJ_USB`/`XDJ_USB_SLOT`/`XDJ_USB_DELAY` (boot auto-insert); `XDJ_NOVNC_DIR`/`XDJ_KNOBS_DIR`
  (no longer used: the panel finds the bundled assets itself). Firmware key: `app-data/key/aes256.key`, content-sniffed
  (64-hex → `XDJRX_AES_KEY`, else `--key-file`); **never bundled**, falls back to repo `.env` in dev.

## Build & run
```
# dev (from repo; shell uses app/host/target/debug/xdj-host via XDJ_REPO)
( cd app/host && cargo build ) && ( cd app/shell && cargo build && cargo run )
# release binaries
( cd app/host && cargo build --release ); ( cd app/shell && cargo build --release )
# package (macOS): bundles patched qemu + dylibs, panel pages, novnc, knobs dist and every license
# text (packaging/collect-licenses.py -> Resources/licenses/); ad-hoc signs. Needs dylibbundler.
# Then test the bundle itself: tools/test/regress.py --app dist/XDJ-RX.app
packaging/build-macos-app.sh        # -> dist/XDJ-RX.app
```
**Refresh a built bundle after a code change** (faster than a full repackage): copy the changed
piece into `dist/XDJ-RX.app/Contents/{MacOS/XDJ-RX, Resources/xdj-host, Resources/panel/*}` then
**re-sign** (`codesign --force -s - <binary>`; `codesign --force --deep -s - dist/XDJ-RX.app`).
`index.html` + the capability are **embedded in the shell binary** at build time — rebuild the shell
to update them.

## Native file dialogs (tauri-plugin-dialog) — two paths, by window origin
- **Local `setup` window** (firmware, key): app commands `browse_firmware`/`browse_key` call the
  dialog plugin's **Rust** API. **Use the non-blocking `pick_file(|path| …)` form** —
  `blocking_pick_file()` deadlocks the macOS main thread and **freezes the app**.
- **Remote `main` panel** (USB LOAD, external `http://127.0.0.1:8765`): **app/custom commands are
  blocked by ACL for remote origins** ("Command X not allowed by ACL"). Use the dialog plugin's
  **JS** API (`window.__TAURI__.dialog.open`), grant `dialog:allow-open`, and add a **`remote`**
  block (`http://127.0.0.1:*`, `http://localhost:*`) to `app/shell/capabilities/default.json` so the
  external page may use IPC at all. `remote` capabilities are stable on Tauri 2.12 (no `unstable`).

## Power control (the app starts powered OFF, unless the unit is blank)
The shell launches `xdj-host` with `XDJ_AUTOSTART=0`: the panel server and VNC bridge come up, QEMU
does not, except that a blank unit is provisioned at once. The panel's *Power On at Launch* pref
defaults **on** (`PREF_DEFAULT` in panel.html). File > *Install Firmware Update…* = panel `POST /install-update <.UPD path>` (builds
`<runtime>/install-update.img`, power-cycles into update mode, inserts it on USB1 at `USB update mount`).
File > *Power On in Test Mode* = panel
`POST /power on test` (holds MEMORY + TEMPO RANGE across a cold boot, releases them after rbp's first
LED frame). `xdj-host` owns QEMU's lifecycle and listens on **`<runtime>/power.sock`** (line protocol:
`on` | `on update` | `off` | `status` → one line `on` / `on update` / `off`). `on update` sets
`XDJ_BOOT_UPDATE=1` for QEMU (`xdj-board.c` (was patch 19), firmware-update boot). `off` kills QEMU instantly (a power
cut — the app's nand image is persistent writeback, like the real unit's flash). Each power-on
deletes the old sockets + `console.log`. QEMU exiting on its own = powered off; only the panel server
dying, SIGTERM, Ctrl-C or the shell vanishing end `xdj-host`. Without `XDJ_AUTOSTART=0` (CLI use)
it powers on at start as before. The panel side (button, prefs, queued USB) is in [xdj-virtual-panel].
Testing a second instance next to a running app: `XDJ_RUNTIME_DIR=/tmp/<short>` (socket paths ≤ 104
chars), `XDJ_PANEL_PORT`/`XDJ_VNC_WS_PORT`, `XDJAUDIO=none`, and a *copy* of the nand (`XDJ_NAND`) —
but two emulators contend for the host (one stalled the other's boot), so keep it short.

## Window = the deck
`main` is locked to the deck's aspect **2110:1152** (`DECK_AR`, `lock_aspect` via
`NSWindow.contentAspectRatio` in `on_page_load`) and sized to it, so the deck fills the window with
no letterbox; `panel.html` draws the stage edge to edge (no outer padding/border/radius).
`contentAspectRatio` only governs live edge-drags — tiling, the green button's Fill/zoom and
programmatic sizes bypass it — so `on_window_event(Resized)` also runs `snap_aspect()`: shrink to the
largest deck-ratio rect inside the chosen size (never grow). It targets the ratio for the **webview's** size (`window.webviews()[0].size()`), carrying the difference to the reported inner size (the title bar on macOS). Full screen is exempt (bands remain,
filled with the deck background). Same for the pop-out `screen` window at 800:480.

## Native menu bar + extra windows
The panel page builds the **app menu** itself via the Tauri JS menu API (`buildAppMenu` in
`panel.html`; permission `core:menu:default` in `capabilities/default.json`) — the main window holds
only the virtual deck. View > *Sub uCom Log* / *System Log* open webview windows `log-subucom` /
`log-console` (`logs.html`); the system log is the QEMU serial `console.log` in the runtime dir, which
the panel reads from the runtime dir. Closing `main` closes `screen` and every `log-*` window.

## Pop-out screen window (`screen`)
Double-clicking the panel's screen (or View > Pop Out Screen) creates a second webview window, label **`screen`**, from the remote
page via the JS API (`window.__TAURI__.webviewWindow.WebviewWindow`) at `/?screen=1`. Needs
`core:webview:allow-create-webview-window` + `core:window:allow-set-focus` in
`capabilities/default.json` (permissions are compiled into the shell — **rebuild the shell**, copying
`panel.html` isn't enough). `main.rs`: `on_page_load` locks the window to the 800x480 framebuffer
aspect with `NSWindow.contentAspectRatio` (objc2-app-kit, macOS only; Tauri has no aspect option),
and closing `main` closes `screen` too.

## Gotchas (all bit us; all fixed — don't regress)
- **prep stdout must be ONLY the dir path.** The shell reads `xdj-host prep` stdout as `XDJ_FW_DIR`.
  The prep scripts inherit stdout, so on a **cache miss** their build chatter polluted it → garbage
  `XDJ_FW_DIR` → launch preflight can't find the nand → no panel → **"panel didn't come up"**, but
  only the **first** time (cache hit = silent = clean). Fixed: prep sends child stdout → stderr, and
  the shell reads the **last non-empty line**.
- **Orphaned QEMU.** The shell may be killed without a clean exit; `xdj-host` watches `XDJ_PARENT_PID`
  (`libc::kill(pid,0)`) and the shell stops the host on `CloseRequested`/`Destroyed`. QEMU (and the
  in-process panel with the host) must die with the app.
- **Packaged shell must not auto-boot the dev `build/qemu` firmware** — gate the dev fallback behind
  `app_resources().is_none()`.
- **WebKit ≠ Chrome** for the panel layout (flex-fill + SVG collapses in WKWebView — see
  [xdj-virtual-panel]). Headless Chrome screenshots won't reproduce it; test in the real app.
- **macOS unix socket path ≤ 104 chars** → keep the runtime dir short.
- Packaging: `dylibbundler -p @executable_path/libs/`, strip duplicate `LC_RPATH`s, `-L` the bundled
  keymaps for the relocated qemu, ad-hoc sign inside-out (arm64 won't run a dylibbundler-broken sig).
- USB mount is **eject-first** and read-only by default (see [xdj-virtual-panel] `/usb`).

## References
`app/shell/src/main.rs`, `app/host/src/main.rs`, `app/shell/{tauri.conf.json,capabilities/default.json}`,
`packaging/build-macos-app.sh`, **`docs/standalone-app.md`** (full architecture + handover),
`docs/standalone-app-roadmap.md`, `docs/usb-mount-howto.md`.
