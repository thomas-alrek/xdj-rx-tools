---
name: build-xdj-app
description: Build, run, package and debug the standalone XDJ-RX desktop app - the Tauri shell + xdj-host orchestrator (with the in-process panel server) that lets a user drop a .UPD firmware and run the emulator with no /tmp rendezvous. Use when working on app/shell (Tauri/Rust), app/host (the orchestrator), firmware prep/caching, the key prompt, native file dialogs, USB mount from the app, packaging (dist/XDJ-RX.app), or any "the app won't boot / panel didn't come up" issue.
---

# Standalone XDJ-RX app (`app/`)

A single desktop app: drop a `.UPD` → it prepares the boot image and runs the emulator in a native
window. Architecture: **`docs/standalone-app.md`**. **Never patches `rbp`/kernel/rootfs**
— all host-side. Licensed GPL-2.0-or-later; the bundle ships every third-party license text. See also skills [xdj-virtual-panel], [run-xdj-emulator], [qemu-imx6-patches],
[upd-firmware-format], [build-xdj-sd-image].

## Pieces
- **`app/shell/`** — the Tauri app (Rust, OS webview). Two windows: **`setup`** (local
  `app/ui/index.html`: drop/click a firmware, key prompt) and **`main`** (external URL to the panel
  served by `xdj-host`). `src/main.rs` has the commands, window lifecycle, and firmware/key flow.
- **`app/host/`** — **`xdj-host`**, the orchestrator (Rust). Two modes:
  `xdj-host prep <upd>` (prepare+cache, prints the dir) and `xdj-host` (run: preflight → panel server +
  VNC↔WS bridge + QEMU, supervise, tear everything down together).
- **`app/host/src/panel.rs`** — the virtual-panel server, in-process (port of `tools/xdj-panel/server.py`, which stays for dev; keep them identical with `tools/xdj-panel/test_panel_parity.py`). See [xdj-virtual-panel].
- **`app/host/src/mcp.rs`** — the MCP server (Streamable HTTP JSON-RPC, stateless) on the panel port,
  `POST /mcp`: power, `load_firmware` (→ `PowerCmd::LoadFirmware`: prep, switch `Config.fw` to that
  dir, power on Provision/Normal), named controls (`press`/`set`, the table in `controls()`), USB,
  PNG `screenshot` (QMP screendump), LEDs, console. Tools call the panel's own `get`/`post`, so they
  behave exactly like panel clicks. Tool list: `docs/standalone-app.md` §7a. Add a control there
  when mapping a new one.
- **`app/ui/`** — `index.html` (setup page), vendored `novnc/`, `node_modules/audio-knobs`.
- **`packaging/build-macos-app.sh`** — assembles `dist/XDJ-RX.app`.

## Flow (drop a firmware → running emulator)
1. `on_firmware_dropped` (drag, or `browse_firmware` command) → `prep_firmware` → `start_host` →
   `show_emulator` (opens the `main` window at `http://127.0.0.1:8765/?vnc=ws://127.0.0.1:6080/`).
2. **prep** (`xdj-host prep`, ~1 s, in-process `xdj_fw::firmware`, crate `crates/xdj-fw` — the same
   code as the `xdj` CLI's `fw decrypt` / `prepare` / `fw stick`): decrypt → `fw.iso` → boot blobs
   (the firmware's kernel) → `update-stick.img` (FAT16, `XDJRX.UPD`). Cached in **app-data by
   firmware hash** (`~/Library/Application Support/XDJ-RX/<hash>/`) with `prep-vN.stamp` (bump it +
   the shell's `fw_complete` when prep output changes).
3. **run** (`xdj-host`): `preflight` → panel server (`panel.rs`) → VNC↔WS bridge → `tokio::select!`
   supervising qemu/panel/ctrl-c/parent-death/provisioning.
4. **The unit is NAND-only (no SD card)** — `BootKind` in `app/host/src/main.rs`, details in
   `docs/standalone-app.md` §4a. No `provisioned.stamp` → **Provision** at once (regardless of
   `XDJ_AUTOSTART`): `nand::init` (`crates/xdj-fw/src/nand.rs`, = `xdj nand init`) + update mode
   with `XDJ_UPDATE_TYPE=20` (ALL), stick on USB1 after `### USB update mount.` → the firmware's own
   updater installs everything. **"Installed" = `release=` in the NAND env** (`xdj nand info`; the
   console success line never prints — the updater's last `umount /mnt/usb` stays busy).
   `provision_watch` sees it, waits 10 s, cuts power, writes `provisioned.stamp` → boots **Normal**. Normal/Update power-ons run `nand::boot` first
   (kernel_bank → `nand-boot/` blobs). Always `XDJ_BOOT_MEDIA=nand
   XDJ_NAND_IMG=<dir>/nand-flash.img XDJ_PANELCOM_IMG=<dir>/panelcom-flash.bin`.
   The SD image (`qemu/build-nand.py`) is dev-only (watch.sh / relaunch.sh / root shell).

## Sockets, ports, env
- Per-instance runtime dir sockets: `qmp.sock` (QMP lifecycle + USB), `vnc.sock` (framebuffer),
  `ctrl.sock`/`key.sock` (the `xdjctrl`/`xdjkey` control chardevs, bound at realize), `power.sock`,
  `console.log` (serial). **No HMP monitor in the app** — USB mount uses QMP
  `human-monitor-command`.
- Ports (hardcoded ⇒ one instance): panel **8765**, VNC-WS bridge **6080**. `start_host` refuses if
  8765 is busy (clearer than a 60 s timeout).
- Env: `XDJ_RESOURCES` (packaged root) / `XDJ_REPO` (dev root); `XDJ_FW_DIR` (prepared dir),
  `XDJ_NAND`, `XDJ_QEMU`, `XDJ_PANEL_PORT`; `XDJ_PARENT_PID` (shell PID, for orphan cleanup);
  `XDJ_USB`/`XDJ_USB_SLOT`/`XDJ_USB_DELAY` (boot auto-insert, read/write); `XDJ_RUNTIME_DIR`,
  `XDJ_VNC_WS_PORT`, `XDJ_AUTOSTART`, `XDJ_FORCE_SETUP`, `XDJ_NET` (eth0 backend, chosen per
  power-on by `net_backend()`: `auto` = the optional socket_vmnet LAN bridge if its socket exists,
  else user NAT; `user`; or a socket path — skill run-xdj-emulator "Network"). The panel finds its bundled assets itself.
  Firmware key: `app-data/key/aes256.key`, content-sniffed
  (64-hex → `XDJRX_AES_KEY`, else `--key-file`); **never bundled**, falls back to repo `.env` in dev.

## Build & run
```
# dev (from repo; shell uses app/host/target/debug/xdj-host via XDJ_REPO)
( cd app/host && cargo build ) && ( cd app/shell && cargo build && cargo run )
# release binaries
( cd app/host && cargo build --release ); ( cd app/shell && cargo build --release )
# package (macOS): bundles patched qemu (built --disable-libssh) + dylibs, panel pages, novnc, knobs
# dist and every license text (packaging/collect-licenses.py -> Resources/licenses/); ad-hoc signs.
# Needs dylibbundler.
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
`POST /power on test` (holds MEMORY + TEMPO RANGE across a cold boot, releases them ~2 s after the
console's `ALSA: Resolve xrun`; ~1 boot in 8 still comes up normal — a known rbp-side flake).
`xdj-host` owns QEMU's lifecycle and listens on **`<runtime>/power.sock`** (line protocol:
`on` | `on update` | `off` | `status` → one line `on` / `on update` / `off`). `on update` sets
`XDJ_BOOT_UPDATE=1` for QEMU (`xdj-board.c`, firmware-update boot). `off` kills QEMU (a power cut —
the app's nand image is persistent writeback, like the real unit's flash). The panel's POWER-off
while rbp runs first does the **standby handshake** (press POWER `0x5d.0`, wait ≤ 10 s for rbp's TX
byte `0x11 = 'S'`, then `off`; `standby_cut` in `panel.rs`, `docs/service-modes.md`). Each power-on
deletes the old sockets + `console.log`. QEMU exiting on its own = powered off; only the panel server
dying, SIGTERM, Ctrl-C or the shell vanishing end `xdj-host`. Without `XDJ_AUTOSTART=0` (CLI use)
it powers on at start. The panel side (button, prefs, queued USB) is in [xdj-virtual-panel].
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

## Gotchas (don't regress)
- **prep stdout must be ONLY the dir path.** The shell reads `xdj-host prep` stdout as `XDJ_FW_DIR`
  (its **last non-empty line**); all progress goes to stderr. Any stdout chatter on a cache miss →
  garbage `XDJ_FW_DIR` → preflight fails → **"panel didn't come up"** only on first run.
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
- USB mount is **eject-first** and **read/write** (`prepare_usb_backing`: host unmount of a
  `/dev/diskN`; `attach_usb_drive` retries "Resource busy"; see [xdj-virtual-panel] `/usb`). `regress.py` loads a copy of its
  `test/usb` fixture so it stays pristine.

## Testing
`tools/test/regress.py` (dev build) / `tools/test/regress.py --app dist/XDJ-RX.app` (the bundle):
provision, boot, panel, USB, standby, test mode, custom-firmware install on a throwaway unit with
private ports — `docs/testing.md`. After any panel-server change also run
`tools/xdj-panel/test_panel_parity.py`.

## References
`app/shell/src/main.rs`, `app/host/src/{main.rs,panel.rs}`,
`app/shell/{tauri.conf.json,capabilities/default.json}`, `packaging/build-macos-app.sh`,
`packaging/collect-licenses.py`, **`docs/standalone-app.md`**, `docs/usb.md`, `docs/testing.md`.
