---
name: xdj-virtual-panel
description: Work on the XDJ-RX virtual web panel (tools/xdj-panel/) - the browser UI that streams the emulator's screen and drives its controls by injecting sub-ucom frames / GPIO keys. Use when adding or fixing a panel control, wiring a newly-mapped control into the UI, changing the server's inject/hold model, or debugging the panel↔emulator bridge.
---

# XDJ-RX virtual panel (`tools/xdj-panel/`)

A local web app that **shows the live device screen** and **operates the emulator** by writing
the inject files the QEMU harness reads. It's the human-facing counterpart to the `map-*` skills:
once a control's frame spec is known, you expose it here.

## Pieces
- **`server.py`** — HTTP bridge. Serves `panel.html`, streams the QEMU framebuffer (`/raw`, raw
  RGB from a persistent `screendump` over the monitor socket), and turns panel POSTs into
  `/tmp/subucom_ctrl` / `/tmp/mainkey` writes. Run:
  `python3 tools/xdj-panel/server.py --port 8765 --mon /tmp/d_mon4.sock` → http://127.0.0.1:8765.
  **`--mon` must match the launch's monitor socket** (debug launcher uses `/tmp/d_mon4.sock`).
- **`panel.html`** — a photo of the device (`assets/xdj-rx.png`) with **calibratable hotspots**
  (`CTRLS` list; press **C** to drag controls onto the photo, Save → `assets/calibration.json`)
  plus always-visible top-bar buttons.
- **`encoder_bridge.py`** — optional helper for the browse encoder. **`debug/`** — the RE
  launcher + raw-RSP/inject/console helpers (see `run-xdj-emulator`).

## Server endpoints
| route | body | effect |
|---|---|---|
| `POST /ctrl` | `A <off> <val16>` / `B <off> <mask>` / `a` / `b` | write a sub-ucom spec line (replaces the ctrl file; persistent holds re-appended) |
| `POST /release` | — | drop the momentary control (keeps persistent state: browse counter + input-select) |
| `POST /input` | `<0\|1> DECK\|LINE` | per-channel **source switch** (holds `B 33 80` / `B 43 80`); default DECK on startup |
| `POST /browse` | signed detent count | ramp the browse encoder counter at `rbp[0x54]/[0x55]` (±1/step, ~45 ms dwell) |
| `POST /mainkey` / `/mainkey_release` | gpio number / — | hold/release a MAIN-UCOM GPIO key (`/tmp/mainkey`) |
| `GET /raw` | — | latest framebuffer (headers `X-Width`/`X-Height`, body `w*h*3`) |
| `GET /cal`, `POST /savecal` | — / JSON | load/save hotspot calibration |

## The inject/hold model (important)
Each `/ctrl` POST **replaces** `/tmp/subucom_ctrl`. The only state the server persists is the
**browse counter** and the **input-select** holds; `_compose_ctrl()` re-appends them to every
write (so a DECK channel's bit, or the encoder counter, survives other control writes). One-shot
`b`/`a` presses ride **on top of** holds (patch 07), so they aren't promoted to holds. If you add
another persistent control, give it the same treatment: a server-side state var + append in
`_compose_ctrl()` + preserve it in `/release`.

## Adding a control
1. **Map it first** (→ `map-subucom-{button,fader,knob,rotary}` or `map-gpio-control`) to get its
   frame spec (`A <off> <val>` / `B <off> <mask>`) or GPIO number.
2. **Expose it:**
   - A photo hotspot: add to the `CTRLS` list in `panel.html` with `type`
     (`btn`/`analog`/`browse`), and `spec`/`gpio`/`browse`; place it with Calibrate.
   - A simple always-on control: a top-bar `<button>` + a small handler calling `post('/ctrl', …)`
     (momentary) or a new persistent endpoint (follow the `/input` pattern for a held setting).
3. JS helpers: `post(path, body)`, `ctrl(spec)`, `release()`, `mainkey(n)` are global in
   `panel.html`. The source-switch toggle (`toggleSrc`/`setSrc` → `/input`) is the worked example.

## Gotchas
- Audio needs routing=DECK (source switch) **and** gains up (on-air gate) — bring up the faders.
- A bare `/ctrl` write replaces all holds; use the harness one-shot (`b`/`a`) or `debug/inj.py pulse`
  to avoid zeroing the analog gains.
- The browse counter must appear in **every** frame once moved, or the firmware reads a huge delta
  (jumps the list). `_compose_ctrl` handles this — don't bypass it for encoder writes.

## References
`tools/xdj-panel/{server.py,panel.html,encoder_bridge.py}`, `docs/subucom-frame-map.md`.
Skills: [run-xdj-emulator], [map-subucom-button], [map-subucom-fader], [map-subucom-knob],
[map-subucom-rotary], [map-gpio-control]. Memory: [[photo-panel]].
