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
  (pots; FX SELECT / CH SELECT as a **stepped** bounded knob), `Kn.createInfiniteKnob` (the browse
  encoder, BEAT FX TIME). Jogs, faders and the
  crossfader are **custom** (the SVG Slider letterboxes too small in narrow cells, and the
  grip-bump knob looked like a cog): jog = CSS platter — static knurled rim + `.jogspin` platter that
  **rotates with the drag/wheel** (marker shows angle) + static `.jogcap` (blue ring while touched);
  pointer-angle → jog spec. Faders =
  div track + thumb (`vfader`/`vfader-c`/`xfader`). CUE/PLAY = round `.jogbtn` (play tinted green).
  Browse encoder = infinite knob with a centre **PUSH** button.
- **Interaction conventions (2026-11-15):**
  - **Double-click resets**: TEMPO faders + crossfader → centre (`centerDrag()`; these drag
    *relative* to the grab point, no jump on press, so a double-click doesn't nudge the value);
    TRIM / HI / MID / LOW / COLOR knobs → mid (`mid:true` in `C`). Channel faders have no reset.
  - **Selectors** (`selectorCell`, FX SELECT 8 pos, CH SELECT 6): bounded `Kn.Knob` with
    `step:1`, one tick per position, stops at both ends, wheel = one step. The library rotates by
    its *raw* value while dragging, so the cell overrides the instance's `updateVisuals`/`endDrag`
    to draw the stepped value (re-check if `audio-knobs` is upgraded).
  - Defaults: TRIM and MASTER level start at **half** (512), like the hardware's centre detent.
  - Arrow glyphs: `◄`/`►` come from different fallback fonts under the system font (different
    sizes), so `setLabel()` draws every `◄` as a mirrored `►` (`.flip`).
- **Layout notes:** LEVEL meters are one centred bank (`t:'meterbank'`: CH1 | MASTER | CH2 between
  the LOAD keys and XF CURVE, like the device; MASTER is one column — rbp sends one master level, and
  no dB scale is drawn since the segment→dB mapping is unknown). BROWSE/TAG LIST/INFO/MENU are evenly
  spaced; the display keys right of the screen use the left column's pitch, top-aligned.
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
- **Pop-out screen** (header `⧉ Screen`): the same page with `?screen=1` renders only `#screen`,
  letterboxed in a resizable window, and never pushes state/holds (view-only; a second VNC client).
  In the app it is a Tauri `WebviewWindow` labelled `screen` (JS API; needs
  `core:webview:allow-create-webview-window` + `core:window:allow-set-focus` in the capability —
  `window.open` does nothing in WKWebView); it closes with `main`. In a browser it's `window.open`.
  Aspect: the shell's `on_page_load` locks the `screen` window to the 800x480 framebuffer via
  `NSWindow.contentAspectRatio` (macOS; Tauri has no option for it). The browser pop-up instead snaps
  its height after a resize settles.

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
| `POST /ctrl` | `A <off> <val16>` / `B <off> <mask>` / `S <off> <mask> <bits>` / `a` / `b` | set analog / hold button / set switch / one-shot (full state re-sent) |
| `POST /release` | held `B` line(s) / empty | let go of that button (empty = all held buttons) |
| `POST /jog` | `<deck> move <deg> <sec>` / `<deck> touch <0\|1>` | jog wheel counter/period/flags |
| `POST /input` | `<0\|1> DECK\|LINE` | per-channel source switch (holds `B 33 80`/`B 43 80`); default DECK |
| `POST /browse` | signed detents | ramp the browse counter at `rbp[0x54]/[0x55]` (±1/step, ~45 ms dwell) |
| `POST /mainkey` `/mainkey_release` | gpio / — | hold/release a MAIN-UCOM GPIO key |
| `POST /usb` | `<usb1\|usb2> <path>` | mount a disk image/device on a front slot (see below) |
| `GET /raw` | — | framebuffer (`X-Width`/`X-Height` headers, body `w*h*3`) |
| `GET /leds` | — | **SSE** stream of rbp's 20-byte LED payload (`data: <40 hex>` per change); `?once=1` = JSON |
| `GET /cal-ref` | — | dev calibration overlay image (`XDJ_CAL_REF`; 404 if unset) |

## Inject/hold model (important — reworked 2026-11-14)
The QEMU harness treats each snapshot as the **whole** frame state (an omitted field falls back
to the auto-level default / 0). So `server.py` keeps the panel's **full state** and re-sends all
of it on every write (`_compose_ctrl`):
- `_analog {off: val}` — every fader/knob moved (persistent; `A` lines).
- `_held {(off,mask)}` — buttons currently down; **many at once** (SHIFT combos), OR-merged per
  byte. `B` line on press, `POST /release` with that same line on release (empty body = all).
- `_switch {(off,mask): bits}` — multi-position switches (`S <off> <mask> <bits>`): FX SELECT,
  CH SELECT, XF CURVE, MIC — they share byte `0x5a`/`0x5b` with buttons.
- `_jog` per deck — counter `+0x0e`, period `+0x0c`, flags `+0x10` (touch/reverse); `POST /jog`
  `"<deck> move <deg> <sec>"` / `"<deck> touch <0|1>"`; an idle thread zeroes the period.
- browse counter (`0x54`) and INPUT SELECT holds, as before. One-shot `a`/`b` ride on top.
Buttons are **held while pressed** (real-hardware semantics), not one-shots. The panel sends all
analog/switch defaults once at load (`pushInitialState`) so the emulator matches the UI. Needs
QEMU **patch 14** (frame hook + `MAXHOLD` 64). The control table `C` in `panel.html` is
runtime-verified — see the top table in `docs/subucom-frame-map.md` before changing a spec.

## USB mount from the panel (`/usb`, LOAD buttons)
`USB1 LOAD` / `USB2 LOAD` sit above the USB STOP buttons. The button opens a **native file dialog**
(in the app, via the Tauri dialog plugin — `window.__TAURI__.dialog.open`; dev browser falls back to
a typed-path prompt), then POSTs `<slot> <path>` to `/usb`. `mount_usb()`:
1. **Ejects first** (`device_del` + `drive_del` of the previous panel insert *and* the boot
   auto-insert id) and removes any stale patch-06 fault file — USB STOP is only a firmware unmount, the QEMU
   device stays plugged, so a second LOAD would hit "Duplicate ID" / port-in-use.
2. Symlinks the image to a space-free path, `drive_add`+`device_add` on the hub (port **1.2** = usb1,
   **1.1** = usb2) with a **fresh unique id**, read-only (`snapshot=on`) by default.
3. Does **not** write `/tmp/usb_insert` / `/tmp/usb2_insert` (it removes any stale one). Those
   patch-06 files drive rbp's active-low USB **fault** flags — asserting one causes "USB Error.
   Remove the device." The attach alone wakes HOST1 (patch 10). Mirrors the host's `maybe_insert_usb`
   (docs/usb-mount-howto.md, usb-slot-topology.md). Monitor access: QMP `human-monitor-command`
   when `--qmp` is set, else raw HMP on `--mon`.

## LEDs, meters, jog rings (`/leds`, QEMU patch 15)
rbp sends the panel MCU its LED state in the MOSI half of every sub-ucom burst (`SubTxData`).
Patch 15 publishes each change as `T <40 hex>\n`: **socket mode** = back up the `xdjctrl`
connection (the server's `_leds_sock_loop` shares `_conn["ctrl"]` with `_sock_send`); **file
mode** = `/tmp/subucom_tx` (`XDJ_TX_PATH`), polled by `_leds_file_loop`. The server fans it out
over SSE; `panel.html` `startLeds()` → `applyLeds(hex)`:
- buttons with `led:[byte,bit,activeLow]` (+ optional `ledc` colour) get class **`lit`**
  (separate from `.down` = finger). Blinking is rbp toggling the bit — just mirror it, **except**
  `steady:true` entries (COLOR FX = blue, BEAT FX ON = yellow): rbp blinks those while on
  (~0.31 s on/off, measured), so the panel holds `lit` until the bit has stayed off for
  `STEADY_HOLD_MS` (550 ms) — a constant glow, ~0.5 s turn-off delay.
  Deck entries use `Ld(byte,bit,low)` (adds the deck-2 offset of +4 bytes).
- `t:'meter'` cells (`meter:<byte>`) = 12-segment bars from a 16-bit LE bitmask (CH1 `0x0e`,
  CH2 `0x0c`, MASTER `0x08`). Grid rows, not flex (WKWebView flex-fill collapse).
- jog cells get a `.jogled` ring: byte `0x00`/`0x04` = 1..36 position, 0 = full ring, else off.
Map + open items: `docs/subucom-led-map.md`. Map a new LED with `debug/ledprobe.py` (file mode).

## Adding a control
1. **Map it** (→ `map-subucom-{button,fader,knob,rotary}` / `map-gpio-control`) for its spec
   (`A <off> <val>` / `B <off> <mask>`) or GPIO.
2. Add an entry to `C` (`{l,t,spec|gpio|specUp/specDown|pushSpec}`) and place it with a `btn/knob/put` call in the layout block
   (design units, see Layout above). `t` ∈ `knob|vfader|vfader-c|xfader|rotary|
   encoder|jog|` (default = button). Buttons: `spec` (sub-ucom), `gpio` (mainkey), `pad`/`big`/
   `round` styling, `longpress`, `usb`, `led:[byte,bit,low]`/`ledc` (LED from `/leds`).
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
  `_compose_ctrl` handles it — don't bypass it (same for the jog and TIME counters).
- To verify a mapping without the LCD: break on the dispatcher `0x2a47c0` (`r1`=code,
  `r2`=state, `r3`=deck) — but on `-smp 1`, step off each bp, and keep halts sparse (the UI
  stops reacting after many halts; reboot before visual checks).
- The log box re-enables `user-select` (the panel is `user-select:none` globally).

## References
`tools/xdj-panel/{server.py,panel.html}`, `app/ui/{node_modules/audio-knobs,novnc}`.
Skills: [build-xdj-app], [run-xdj-emulator], [map-subucom-button/fader/knob/rotary],
[map-gpio-control]. Docs: `docs/subucom-frame-map.md`, `docs/usb-mount-howto.md`. Memory: [[photo-panel]].
