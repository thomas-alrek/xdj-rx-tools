---
name: build-xdj-app
description: Build, run, package and debug the standalone XDJ-RX desktop app - the Tauri shell + xdj-host orchestrator + panel sidecar that lets a user drop a .UPD firmware and run the emulator with no /tmp rendezvous. Use when working on app/shell (Tauri/Rust), app/host (the orchestrator), firmware prep/caching, the key prompt, native file dialogs, USB mount from the app, packaging (dist/XDJ-RX.app), or any "the app won't boot / panel didn't come up" issue.
---

# Standalone XDJ-RX app (`app/`)

A single desktop app: drop a `.UPD` → it prepares the boot image and runs the emulator in a native
window. Governed by `docs/standalone-app-roadmap.md`. **Never patches `rbp`/kernel/rootfs** — all
host-side. See also skills [xdj-virtual-panel], [run-xdj-emulator], [qemu-imx6-patches],
[upd-firmware-format], [build-xdj-sd-image].

## Pieces
- **`app/shell/`** — the Tauri app (Rust, OS webview). Two windows: **`setup`** (local
  `app/ui/index.html`: drop/click a firmware, key prompt) and **`main`** (external URL to the panel
  sidecar). `src/main.rs` has the commands, window lifecycle, and firmware/key flow.
- **`app/host/`** — **`xdj-host`**, the orchestrator (Rust). Two modes:
  `xdj-host prep <upd>` (prepare+cache, prints the dir) and `xdj-host` (run: preflight → spawn QEMU +
  sidecar + VNC↔WS bridge, supervise, tear everything down together).
- **`tools/xdj-panel/server.py`** — the panel sidecar (see [xdj-virtual-panel]).
- **`app/ui/`** — `index.html` (setup page), vendored `novnc/`, `node_modules/audio-knobs`.
- **`packaging/build-macos-app.sh`** — assembles `dist/XDJ-RX.app`.

## Flow (drop a firmware → running emulator)
1. `on_firmware_dropped` (drag, or `browse_firmware` command) → `prep_firmware` → `start_host` →
   `show_emulator` (opens the `main` window at `http://127.0.0.1:8765/?vnc=ws://127.0.0.1:6080/`).
2. **prep** (`xdj-host prep`): `xdjrx_upd.py` decrypt → `qemu_prepare.py` boot blobs
   (vmlinux/tramp/atags) → `qemu/build-nand.py` nand.img. Cached in **app-data by firmware hash**
   (`~/Library/Application Support/XDJ-RX/<hash>/`); re-dropping cache-hits.
3. **run** (`xdj-host`): `preflight` (qemu + nand + boot blobs) → `spawn_qemu` (`-M sabrelite`, USB
   hub-only boot, per-instance sockets) → `spawn_sidecar` (server.py) → VNC↔WS bridge →
   `maybe_insert_usb` → `tokio::select!` supervising qemu/sidecar/ctrl-c/parent-death.

## Sockets, ports, env
- Per-instance runtime dir sockets: `qmp.sock` (QMP lifecycle + USB), `vnc.sock` (framebuffer),
  `ctrl.sock`/`key.sock` (Phase-1 control chardevs). **No HMP monitor in the app** — USB mount uses
  QMP `human-monitor-command`.
- Ports (hardcoded ⇒ one instance): panel **8765**, VNC-WS bridge **6080**. `start_host` refuses if
  8765 is busy (clearer than a 60 s timeout).
- Env: `XDJ_RESOURCES` (packaged root) / `XDJ_REPO` (dev root); `XDJ_FW_DIR` (prepared dir),
  `XDJ_NAND`, `XDJ_QEMU`, `XDJ_PANEL_PORT`; `XDJ_PARENT_PID` (shell PID, for orphan cleanup);
  `XDJ_USB`/`XDJ_USB_SLOT`/`XDJ_USB_DELAY` (boot auto-insert); `XDJ_NOVNC_DIR`/`XDJ_KNOBS_DIR`
  (bundled asset dirs for the sidecar). Firmware key: `app-data/key/aes256.key`, content-sniffed
  (64-hex → `XDJRX_AES_KEY`, else `--key-file`); **never bundled**, falls back to repo `.env` in dev.

## Build & run
```
# dev (from repo; shell uses app/host/target/debug/xdj-host via XDJ_REPO)
( cd app/host && cargo build ) && ( cd app/shell && cargo build && cargo run )
# release binaries
( cd app/host && cargo build --release ); ( cd app/shell && cargo build --release )
# package (macOS): bundles patched qemu + dylibs, scripts, panel, novnc, knobs dist,
# e2fsprogs, python-build-standalone; ad-hoc signs. Needs dylibbundler + brew e2fsprogs.
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

## Gotchas (all bit us; all fixed — don't regress)
- **prep stdout must be ONLY the dir path.** The shell reads `xdj-host prep` stdout as `XDJ_FW_DIR`.
  The prep scripts inherit stdout, so on a **cache miss** their build chatter polluted it → garbage
  `XDJ_FW_DIR` → launch preflight can't find the nand → no sidecar → **"panel didn't come up"**, but
  only the **first** time (cache hit = silent = clean). Fixed: prep sends child stdout → stderr, and
  the shell reads the **last non-empty line**.
- **Orphaned QEMU.** The shell may be killed without a clean exit; `xdj-host` watches `XDJ_PARENT_PID`
  (`libc::kill(pid,0)`) and the shell stops the host on `CloseRequested`/`Destroyed`. Both QEMU and
  the sidecar must die with the app.
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
`packaging/build-macos-app.sh`, `docs/standalone-app-roadmap.md`, `docs/usb-mount-howto.md`.
