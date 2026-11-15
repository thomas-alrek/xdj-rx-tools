---
name: xdj-virtual-panel
description: Work on the XDJ-RX virtual web panel (tools/xdj-panel/) - the browser UI that streams the emulator's screen and drives its controls by injecting sub-ucom frames / GPIO keys. Use when adding or fixing a panel control, adjusting the layout, wiring a newly-mapped control into the UI, changing the server's inject/hold model, mounting USB from the panel, or debugging the panel↔emulator bridge. Also covers the app's native menu bar (MENU in panel.html), the pop-out screen, panel LEDs/meters, and the Sub uCom / System log windows (logs.html, /log/*).
---

# XDJ-RX virtual panel (`tools/xdj-panel/`)

A local web app that **shows the live device screen** and **operates the emulator**. It's the
human-facing counterpart to the `map-*` skills: once a control's frame spec is known, expose it
here. Runs two ways: standalone in a browser (dev, `server.py`), and inside the Tauri desktop app (served by `xdj-host`'s in-process `panel.rs`)
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
- **Channel INPUT SELECT** (DECK ↔ LINE/PHONO, frame bits `0x33.7`/`0x43.7`) is the toggle in each
  channel strip (`srcCh` entries `deck1src`/`deck2src`, ids `src0`/`src1`, blue DECK / amber LINE;
  `toggleSrc` → `POST /input`). It used to be in the header. The rear **LINE/PHONO** EXT-TYPE switch
  (GPIO 119/120) is deliberately **not** on the panel — there's no external audio to switch.
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
- **The window is only the virtual deck** (2026-11-15): no header bar, no log strip, no in-page
  menu (by request — don't add one). Everything else lives in the **native menu bar** (app only):
  `MENU` in `panel.html` = File | Edit | Mixer | Deck 1 | Deck 2 | Color FX | FX | Master | View |
  Window, built with `window.__TAURI__.menu` (`buildAppMenu`, needs `core:menu:default`) and set as
  the app menu. A plain browser gets no menu. Menu actions go *through the widgets*: `tap(id)` =
  momentary press of a button (visual + spec/gpio, auto-release), `setW(id,v)` = a widget's
  `WG[id].set(v)` (knobs/faders/xfader/selectors/switches register setters), `setSrc`, `mountUsb`.
  To expose a new control in the menu, add a line to `MENU`/`deckMenu`.
- **Logs**: `log(m)` goes to the browser console and `POST /log/note`. View > *View Sub uCom Log* /
  *View System Log* open `logs.html?src=subucom|console` (Tauri windows `log-subucom`/`log-console`,
  or browser pop-ups), streaming `GET /log/<src>`: the sub-ucom log = every control POST (`->`),
  rbp LED changes (`<-`, blink toggles and meter/jog bytes suppressed) and panel notes (`·`); the
  system log tails the guest console (`--console`; default `console.log` beside `--ctrl-sock` = the
  app's runtime dir, else `/tmp/xdj_console.log`, which `relaunch.sh` writes via chardev `logfile=`).
- **POWER switch** (top-left, `t:'power'`, red LED = on; hidden unless `GET /power` says
  `available`, i.e. run by `xdj-host`). Click = `POST /power on[ update|test]|off` (panel → host
  `power.sock`, see [build-xdj-app]). File menu: *Power On / Off*, check items *Enter Firmware Update
  Mode (at next power on)* and *Power On at Launch* (both `localStorage` prefs `xdj.updateMode` /
  `xdj.powerOnLaunch`). Powered off: LED stream sends `off` (panel darkens everything) and `#screen`
  gets a black `.off` overlay. **USB LOAD while off is queued** (`/usb` → `_pending_usb`) and inserted
  after power on once the guest is ready: normal boot = console `Resolve xrun` + 6 s, update mode =
  `USB update mount` + 2 s (same choreography as `maybe_insert_usb`). On ctrl-socket reconnect (a fresh
  QEMU) the panel re-sends the full panel state (`_repush` / `ctrl_conn`).
- **The deck is the root**: `.stagewrap` has a 1rem padding (the only margin) and `#stage` no border/radius/shadow; the
  visible canvas is the design canvas cropped by `CROP`=16 units per side (`VW`x`VH` = 2110x1152, via
  CSS vars `--vw/--vh`), and the app window is aspect-locked to exactly that (`DECK_AR` in the shell).
  If the viewport's shape is within ±10% of the deck's (`FILL_TOL`), `#stage.fill` stretches to fill
  it exactly instead of letterboxing (on macOS the window's reported size includes the ~30 px title
  bar, so an "aspect-locked" window still left side bands); round widgets size from their smaller side,
  so they stay round. Further off → letterbox. Don't put CSS transitions on things toggled by
  `visibility` (an offscreen WKWebView never ticks them — the power button uses `transition:none`).
- **Pop-out screen** (**double-click the screen**, or View > Pop Out Screen): the same page with `?screen=1` renders only `#screen`,
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

## The panel server: two implementations, kept identical
- **In the app: `app/host/src/panel.rs`**, in-process in `xdj-host` (axum, :8765): socket mode only
  (`xdjctrl`/`xdjkey` chardevs, QMP for USB, power through the host's channel).
- **Dev / debug launcher: `tools/xdj-panel/server.py`**: the same routes plus `/tmp`-file mode, the HMP
  screendump (`/raw`) and calibration. Run: `python3 tools/xdj-panel/server.py --port 8765 --mon
  /tmp/d_mon4.sock`.
- **Change both, then run `tools/xdj-panel/test_panel_parity.py`**: it plays one scripted session
  against each (fake chardev sockets record every snapshot) and diffs snapshots, HTTP replies, LEDs and
  the control log. Byte-for-byte quirks matter (e.g. `%02x` of negatives, unknown `/jog` verbs = ok).

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
| `GET /power` | — | `{available, state:on/off, update, pending:{slot:path}}` (host power control) |
| `POST /power` | `on` / `on update` / `off` / `toggle` | power the emulator via `xdj-host` (503 when not run by it) |
| `GET /log/subucom` `/log/console` | — | **SSE** log lines (backlog first); page `GET /logs.html?src=…` |
| `POST /log/note` | text | append a panel note to the sub-ucom log |
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
`xdj-panel.c` (was patch 14)** (frame hook + `MAXHOLD` 64). The control table `C` in `panel.html` is
runtime-verified — see the top table in `docs/subucom-frame-map.md` before changing a spec.

## USB mount from the panel (`/usb`, LOAD buttons)
`USB1 LOAD` / `USB2 LOAD` sit above the USB STOP buttons. The button opens a **native file dialog**
(in the app, via the Tauri dialog plugin — `window.__TAURI__.dialog.open`; dev browser falls back to
a typed-path prompt), then POSTs `<slot> <path>` to `/usb`. `mount_usb()`:
1. **Ejects first** (`device_del` + `drive_del` of the previous panel insert *and* the boot
   auto-insert id) and removes any stale `xdj-panel.c` (was patch 06) fault file — USB STOP is only a firmware unmount, the QEMU
   device stays plugged, so a second LOAD would hit "Duplicate ID" / port-in-use.
2. Symlinks the image to a space-free path, `drive_add`+`device_add` on the hub (port **1.2** = usb1,
   **1.1** = usb2) with a **fresh unique id**, read-only (`snapshot=on`) by default.
3. Does **not** write `/tmp/usb_insert` / `/tmp/usb2_insert` (it removes any stale one). Those
   `xdj-panel.c` (was patch 06) files drive rbp's active-low USB **fault** flags — asserting one causes "USB Error.
   Remove the device." The attach alone wakes HOST1 (`xdj-usbnc.c` (was patch 10)). Mirrors the host's `maybe_insert_usb`
   (docs/usb-mount-howto.md, usb-slot-topology.md). Monitor access: QMP `human-monitor-command`
   when `--qmp` is set, else raw HMP on `--mon`.

## LEDs, meters, jog rings (`/leds`, `xdj-panel.c` (was patch 15))
rbp sends the panel MCU its LED state in the MOSI half of every sub-ucom burst (`SubTxData`).
`xdj-panel.c` (was patch 15) publishes each change as `T <40 hex>\n`: **socket mode** = back up the `xdjctrl`
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
- **byte `0x14`** (`xdj-board.c` (was patch 17), the 21st byte) = source-key LEDs from GPIO 162–165: bit0 rekordbox,
  bit1 MIDI, bit2 USB1, bit3 USB2 — the `src_*` keys use `led:[0x14,n]` like any other LED.
  `server.py` accepts 40 or 42 hex.
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
- **Double-click on the noVNC screen never fires `dblclick`**: noVNC `setCapture()`s on mousedown,
  which in WebKit lays a full-window proxy div over the page. The pop-out detects two `pointerdown`s
  in the **capture phase** instead. Dev-browser tests use the `/raw` canvas (no noVNC) and won't show
  this — test in the app.
- The panel is `user-select:none` globally; text to copy lives in the log windows (`logs.html`).
- Loading the panel page **pushes its full default state** (`pushInitialState`) — opening a second
  panel tab or a headless snapshot resets the emulator's mixer to the panel defaults.

## References
`tools/xdj-panel/{server.py,panel.html,logs.html}`, `app/ui/{node_modules/audio-knobs,novnc}`.
Skills: [build-xdj-app], [run-xdj-emulator], [map-subucom-button/fader/knob/rotary],
[map-gpio-control]. Docs: `docs/subucom-frame-map.md`, `docs/subucom-led-map.md`, `docs/usb-mount-howto.md`. Memory: [[photo-panel]].
