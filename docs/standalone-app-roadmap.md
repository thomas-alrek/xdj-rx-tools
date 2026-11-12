# Standalone app roadmap — packaging the XDJ-RX emulator

**Status: IN PROGRESS — Phase 1 (control socket) done 2026-11-12; Phases 2–5 planned.**
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

### Phase 2 — Tauri shell + orchestrator ⬜
- Scaffold the Tauri app. Rust host spawns & supervises the qemu child and owns
  **three per-instance sockets** (QMP / VNC / control) in the app runtime dir —
  no global `/tmp`, so multiple instances are possible.
- Webview loads panel.html. Screen pane becomes **noVNC**.
  - Wrinkle: noVNC speaks **RFB-over-WebSocket**; QEMU `-vnc` is **raw-TCP RFB**.
    The Rust host runs a tiny `websockify`-style WS↔TCP bridge (trivial w/ tokio).
  - panel overlay / hotspots / calibration stay as-is.
- Port server.py's control composition (input-select DECK/LINE holds, browse
  counter ramp, one-shot vs hold) into Rust — **or** run server.py as a bundled
  sidecar first to move faster, port later.
- Retire the HTTP `/raw` screendump path once the screen pane is live.

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

### Phase 3 — Firmware drag-and-drop + app-data ⬜
- Webview drag-drop target accepts a `.UPD`/`.zip`. Rust runs the prep pipeline
  (bundled Python sidecar initially) on the dropped file.
- Output to a real app-data dir, **keyed by firmware hash**, so re-dropping the
  same firmware is instant:
  - macOS: `~/Library/Application Support/XDJ-RX/<hash>/`
  - Linux: `$XDG_DATA_HOME/xdj-rx/<hash>/` (fallback `~/.local/share`)
- Repoint the scripts from "URL + `build/`" to "input path + output dir":
  [xdjrx_upd.py](../xdjrx_upd.py), [qemu_prepare.py](../qemu_prepare.py),
  `build-nand` (skill `build-xdj-sd-image`). AES-key fetch
  ([fetch_key.py](../fetch_key.py)) stays automatic + cached.
- Progress UI driven from Rust (the pipeline is ~minutes on a cold firmware).
- **Distribution rule (enforced by this model):** ship the emulator + tooling
  only. **Never** bundle firmware, the AES key, or `nand.img` — boot blobs are
  generated at runtime from the user's `.UPD`. This is what keeps the app legally
  distributable and matches the preservation framing in
  [emulator-status.md](emulator-status.md).

### Phase 3b — Physical USB passthrough ⬜
Mount a **real** host USB stick into the guest, the same way images mount today.
Already supported by the current QEMU build — **no new patch** (verified: the
binary knows `usb-host` *and* `usb-storage`, and libusb was auto-detected). Same
"media in" surface as firmware drag-drop, hence a Phase 3 sibling.

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

### Phase 4 — Audio backend selection ⬜
- Rust picks `-audio` by OS with a **fallback chain** (Linux: `pipewire`→`pa`→
  `alsa`; macOS: `coreaudio`; Windows: `wasapi`/`dsound`), always keeping
  `timer-period=1451` (see watch.sh for why). Expose a device picker in the UI.
- Mostly lifting the OS split already in watch.sh into the orchestrator.

### Phase 5 — Packaging & cross-platform build 🧱 (the long pole) ⬜

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

## 6. Related
- [emulator-status.md](emulator-status.md) — feature board (boot/USB/audio/panel).
- [audio-handover.md](audio-handover.md) — the `-audio` voice / timer-period model.
- [subucom-virtual-panel-handover.md](subucom-virtual-panel-handover.md) — the panel bridge this replaces.
- Skills: `qemu-imx6-patches`, `build-xdj-sd-image`, `run-xdj-emulator`, `xdj-virtual-panel`, `upd-firmware-format`.
</content>
</invoke>
