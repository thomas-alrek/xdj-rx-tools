# Standalone app roadmap — packaging the XDJ-RX emulator

**Status: IN PROGRESS — Phases 1 (control socket) + 2 (Tauri shell + orchestrator) done 2026-11-12, Phase 3 (firmware drag-drop + app-data) done 2026-11-13; Phase 3b (USB media) plumbing done with a deferred enumeration issue, Phase 4 (audio backend pick) done, Phase 5 macOS .app done (ad-hoc signed; notarization + key-prompt pending); Phase 5 Windows/Linux + the Python→Rust port remain.**
The goal: turn today's "custom QEMU + web UI + `/tmp` files" setup into a single
self-contained, cross-platform desktop app. The user drops a `.UPD` firmware onto
the app; the app prepares the boot image and runs the emulator, with a native
(Tauri) shell and no global `/tmp` rendezvous.

Legend: ✅ done · 🔶 partial · ⬜ not started · 🧱 long pole

Chosen direction (decided 2026-11-12):
- **UI:** Tauri (Rust + OS webview) — reuse [tools/xdj-panel/panel.html](../tools/xdj-panel/panel.html), don't rewrite the panel.
- **IPC:** full clean — QEMU **VNC** for the framebuffer + a per-instance **control socket**; retire the `/tmp` files.

Hard constraint carried from [CLAUDE.md](../CLAUDE.md): **never modify the guest
image to suit the emulator.** Nothing in this plan touches `rbp`, the kernel, or
the rootfs. Everything is host-side (QEMU patches + the app shell).

---

## 1. Where we are today

Three channels, all on well-known `/tmp` paths, bridged by a Python server
([tools/xdj-panel/server.py](../tools/xdj-panel/server.py)):

| Concern | Today | Mechanism |
|---|---|---|
| Control → QEMU | `/tmp/subucom_ctrl`, `/tmp/mainkey` | Patches 07/09/13 `fopen`/`access` the file **every frame**, parse a text spec (`B 12 08`). server.py writes it atomically. |
| Framebuffer → UI | `/tmp/d_mon4.sock` + `/tmp/xdjpanel_fb.ppm` | server.py drives QEMU monitor `screendump` to a PPM, reads it back, serves raw RGB over HTTP `/raw`; browser polls onto a `<canvas>` (~20–40 fps). |
| Lifecycle | `/tmp/d_mon4.sock` | QEMU monitor (HMP) unix socket — the one already-clean channel. |

Already in good shape:
- **UI is already a web app** — panel.html + a stdlib `http.server`. Reusable as-is.
- **Audio is already OS-switched** — [qemu/watch.sh](../qemu/watch.sh) picks
  `coreaudio` (Darwin) / `pa` (else), `XDJAUDIO` override, `timer-period=1451`.
- **Firmware prep is a clean pipeline** — [run.sh](../run.sh) chains
  `fetch_key.py` → `xdjrx_upd.py` → `qemu_prepare.py` → `build-nand`. The only
  snags: the input is a **hardcoded download URL**, and outputs land in `build/`.

Weak link: the framebuffer path, and it's **already the main performance pain
today** — the UI is visibly sluggish. `screendump`→PPM→re-read is ~18 ms/grab,
full frame every time, no dirty-rects, **pull not push**, then the browser decodes
raw RGB onto a `<canvas>` each poll. See
[server.py:123 `grab_raw`](../tools/xdj-panel/server.py#L123). This is why the
screen-rendering strategy below (Phase 2) is a planned ladder, not an afterthought.

---

## 2. Target architecture

```
┌──────────────────────── Tauri app (one process tree) ────────────────────────┐
│                                                                               │
│  Webview (reuses panel.html)              Rust host / orchestrator            │
│  ┌───────────────────────────┐           ┌─────────────────────────────────┐ │
│  │ panel overlay + hotspots  │  Tauri    │ • spawn + supervise qemu child   │ │
│  │ screen pane = noVNC ◄──────┼──IPC──────┤ • QMP client (lifecycle)         │ │
│  │ drag-drop .UPD target      │           │ • WS↔TCP bridge for noVNC        │ │
│  └───────────────────────────┘           │ • control-socket writer          │ │
│                                           │ • firmware prep (sidecar)        │ │
│                                           │ • audio backend selection        │ │
│                                           └──────────────┬──────────────────┘ │
│                                                          │ 3 per-instance      │
│                                                          │ sockets (runtime    │
│                                                          │ dir, not /tmp)      │
│                                     ┌────────────────────▼──────────────────┐ │
│                                     │  patched qemu-system-arm               │ │
│                                     │   -qmp  (lifecycle)                    │ │
│                                     │   -vnc  (host-side scanout surface)    │ │
│                                     │   -chardev socket (control in)         │ │
│                                     └────────────────────────────────────────┘ │
└───────────────────────────────────────────────────────────────────────────────┘
```

**Why VNC is accuracy-safe (the key point):** `-vnc` is a **QEMU** feature, not a
guest feature. It exports the *same host-side console surface* that patch 02's
scanout already registers and that the cocoa window already draws — exactly like
the current `screendump`, which also never touches the guest. The guest Linux
writes to the i.MX6 framebuffer as on real hardware; cocoa and VNC are just two
renderers of that one surface. **Zero guest-image changes.** It's strictly a
better pipe for bytes we already extract, and it brings dirty-rects + push.

---

## 3. Phased plan

### Phase 0 — VNC parity ✅ (accepted on trust, 2026-11-12)
Would have been: add `-vnc :0` next to cocoa on the current QEMU and confirm the
live UI renders over RFB. Skipped by decision — it's the same console surface, so
treated as given. (If anything downstream surprises us, this is the first thing to
sanity-check.)

### Phase 1 — Control socket (QEMU patch rework) ✅ (2026-11-12)
Replaced the per-frame `/tmp` file poll with **two QEMU chardevs** the harness reads
(one per legacy file), keeping the exact text spec and parsing.
- **Two channels**: `id=xdjctrl` = the sub-ucom snapshot (patches 07/13);
  `id=xdjkey` = the main-ucom key GPIO int (patch 09). QEMU is the socket **server**
  (`-chardev socket,...,server=on,wait=off`); the harness connects as a client.
- **Framing = NUL-terminated snapshots**: each write is the ENTIRE desired state, so
  one record == one atomic ctrl-file write (preserves "the whole snapshot replaces the
  held set"). Empty record = release. Last snapshot is kept on client disconnect.
- **Parsing byte-identical**: the old inline `fopen`/`fgets`/`sscanf` loop was extracted
  into a shared `xdj_parse_ctrl()`; only the byte *source* changed. One-shots fire once
  per fresh record. chardev handlers + the SPI-flush hook both run under the BQL → no locks.
- **Fallback**: when a channel's chardev is absent the harness reads its env path
  (`XDJ_CTRL_PATH` / `XDJ_KEY_PATH`, default the legacy `/tmp` names) — also makes the file
  path per-instance. So existing file-based tooling (inj.py, sweep_*.py) is unchanged.
- **Opt-in**: both launchers add the chardevs only when `XDJ_RUNTIME_DIR` is set
  (sockets land in that per-instance dir); unset = today's `/tmp`-file behavior.
- **Client**: `server.py --ctrl-sock/--key-sock` pushes snapshots to the sockets (file mode
  stays the default); it remains the reference for the Phase 2 Rust port.
- **Verified live** (2026-11-12): over `xdjctrl`, a TEMPO-RANGE one-shot cycled DECK1 ±10→±16
  (direct) and DECK2 via server.py HTTP; over `xdjkey`, a mainkey press toggled the deck
  time display. File fallback still works with no chardevs. Full patch series reproduces the
  live tree byte-for-byte + builds from scratch.
- Touched: [qemu/patches/07-imx6-multifield-holds.patch](../qemu/patches/07-imx6-multifield-holds.patch),
  [09](../qemu/patches/09-imx6-mainucom-keys.patch),
  [13](../qemu/patches/13-imx6-subucom-autolevel.patch),
  [tools/xdj-panel/server.py](../tools/xdj-panel/server.py),
  [qemu/watch.sh](../qemu/watch.sh), [relaunch.sh](../tools/xdj-panel/debug/relaunch.sh).
  See skill `qemu-imx6-patches`.

### Phase 2 — Tauri shell + orchestrator 🔶 (orchestrator core done 2026-11-12)
**Orchestrator core — ✅ done & verified (headless), in [app/host](../app/host) (`xdj-host`):**
- Rust host spawns & supervises the qemu child and owns the **per-instance sockets**
  (QMP / VNC / control chardevs) in a runtime dir — no global `/tmp`. Supervises:
  qemu/sidecar exit or Ctrl-C tears the rest down and cleans the sockets.
- Screen pane = **noVNC**. QEMU `-vnc unix:` is raw RFB; the Rust host runs a tokio
  **WS↔unix bridge** so noVNC (RFB-over-WebSocket) can show it.
  [panel.html](../tools/xdj-panel/panel.html) uses noVNC when opened with
  `?vnc=ws://…`, else the old `/raw` poll; the same SCREEN calibration positions
  either; overlay/hotspots/calibration unchanged. noVNC vendored at
  [app/ui/novnc](../app/ui/novnc); served at `/novnc/*`.
- Control composition: **server.py runs as a sidecar** (the "sidecar first, port to
  Rust later" option — port is Phase 5.1).
- Verified live: `xdj-host` boots QEMU to the live UI; a frame captured over the
  WS+RFB bridge is the full 800×480 rekordbox UI; a control injected via the sidecar
  (DECK1 tempo range) shows up in the next VNC capture; Ctrl-C shuts the tree down.
- `/raw` is retired for the app path (kept as a fallback for the legacy server.py-only
  setup).

**Tauri native shell — ✅ in [app/shell](../app/shell) (`xdj-rx-shell`):** a Tauri 2 window
that spawns `xdj-host`, waits for the panel port, then opens on the live UI (panel skin +
noVNC screen). On exit it sends `xdj-host` SIGTERM so the whole tree (QEMU + sidecar +
sockets) tears down cleanly (a hard kill would orphan them). Verified: builds + launches,
spawns and supervises the full stack, the URL it loads serves the live 800×480 UI
(captured via its own bridge), and the **native window launches on the user's display
(confirmed 2026-11-12)**.

**Phase 2 remaining / follow-ups:** climb the screen-rendering ladder only if noVNC feel
isn't tight enough (rung 2/3, §Screen rendering); a small splash while QEMU boots; multi-
instance (pick free ports/runtime dir per instance — the plumbing is already per-instance).

**Screen rendering — a performance ladder (not noVNC-or-nothing).**
The webview carries two very different things: the **panel skin** (photoreal
image + hotspots + calibration — latency-insensitive, HTML's home turf, keep it in
the webview) and the **screen pane** (the live framebuffer — latency-critical).
Because the Tauri core is already native (Rust), the screen pane can be made as
native as performance demands, while the skin stays HTML. This is the **hybrid**
that makes Tauri competitive with a fully-native toolkit without rewriting the
panel. Climb this ladder only as far as perf requires:

1. **noVNC in the webview** (simplest). QEMU `-vnc` push + dirty-rects already
   removes today's full-frame poll — a big win on its own. Decode RFB in JS,
   draw to `<canvas>`. Start here; measure.
2. **Decode RFB in Rust, push frames to the webview.** If JS decode is the
   bottleneck, move RFB decode into the Rust host and hand the webview ready
   pixels (shared buffer / Tauri IPC). Removes the JS decode + the WS bridge.
3. **Native GPU-backed screen surface.** If the webview blit itself is the limit,
   render the screen pane natively — decoded RFB (or a shared-memory scanout from
   QEMU) straight to a GPU texture (`wgpu`/native layer), composited under/over the
   HTML panel chrome. Lowest latency, closest to a fully-native UI, skin stays HTML.

Rationale for the ladder: today's sluggishness is dominated by the pull-poll
(§1), so rung 1 alone should feel dramatically better; rungs 2–3 are there if the
DJ-UI feel (scrolling waveforms, jog response) still isn't tight enough. Don't
pre-build rung 3 — measure at each rung first.

### Phase 3 — Firmware drag-and-drop + app-data ✅ (2026-11-13)
- **Prep engine** (`xdj-host prep <firmware.UPD|.zip>`): SHA-256 the firmware, cache
  dir = `<app-data>/<hash>`, and if incomplete run the pipeline into it via the repo's
  Python (venv/uv/system): `xdjrx_upd.py decrypt` → `fw.iso`, `qemu_prepare.py` → boot
  blobs, `build-nand.py` → `nand.img`. A complete cache dir is reused instantly.
  - macOS: `~/Library/Application Support/XDJ-RX/<hash>/`
  - Linux: `$XDG_DATA_HOME/xdj-rx/<hash>/` (fallback `~/.local/share`)
- **Scripts repointed** off the hardcoded `build/` layout: [qemu_prepare.py](../qemu_prepare.py)
  honors `XDJ_OUT`; [build-nand.py](../qemu/build-nand.py) honors `XDJ_OUT`+`XDJ_ISO`;
  [xdjrx_upd.py](../xdjrx_upd.py) already took `-o` (and has a built-in key, so no
  fetch needed). All default to `build/` so run.sh/watch.sh are unchanged.
- **Drag-drop** in the shell: drop a `.UPD`/`.zip` on the window → prep (with a progress
  overlay) → boot. Startup boots the last-used firmware (recorded in app-data) or the dev
  `build/qemu` layout, else shows the drop target. `xdj-host` run takes `XDJ_FW_DIR`.
- **Distribution rule (enforced):** nothing firmware-derived is committed or bundled —
  boot artifacts are generated at runtime from the user's own `.UPD` into app-data.
- **Before release (see §5bis):** (A) the AES key must be **prompted from the user**, not
  the dev `.env`/auto-fetch — prep today only works because a dev `.env` exists; (B) the
  Python + e2fsprogs prep pipeline must be **bundled per-platform** (the engine's current
  venv/uv/system Python lookup is a dev assumption).
- Verified: `prep` built fw.iso+blobs+nand into app-data and the second run was an instant
  cache hit; `XDJ_FW_DIR=<that dir> xdj-host` booted it to the live UI; the shell auto-boots
  a prepared firmware and (with `XDJ_FORCE_SETUP=1`) shows the drop target with no emulator.
  The actual drop→prep→boot transition needs a real file-drop on the window (user-confirmed,
  like the window launch); its pieces (prep, start_host, drop screen) are each verified.

### Phase 3b — USB media (disk image + physical device) 🔶 (orchestrator plumbing done 2026-11-13)
Mount a USB into the guest — a **disk image** (`.img`) or a **real host stick** — the same
way, after boot. **Orchestrator plumbing done** in [app/host](../app/host) (`maybe_insert_usb`),
**no new QEMU patch**:
- Boot **hub-only** (USB-2.0 hub on `usb-bus.1` port 1), then after rbp is up (console
  marker `Resolve xrun` + `XDJ_USB_DELAY`s settle) run the proven insert choreography over
  QMP: `drive_add` + `device_add usb-storage,bus=usb-bus.1,port=1.1` (USB2) / `1.2` (USB1),
  then assert the slot's detect GPIO (`/tmp/usb2_insert` / `/tmp/usb_insert`, patch 06).
- Driven by env: `XDJ_USB=<image.img|/dev/diskN>`, `XDJ_USB_SLOT=usb1|usb2` (default usb2),
  `XDJ_USB_WRITE=1` (default: `snapshot=on`, writes discarded — protects a real stick).
  A physical device is `diskutil unmountDisk`'d (mac) / `umount`'d (linux) first.
- **Verified**: QEMU attaches the MSD at the correct topology (`info usb` → Port 1.1) with a
  snapshot overlay over the backing (`info block`), and rbp sees the slot.
- **KNOWN ISSUE (deferred, 2026-11-13):** the device doesn't auto-enumerate/mount — rbp shows
  the **"USB Error. Remove the device."** modal (the mount-rendezvous race from
  usb-slot-topology.md; the detect-GPIO → HOST1-resume path isn't completing through the
  orchestrator the way the manual `relaunch.sh` recipe does). **Temporarily clearable by
  pressing USB2.** To tackle later: compare against the manual recipe (boot-time drive
  backend?), the readiness window, and whether pressing USB2 (mainkey 158) is the missing
  trigger. The per-instance wart also remains: the detect hooks are still global `/tmp` files
  (patch 06), not socketized like Phase 1.

Remaining 3b work: the UI picker (enumerate host USB block devices, list beside the image
picker; `diskutil list` / `lsblk`), `usb-host` passthrough mode (B, below), and resolving the
enumeration race above.

Media mechanisms, different fidelity:

Two mechanisms, different fidelity:
- **A. Raw host block device as `usb-storage` (default).** Point `-drive` at the
  device node instead of an image file:
  ```
  -drive if=none,id=usbstick,file=/dev/rdisk4,format=raw,snapshot=on \
  -device usb-storage,...,drive=usbstick
  ```
  QEMU synthesizes its own usb-storage around the stick's **live blocks** — the
  guest sees the real filesystem + rekordbox library (QEMU's descriptors, not the
  stick's). Reuses the entire working USB path (incl. patch-10 post-boot insert).
  The XDJ-RX cares about filesystem content, not USB descriptors, so this gives
  full feature fidelity and is robust cross-platform.
- **B. True passthrough via `usb-host` (optional, max fidelity).** `-device
  usb-host,vendorid=0x..,productid=0x..` hands the actual device to the guest with
  its real descriptors/quirks. Closest to the hardware, but macOS claims
  mass-storage devices (`IOUSBMassStorage`), so grabbing them is fragile — keep B
  behind an advanced/Linux toggle.

**Recommendation: A as default, B as an optional advanced mode.**

Safety rules the app MUST enforce:
- **Unmount on the host first** (`diskutil unmountDisk` mac / `umount` linux).
  Host-mounted + guest-writing = two writers = filesystem corruption.
- **Protect the real library by default** — `snapshot=on` or a read-only attach so
  a browse session can't mutate the user's stick; writes are an explicit opt-in.
- **Permissions**: opening the device node needs admin (mac `/dev/rdiskN`) or
  udev/group access (linux); surface this clearly.
- **Cold-plug / insert** flow is identical to images — reuse hub-only-boot +
  post-boot `device_add` + usbnc-wakeup (patch 10).

Orchestration (Rust host): enumerate connected USB block devices, list them in the
UI beside the image picker, unmount, attach via A (B behind a toggle). No QEMU
patch. See [usb-mount-howto.md](usb-mount-howto.md),
[usb-slot-topology.md](usb-slot-topology.md).

### Phase 4 — Audio backend selection ✅ (backend pick done 2026-11-13)
- The Rust host (`pick_audio`) chooses `-audio` by OS, keeping `timer-period=1451`:
  macOS `coreaudio`; Windows `wasapi`; Linux prefers whatever is actually running —
  `pipewire` (if `$XDG_RUNTIME_DIR/pipewire-0`), else `pa` (if `…/pulse/native`), else
  `alsa`. `XDJAUDIO` overrides the whole value (incl. `none` to disable host audio).
- Verified: macOS still emits `-audio coreaudio,timer-period=1451` (unchanged, and audio
  to the host was already confirmed in Phase 2). The Linux/Windows branches are selection
  logic only (not runnable to verify on the dev Mac).
- Remaining: a **device picker** in the UI (which output device), and — if a chosen backend
  ever fails at QEMU startup — an actual relaunch-based fallback (today it's a best-effort
  pre-launch pick, not a runtime retry).

### Phase 5 — Packaging & cross-platform build 🧱 (the long pole) 🔶 (macOS .app done 2026-11-13)

**macOS .app — ✅ done & verified** via [packaging/build-macos-app.sh](../packaging/build-macos-app.sh)
(→ `dist/XDJ-RX.app`, ~145 MB, self-contained):
- Bundles the Tauri shell + `xdj-host` + patched `qemu-system-arm` (Homebrew dylibs
  relocated with `dylibbundler`, prefix `@executable_path/libs/` so inter-lib deps
  resolve; duplicate `LC_RPATH`s stripped) + the prep scripts + the panel sidecar +
  vendored noVNC + e2fsprogs (dylibs relocated) + a standalone CPython with the prep
  deps (`pycdlib`/`pycryptodome`/`inflate64`). Firmware/key/nand are **never** bundled.
- Code is **relocatable**: `xdj-host`/shell resolve resources via `XDJ_RESOURCES` or
  next-to-exe (Contents/Resources), falling back to the dev repo; runtime dir + prepared
  firmware live in app-data, not the (read-only) bundle.
- **QEMU built `--disable-sdl`** — on macOS the SDL dep is sdl2-compat, which dlopen's
  libSDL3 (a dependency bundlers can't follow → "Failed loading SDL3 library"); we use
  cocoa/vnc/none, so SDL is dropped. QEMU gets `-L <bundled keymaps>` (it's relocated
  from its build tree, so `-vnc` would otherwise fail to find `keymaps/en-us`).
- Verified: `dist/XDJ-RX.app` boots a prepared firmware to the **full live UI**, entirely
  from bundled resources (captured via the bundled VNC bridge).
- **Not done (as agreed):** Developer-ID signing + **notarization** (binaries are only
  **ad-hoc** signed — required for arm64 to run at all, but a notarized build is what a
  user double-clicks without Gatekeeper warnings). And **drag-drop prep needs the AES key**
  (§5bis-A): a freshly-dropped `.UPD` fails at decrypt in the .app because no key is shipped
  (correct!) — the key-prompt UX is the blocker there.

**Target platforms (minimum):** macOS, Windows, Ubuntu/Debian (`.deb`).
**Goal:** truly portable, **no user install step** (no "install Python/Homebrew/
QEMU first"). Everything the app needs is bundled.

#### 5.0 Sequencing principle: "no install" ≠ "pure Rust"
These are **separable** and must not be coupled:
- **No install step** is achievable *immediately* by **bundling** (QEMU + helper
  tools + — interim — a standalone Python) inside the app artifact. A ~100 MB app
  is normal and fine.
- **Pure Rust** (retiring the Python sidecar) is an *optimization* done
  incrementally afterwards. Don't block portability on it.

So: ship a bundled-sidecar app first, then port Python→Rust piece by piece.

#### 5.1 Python → Rust port (incremental; do after no-install works)
The firmware pipeline is already ~90% pure-logic and ports cleanly:

| Piece | Today | Rust |
|---|---|---|
| AES decrypt / cryptoloop | pycryptodome ([xdjrx_upd.py](../xdjrx_upd.py)) | `aes`/`cbc` |
| gzip/zlib, kernel decompress | stdlib ([qemu_prepare.py](../qemu_prepare.py)) | `flate2` |
| tar payloads | `tarfile` | `tar` |
| cramfs → tree | [cramfs_extract.py](../qemu/cramfs_extract.py) (~80 lines) | port it |
| ISO9660 extract | pycdlib | crate or hand-rolled read |
| boot blobs / MBR / u-boot env crc32 / dd | already pure Python | byte work + `crc32fast` |
| AES-key fetch | [fetch_key.py](../fetch_key.py) | `ureq`/`reqwest` |
| **ext4 fs create + /dev nodes + uid/gid** | **`mke2fs -d` + `debugfs`** ([build-nand.py](../qemu/build-nand.py)) | **see 5.2** |

#### 5.2 The one hard dependency: ext4 creation
`build-nand.py` is already pure-Python except for **e2fsprogs** (`mke2fs` makes
the ext4 rootfs; `debugfs` injects `/dev` nodes + sets uid/gid 0 unprivileged).
There is **no mature write-capable ext4 crate** in Rust — a correct ext4 writer
(inodes, block groups, extents, special nodes) is a genuine multi-week task.
Required at runtime (the SD rootfs is built from the user's `.UPD`, and the
firmware `/init` mounts ext4 — can't change without touching the guest).

Two options:
- **Default: bundle `mke2fs`+`debugfs`.** Tiny, mature; shelling out to a
  *bundled* copy still means zero user install. Pragmatic, unblocks mac + Linux
  now. **Caveat: e2fsprogs on Windows is awkward** (needs an MSYS2/Cygwin build) —
  this is the weak point of the bundle plan on the Windows target.
- **Eventually: port ext4 writing to Rust.** The one item worth the multi-week
  cost *specifically because it unblocks a clean Windows build* (no finicky
  e2fsprogs-on-Windows). Prioritize this only when Windows packaging demands it.

#### 5.3 QEMU: bundle, don't embed
- **Can't** static-link QEMU into the Rust binary (no `libqemu`; monolithic with
  its own `main`/globals/event loop) or `include_bytes!`+extract (fights
  Gatekeeper/AV). A literal single-file binary that *is* QEMU + UI is not practical.
- **Do** bundle `qemu-system-arm` beside the app binary. Minimize the bundle by
  **static-linking QEMU's deps** (glib, pixman, gnutls, …) where the platform
  allows — turns "relocate ~20 dylibs" into "ship one clean binary."
- Built from [qemu/build-qemu.sh](../qemu/build-qemu.sh) + `qemu/patches/`; the
  packaging step consumes its output per platform.

#### 5.4 Per-platform build & distribution matrix

| Platform | QEMU build | Bundle format | Install step? | Notes |
|---|---|---|---|---|
| **macOS** | native (clang); static-link Homebrew deps, keep system frameworks dynamic | **`.app`** (drag to Applications) | **none** (drag-and-run) | `dylibbundler` for any remaining dylibs; **codesign + notarize** or Gatekeeper blocks it. Universal (arm64+x86_64) via `lipo` or two builds. |
| **Windows** | **MSYS2/MinGW** build of QEMU | folder / **`.zip`** (unzip-and-run); optional MSI later | **none** (zip) | QEMU = exe + DLLs. **e2fsprogs is the sticking point (5.2)** — either an MSYS2 e2fsprogs build or the Rust ext4 writer. WebView2 runtime must be present (ship the evergreen bootstrapper). |
| **Ubuntu/Debian** | native (gcc); or `--static` for a fully static `qemu-system-arm` | **`.deb`** (+ **AppImage** for true no-install) | `.deb` = `dpkg -i` (an install); AppImage = none | **Vendor everything** under `/opt/xdj-rx/` (bundled qemu + e2fsprogs) so the `.deb` is self-contained and doesn't force the user to `apt install qemu`. AppImage is the "single portable file" option alongside the `.deb`. |

Note on `.deb`: a `.deb` *is* an install (dpkg), unlike `.app`/AppImage. To honor
"no forced dependency install," **vendor** the bundled qemu + e2fsprogs inside the
package (under `/opt`), rather than declaring `Depends: qemu-system-arm, e2fsprogs`
on the distro versions (which would reintroduce an install step *and* use an
unpatched QEMU). Ship **both** a `.deb` (idiomatic) and an AppImage (zero-install).

#### 5.5 CI / build infrastructure
- QEMU + e2fsprogs are per-OS native builds → use a **GitHub Actions matrix with
  native runners** (macos-latest, windows-latest, ubuntu-latest). Cross-compiling
  QEMU is painful; native-per-runner is the sane path.
- Tauri/Rust cross-compiles well and its bundler already emits `.app`/`.dmg`,
  `.deb`/AppImage, and Windows installers — feed it the per-platform QEMU artifact.
- Artifacts: `.app`(+`.dmg`) · `.zip`(+ optional MSI) · `.deb` + AppImage.
- Never ship firmware / AES key / `nand.img` in any artifact (§3 Phase 3).

#### 5.6 End-state layout (illustrative, macOS)
```
XDJ-RX.app
├── Contents/MacOS/xdj-rx        native Tauri host + UI
├── Contents/Resources/qemu-system-arm   bundled, deps static-linked
├── Contents/Resources/{mke2fs,debugfs}  bundled e2fsprogs (until Rust ext4)
└── Contents/Resources/assets/   panel.html + skin
```
No Python, no Homebrew, no user install. Firmware prep is pure Rust **except** the
two e2fsprogs shell-outs (5.2), which bundling keeps install-free.

---

## 4. Effort shape
- Phases 1–4 are **days each** — mostly relocating logic that already exists.
- Phase 5 is the **weeks** item. Don't let it block 1–4; the app can run against a
  system/Homebrew QEMU during development and only bundle at release time.

## 5. Open questions (decide when reached)
- Control logic: port server.py → Rust now, or keep as sidecar through Phase 3?
- Firmware prep: keep Python sidecar long-term (bundle via PyInstaller/embedded),
  or port the pipeline to Rust for a single binary?
- Screen rendering: how far up the Phase 2 ladder (noVNC → Rust-decode → native
  GPU surface) perf actually forces us. Decide by measuring at each rung, not up
  front. Today's UI is already sluggish, so rung 1 is the baseline to beat.

## 5bis. Deferred items raised during Phases 1–3 (must tackle before release)

### A. The AES decryption key must be user-supplied, never bundled (raised 2026-11-13)
The `.UPD` payload is a cryptoloop AES-256 image; decrypting it needs Pioneer's key.
**Good news: the key is NOT baked into our source.** [xdjrx_upd.py](../xdjrx_upd.py)
`_load_key` takes it, in order, from `--key-file aes256.key` (a passphrase file), the
`XDJRX_AES_KEY` env var, or a `.env` (git-ignored) — and **errors out if none is
present** (the `--help` line "else built-in key" is stale wording; there is no built-in
fallback). [fetch_key.py](../fetch_key.py) can *derive* the key from Pioneer's own
public GPL source release (~280 MB download) and write `.env`.

Why it's a release blocker: today the prep path (Phase 3 `xdj-host prep`) only works
because the dev `.env` exists in the repo. A shipped app has no `.env`, and we must
**not** bundle the key or silently auto-fetch it — shipping the key (or a one-click
grab of it) distributes Pioneer-owned material, exactly what CLAUDE.md's standing
constraint forbids.

Plan (when we reach it):
- On first firmware drop with no key on hand, **prompt the user** for the key — accept
  an `aes256.key` passphrase file (drag/drop or file picker) or a 64-hex string, and
  wire it straight through to the prep step (`--key-file` / `XDJRX_AES_KEY`). The user
  owns the device and supplies the key, the app never ships it.
- Cache the supplied key per-user in app-data (same place as the prepared firmware), so
  it's asked for once — decide: store the raw key vs. only remember "key present".
- `fetch_key.py` (auto-derive from the GPL source): keep it a **dev-only convenience**,
  off by default in the app; at most offer it as an explicit, user-initiated action with
  clear provenance. Decide the exact policy when building the key-prompt UX.
- `xdj-host prep` currently passes no key (relies on `.env`); add a key argument/env it
  forwards to `xdjrx_upd.py`, and surface a clear "firmware key required" error instead
  of a Python traceback when it's missing.

### B. The nand.img / prep pipeline must ship and run on every target (raised 2026-11-13)
Phase 3's prep is **Python** ([xdjrx_upd.py](../xdjrx_upd.py),
[qemu_prepare.py](../qemu_prepare.py), [qemu/build-nand.py](../qemu/build-nand.py)) and
shells out to **e2fsprogs** (`mke2fs`+`debugfs`) for the ext4 rootfs. The current engine
(`xdj-host`'s `python_cmd`) finds a Python via the repo venv / `uv` / system `python3` —
a **dev-only assumption**; a shipped app has none of those. This is the same portability
problem already analysed in **§5.1 (Python→Rust port)** and **§5.2 (ext4 creation is the
one hard dep)** — recording it here so Phase 3's engine is revisited during packaging:
- Interim (unblocks macOS + Linux now): **bundle** a standalone Python + the prep scripts
  + `mke2fs`/`debugfs` inside the app, and have `xdj-host` call the bundled copies instead
  of a system Python (replace `python_cmd`'s dev lookup with a resource-relative path).
- Windows is the weak point: e2fsprogs needs an MSYS2/Cygwin build (§5.2). This is the
  main argument for eventually **porting the pipeline (incl. an ext4 writer) to Rust** so
  prep is a single native binary with no Python/e2fsprogs runtime at all.
- Decide per platform: bundled-sidecar everywhere first, then Rust-port the ext4 step when
  Windows packaging demands it (§5.2).

## 6. Related
- [emulator-status.md](emulator-status.md) — feature board (boot/USB/audio/panel).
- [audio-handover.md](audio-handover.md) — the `-audio` voice / timer-period model.
- [subucom-virtual-panel-handover.md](subucom-virtual-panel-handover.md) — the panel bridge this replaces.
- Skills: `qemu-imx6-patches`, `build-xdj-sd-image`, `run-xdj-emulator`, `xdj-virtual-panel`, `upd-firmware-format`.
</content>
</invoke>
