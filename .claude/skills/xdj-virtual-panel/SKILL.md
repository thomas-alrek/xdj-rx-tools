---
name: xdj-virtual-panel
description: Work on the XDJ-RX virtual web panel (tools/xdj-panel/) - the browser UI that streams the emulator's screen and drives its controls by injecting sub-ucom frames / GPIO keys. Use when adding or fixing a panel control, adjusting the layout, wiring a newly-mapped control into the UI, changing the server's inject/hold model, mounting USB from the panel, or debugging the panel↔emulator bridge.
---

# XDJ-RX virtual panel (`tools/xdj-panel/`)

A local web app that **shows the live device screen** and **operates the emulator**. It's the
human-facing counterpart to the `map-*` skills: once a control's frame spec is known, expose it
here. Runs two ways: standalone in a browser (dev), and inside the Tauri desktop app as a sidecar
(see skill [build-xdj-app]).

## Design (post-rebuild, late 2026-11)
The panel was **rebuilt as an original flat design** — the old version used a copyrighted Pioneer
device **photo** as its background; that photo was deleted and must never ship again. Current
`panel.html`:

- **No photo.** A dark console drawn with decorative section boxes (`ZONES`) + real widgets.
- **Widgets** = npm **`audio-knobs`** (MIT, HowdyMoto/Knobs), vendored to `/knobs/`: `Kn.Knob`
  (pots), `Kn.createInfiniteKnob` (FX-select rotary; the browse encoder). Jogs, faders and the
  crossfader are **custom** (the SVG Slider letterboxes too small in narrow cells, and the
  grip-bump knob looked like a cog): jog = CSS platter — static knurled rim + `.jogspin` platter that
  **rotates with the drag/wheel** (marker shows angle) + static `.jogcap` (blue ring while touched);
  pointer-angle → jog spec. Faders =
  div track + thumb (`vfader`/`vfader-c`/`xfader`). CUE/PLAY = round `.jogbtn` (play tinted green).
  Browse encoder = infinite knob with a centre **PUSH** button.
- **Layout = grid in design units (late 2026-11 overhaul).** Positions are written in units of a
  `W×H = 2142×1184` canvas via helpers `put/btn/knob/round(id,cx,cy,w,h)` into `L`, then converted to
  percent `COORDS` of the fixed-aspect `#stage` (`aspect-ratio: 2142/1184`, sized with `min(100cqw,
  100cqh·aspect)` so it never distorts). Bands/zones are constants (`D1_X`, `D2_X`, `MIX_X`, `MAIN_Y`…);
  **one `deckLayout(n,x0)` drives both decks** (identical, NOT mirrored: CUE/PLAY + transport left,
  tempo on the right). The pad row spans exactly the jog diameter; display keys are spread evenly
  over the screen height in a `disp` housing with a `bezel`. `LBL_U` = engraved section labels.
  To move a control, edit its `btn/knob/put` call — keep shared rows/columns (`R1`, `RC`, `FL`…).
  `const C = {}` is the control registry (`l` label, `t` type, `spec`/`gpio`/`specUp`…).
- **Screen**: noVNC (`?vnc=ws://host/`, RFB over WebSocket) in the app, or `/raw` screendump poll
  in dev. `#screen,#screen *{cursor:default!important}` keeps the pointer visible (noVNC hides it).

### Checking layout changes
Serve with `python3 tools/xdj-panel/server.py --port 8799` and screenshot headless Chrome
(`--headless --window-size=1760,1150 --screenshot=out.png --virtual-time-budget=2500`), **and** in
WebKit (the app's engine) — a tiny Swift `WKWebView.takeSnapshot` tool works. The old `?cal=` photo
overlay (`XDJ_CAL_REF`, `/cal-ref`, 404s if unset) still exists but no longer matches the grid layout.

## server.py
HTTP bridge. Serves `panel.html` + `/knobs/*` + `/novnc/*`, streams the framebuffer, and turns
panel POSTs into control writes. Run (dev): `python3 tools/xdj-panel/server.py --port 8765 --mon
/tmp/d_mon4.sock`. In the app the sidecar gets `--ctrl-sock/--key-sock` (Phase-1 chardevs) and
`--qmp qmp.sock` (for USB mount — there's no HMP monitor in the .app).

| route | body | effect |
|---|---|---|
| `POST /ctrl` | `A <off> <val16>` / `B <off> <mask>` / `a` / `b` | write a sub-ucom spec (replaces the ctrl file; persistent holds re-appended) |
| `POST /release` | — | drop the momentary control (keeps browse counter + input-select) |
| `POST /input` | `<0\|1> DECK\|LINE` | per-channel source switch (holds `B 33 80`/`B 43 80`); default DECK |
| `POST /browse` | signed detents | ramp the browse counter at `rbp[0x54]/[0x55]` (±1/step, ~45 ms dwell) |
| `POST /mainkey` `/mainkey_release` | gpio / — | hold/release a MAIN-UCOM GPIO key |
| `POST /usb` | `<usb1\|usb2> <path>` | mount a disk image/device on a front slot (see below) |
| `GET /raw` | — | framebuffer (`X-Width`/`X-Height` headers, body `w*h*3`) |
| `GET /cal-ref` | — | dev calibration overlay image (`XDJ_CAL_REF`; 404 if unset) |

## Inject/hold model (important)
Each `/ctrl` POST **replaces** `/tmp/subucom_ctrl` (or pushes a NUL-framed snapshot on the
`--ctrl-sock` chardev). The server persists only the **browse counter** and **input-select** holds;
`_compose_ctrl()` re-appends them to every write. One-shot `b`/`a` presses ride **on top of** holds
(patch 07). A new persistent control needs the same treatment: a server state var + append in
`_compose_ctrl()` + preserve in `/release`.

## USB mount from the panel (`/usb`, LOAD buttons)
`USB1 LOAD` / `USB2 LOAD` sit above the USB STOP buttons. The button opens a **native file dialog**
(in the app, via the Tauri dialog plugin — `window.__TAURI__.dialog.open`; dev browser falls back to
a typed-path prompt), then POSTs `<slot> <path>` to `/usb`. `mount_usb()`:
1. **Ejects first** (`device_del` + `drive_del` of the previous panel insert *and* the boot
   auto-insert id) and removes the detect file — USB STOP is only a firmware unmount, the QEMU
   device stays plugged, so a second LOAD would hit "Duplicate ID" / port-in-use.
2. Symlinks the image to a space-free path, `drive_add`+`device_add` on the hub (port **1.2** = usb1,
   **1.1** = usb2) with a **fresh unique id**, read-only (`snapshot=on`) by default.
3. Writes the detect file (`/tmp/usb_insert` / `/tmp/usb2_insert`) — the insertion edge patch 10/06
   watches, so the firmware resumes HOST1 and mounts. Mirrors the host's `maybe_insert_usb`
   (docs/usb-mount-howto.md, usb-slot-topology.md). Monitor access: QMP `human-monitor-command`
   when `--qmp` is set, else raw HMP on `--mon`.

## Adding a control
1. **Map it** (→ `map-subucom-{button,fader,knob,rotary}` / `map-gpio-control`) for its spec
   (`A <off> <val>` / `B <off> <mask>`) or GPIO.
2. Add an entry to `C` (`{l,t,spec|gpio|specUp/specDown|pushSpec}`) and place it with a `btn/knob/put` call in the layout block
   (design units, see Layout above). `t` ∈ `knob|vfader|vfader-c|xfader|rotary|
   encoder|jog|` (default = button). Buttons: `spec` (sub-ucom), `gpio` (mainkey), `pad`/`big`/
   `round` styling, `longpress`, `usb`.
3. JS helpers in `panel.html`: `sctrl(spec)`, `release()`, `mainkey(n)`, `post(path,body)`,
   `doMount(slot,path)`. `cell(id)` dispatches by `t`.

## Gotchas
- **WebKit (the .app) ≠ Chrome (headless screenshots).** Flex-fill containers holding an SVG knob
  **collapse to 0** in WKWebView; use grid `fr` row-tracks or definite sizes (that's why the top
  zones and channel strips are grids). Always re-verify layout changes in the actual app.
- Round widgets: size by the **smaller** cell dimension (`height:100%;width:auto;aspect-ratio:1`
  for wider-than-tall cells) or you get ovals. Knob SVGs have 25% transparent padding — crop with a
  modest `transform:scale(1.28)` and `overflow:visible` (too much + `overflow:hidden` clips the dial).
- Audio needs routing=DECK (source switch) **and** gains up (on-air gate) — bring the faders up.
- Browse counter must appear in **every** frame once moved or the firmware reads a huge delta;
  `_compose_ctrl` handles it — don't bypass it.
- The log box re-enables `user-select` (the panel is `user-select:none` globally).

## References
`tools/xdj-panel/{server.py,panel.html}`, `app/ui/{node_modules/audio-knobs,novnc}`.
Skills: [build-xdj-app], [run-xdj-emulator], [map-subucom-button/fader/knob/rotary],
[map-gpio-control]. Docs: `docs/subucom-frame-map.md`, `docs/usb-mount-howto.md`. Memory: [[photo-panel]].
