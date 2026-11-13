# Standalone XDJ-RX app — architecture & development handover

This document is a **developer handover**: enough to take over the standalone desktop app with no
prior context. It covers what the app is, how the pieces fit, the runtime data flow, how to build /
run / debug / package it, and the pitfalls that have already bitten us.

For the *plan and status*, see [standalone-app-roadmap.md](standalone-app-roadmap.md). For the
operational cheat-sheets, see the skills `build-xdj-app` and `xdj-virtual-panel` in
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
> Version-specific claims (e.g. "remote capabilities are stable on Tauri 2.12") are true as of the
> commit that added them — re-verify on a Tauri upgrade.

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
│        - tools/xdj-panel/server.py  (the panel sidecar, HTTP :8765) │  │
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
| `tools/xdj-panel/` | `server.py` (panel sidecar) + `panel.html` (the control UI). See skill `xdj-virtual-panel`. |
| `packaging/build-macos-app.sh` | Assembles `dist/XDJ-RX.app`. |
| `xdjrx_upd.py`, `qemu_prepare.py`, `qemu/build-nand.py` | The firmware-prep pipeline the host drives. |
| `qemu/` | The patched-QEMU build scripts + `patches/01..13`. |

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
5. `xdj-host` (run mode): `preflight` (qemu + nand + boot blobs present) → `spawn_qemu` →
   `spawn_sidecar` (server.py) → start the VNC→WS bridge → `maybe_insert_usb` → supervise.
   The sidecar binds :8765 within ~1 s, so `wait_port` returns and the shell opens the **main**
   window at `http://127.0.0.1:8765/?vnc=ws://127.0.0.1:6080/`.
6. The panel (`panel.html`) connects to the screen over noVNC (the `?vnc=` WebSocket) and drives
   controls via the server's HTTP endpoints.

If step 4 times out, the shell shows *"the emulator panel didn't come up — see host.log"*. That
almost always means `xdj-host` exited at preflight (so no sidecar bound the port) — check `host.log`.

---

## 4. Firmware prep & caching (`xdj-host prep`)

`app/host/src/main.rs::prep_firmware`. Cache key = first 16 hex of the `.UPD` SHA-256 →
`app-data/<hash>/`. If `vmlinux.bin + tramp.bin + atags.bin + nand.img` already exist there, it's a
**cache hit** (instant). Otherwise it runs three Python steps (each inherits the environment; the
bundled `python` + `e2fsprogs` are prepended when packaged):

1. `xdjrx_upd.py decrypt <upd> -o <dir>/fw.iso` — needs the AES key (§5).
2. `qemu_prepare.py <fw.iso>` with `XDJ_OUT=<dir>` — kernel + SABRE-SD boot blobs.
3. `qemu/build-nand.py` with `XDJ_OUT=<dir> XDJ_ISO=<fw.iso>` — the ext4 rootfs + GUI + env + MBR → `nand.img`.

**Contract that matters:** `xdj-host prep` prints **only the prepared dir path** to stdout (the
shell reads stdout as `XDJ_FW_DIR`). The Python steps' own stdout is redirected to **stderr** so it
can't pollute that path. The shell additionally reads the **last non-empty stdout line** defensively.
> This was the "fails the first time, works the second" bug: on a cache miss the scripts flooded
> stdout, the shell used the whole blob as `XDJ_FW_DIR`, and the launch's preflight then couldn't
> find the nand. Keep stdout clean.

---

## 5. Firmware decryption key

Pioneer `.UPD` files are AES-encrypted. The key is **user-supplied and never bundled**. Stored at
`app-data/key/aes256.key`, content-sniffed at use: a 64-hex string → passed as `XDJRX_AES_KEY`;
anything else → treated as a passphrase file (`--key-file`). The setup page's key box (paste 64-hex
→ `set_key_hex`, or drop/Browse a file → `store_key_file`) writes it. In dev, a repo `.env` is a
fallback (`xdjrx_upd.py` reads it). No key anywhere → a clear "add your key" error.

---

## 6. QEMU invocation (`qemu_args`)

Headless `-M sabrelite` (the XDJ-RX is a SABRE-SD board), `-m 256`, `-smp 2` (override `XDJSMP`),
`-display none`. Boot blobs are loaded with `-device loader,...` (vmlinux @0x10008000, atags
@0x10000100, tramp @0x10000000). `nand.img` is the SD drive. A **USB-2.0 hub** is added but the
storage is **not** cold-plugged — it's inserted after `rbp` starts polling (§8), avoiding a mount
race. Per-instance channels live in the runtime dir:

| flag | socket | consumer |
|---|---|---|
| `-chardev socket,id=xdjctrl` | `ctrl.sock` | sub-ucom control (server.py `--ctrl-sock`) |
| `-chardev socket,id=xdjkey` | `key.sock` | MAIN-UCOM GPIO keys (server.py `--key-sock`) |
| `-qmp unix:` | `qmp.sock` | lifecycle + USB `drive_add`/`device_add` (host and sidecar) |
| `-vnc unix:` | `vnc.sock` | framebuffer → the WS bridge → noVNC |
| `-serial file:` | `console.log` | kernel console |

Audio backend is chosen by `pick_audio` (coreaudio on macOS, etc.; `XDJAUDIO`/`none` override).
Packaged, `-L <qemu-bios>` points the relocated qemu at the bundled keymaps (else `-vnc` fails).

---

## 7. The panel UI (sidecar)

The **main** window loads `http://127.0.0.1:8765/` — served by `tools/xdj-panel/server.py`, an
ordinary Python `http.server` the host runs as a sidecar (`--ctrl-sock/--key-sock/--qmp`, plus
`XDJ_NOVNC_DIR`/`XDJ_KNOBS_DIR` for bundled assets). The UI (`panel.html`) is an original,
copyright-free design (the old device-photo background was removed). Full detail is in the
**`xdj-virtual-panel`** skill; the essentials:
- Screen = noVNC over the `?vnc=` WebSocket (the host's VNC-unix→WS bridge on :6080).
- Controls = HTTP POSTs (`/ctrl`, `/browse`, `/input`, `/mainkey`, `/usb`) → the control channels.
- Layout is absolute and photo-calibrated; widgets are the `audio-knobs` npm lib + custom jog/faders.

---

## 8. USB media

**At boot:** `maybe_insert_usb` (host) optionally inserts `XDJ_USB` on `XDJ_USB_SLOT` once `rbp` is
polling (hub ports 1.2=usb1, 1.1=usb2), writing the detect file (`/tmp/usb_insert` / `/tmp/usb2_insert`)
that QEMU patch 06/10 watches. **At runtime:** the panel's USB LOAD buttons POST `/usb <slot> <path>`
and the server does the same `drive_add`/`device_add` over QMP, **ejecting any previous insertion
first** (USB STOP only unmounts in the firmware; the QEMU device stays plugged). Read-only
(`snapshot=on`) by default. See `docs/usb-mount-howto.md`, `docs/usb-slot-topology.md`.

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

The host supervises with `tokio::select!` over: qemu exit, sidecar exit, Ctrl-C, and **parent
death** (it polls `XDJ_PARENT_PID` with `libc::kill(pid,0)`). Any of these tears the rest down. The
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
| `XDJ_FW_DIR` | Prepared firmware dir (boot blobs + `nand.img`). Set by the shell after prep. |
| `XDJ_NAND` | Override the nand path (default `<fw_dir>/nand.img`). |
| `XDJ_QEMU` | Override the qemu binary. |
| `XDJ_RUNTIME_DIR` | Sockets/logs dir (default: app-data `run/` packaged, `build/run` dev). Keep it short — macOS unix socket paths ≤ 104 chars. |
| `XDJ_PARENT_PID` | Shell PID; the host exits if it dies (orphan cleanup). |
| `XDJ_PANEL_PORT` / `XDJ_VNC_WS_PORT` | Default 8765 / 6080. |
| `XDJ_PYTHON` | Python for prep (default: bundled, else `python3`). |
| `XDJ_USB` / `XDJ_USB_SLOT` / `XDJ_USB_DELAY` | Boot-time USB auto-insert. |
| `XDJ_NOVNC_DIR` / `XDJ_KNOBS_DIR` | Bundled asset dirs handed to the sidecar. |
| `XDJAUDIO` / `XDJSMP` | Audio backend / vCPU count. |
| `XDJ_CAL_REF` | Dev-only: reference photo for the panel's `?cal` calibration overlay (served at `/cal-ref`; 404 if unset). |
| `XDJ_FORCE_SETUP` | Shell: always show the drop screen (testing). |

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
`<runtime>/qemu.log`, `<runtime>/sidecar.log`, `<runtime>/console.log`. **When something "doesn't
come up", read these first.** Iterate on the panel layout with headless Chrome screenshots (see the
`xdj-virtual-panel` skill) — but always re-verify in the real app, because **WebKit (WKWebView) and
Chrome render the layout differently** (flex-fill + SVG collapses in WebKit).

---

## 13. Packaging (macOS `.app`)

`packaging/build-macos-app.sh` → `dist/XDJ-RX.app`. It builds both release binaries and bundles:
the patched `qemu-system-arm` with its Homebrew dylibs **relocated** (`dylibbundler -p
@executable_path/libs/`, duplicate `LC_RPATH`s stripped), the keymaps, the prep scripts, the panel +
noVNC + knobs dist, `e2fsprogs` (relocated), and a relocatable `python-build-standalone` with the
prep deps. Everything is **ad-hoc code-signed inside-out** (`codesign --force -s -`) because Apple
Silicon won't run a binary whose signature dylibbundler invalidated. Prereqs: Rust, a built patched
qemu (`qemu/build-qemu.sh`), Homebrew `e2fsprogs`, `dylibbundler`, network (pip + python download).

**Fast iteration on an already-built bundle** (no full repackage): copy the changed artifact into
`Contents/MacOS/XDJ-RX` (shell), `Contents/Resources/xdj-host` (host), or
`Contents/Resources/panel/*` (panel/server), then re-sign the binary and `--deep` the app.
`index.html` and the capability are **embedded in the shell binary** at build time — rebuild the
shell to change them.

This is **ad-hoc**, not Developer-ID signed or notarized: it runs locally but Gatekeeper will warn
other users. Notarization is deferred (§14).

---

## 14. Known pitfalls (all were real bugs — don't regress)

- **prep stdout must be only the dir path** (§4). Anything else → bad `XDJ_FW_DIR` → "panel didn't come up", first run only.
- **`blocking_pick_file` freezes the app** on macOS — use the non-blocking callback form (§9).
- **App commands are ACL-blocked for remote-origin windows** — use the dialog plugin's JS API + a `remote` capability for the panel (§9).
- **WebKit ≠ Chrome** for the panel layout — grid `fr` tracks, not flex-fill + SVG, in WebKit (§12).
- **Orphaned QEMU** if teardown isn't wired — parent-death watch + window-close + SIGTERM (§10).
- **Packaged shell must not auto-boot the dev `build/qemu`** — `resolve_startup_fw` gates that behind `app_resources().is_none()`.
- **macOS unix socket path ≤ 104 chars** — keep `XDJ_RUNTIME_DIR` short.
- **USB re-load must eject first** — USB STOP is only a firmware unmount (§8).

---

## 15. Deferred / next steps

- **Notarization + Developer-ID signing** so non-dev users can run it without Gatekeeper warnings.
- **Windows & Linux packaging** (the orchestrator is mostly portable; qemu + python bundling and the
  dialog/socket bits need per-OS work).
- **USB enumeration polish** — the cold-plug mount race is worked around (hub-only boot + delayed
  insert), but see `docs/usb-accept-handover.md` for the deeper story.
- **Python → Rust port of the prep pipeline** (roadmap's long pole) to drop the bundled interpreter.

## 16. See also
Skills: `build-xdj-app`, `xdj-virtual-panel`, `run-xdj-emulator`, `qemu-imx6-patches`,
`upd-firmware-format`, `build-xdj-sd-image`. Docs: [standalone-app-roadmap.md](standalone-app-roadmap.md),
[usb-mount-howto.md](usb-mount-howto.md), [usb-slot-topology.md](usb-slot-topology.md),
[emulator-status.md](emulator-status.md).
