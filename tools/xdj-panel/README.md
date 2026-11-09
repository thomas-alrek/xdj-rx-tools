# XDJ-RX virtual panel

A small local web app for driving the emulated XDJ-RX panel by hand — buttons,
tempo/channel faders, EQ, beat-FX — laid out like the physical unit, with a **live
view of the QEMU screen** so you see each control react. Use it to test the mapped
controls and to discover the unmapped ones (press, watch, note the frame offset/bit
shown in the log).

## Run

1. Boot the emulator so its **monitor socket** is at `/tmp/d_mon4.sock` and it reads
   `/tmp/subucom_ctrl` (the standard launch: `PORT=1234 SUF=4 … launch_v2.sh`, with
   `XDJUSB=test/usb/rekordbox_test.img` if you want the browser/USB path). Wait for the
   deck UI (not the splash).
2. Start the bridge and open it:
   ```
   python3 tools/xdj-panel/server.py           # --port 8765  --mon /tmp/d_mon4.sock
   open http://127.0.0.1:8765
   ```

The bridge serves the panel, turns clicks/drags into the injection protocol
(`/tmp/subucom_ctrl`), and streams the framebuffer (via the monitor socket) as the
live screen.

## Using it

- **Buttons** hold while the mouse is down and release on mouse-up (a real press/hold).
- **Faders / knobs** (range sliders) hold at their last position.
- Only **one control is active at a time** — the harness reads a single control line, so
  moving a new control replaces the previous hold. **Release all** clears it.
- The **log** shows the exact spec sent for every action (e.g. `→ B 12 08`), so when an
  unverified control does something, you know its frame offset/bit.
- **Probe** panel: hold any bit of any byte, sweep a 16-bit analog, or send a raw spec
  (`B <off> <bit>` hold button · `A <off> <val>` hold analog · lowercase = one-shot; hex).

## Mapping status (colour)

- 🟢 **verified** (green underline) — confirmed on screen: TEMPO faders & TEMPO RANGE
  (both decks), BEAT FX ROLL/TAP. See [`docs/xdj-rx-control-map.md`](../../docs/xdj-rx-control-map.md).
- 🟡 **best-guess** (amber) — plausible offsets/bits not yet confirmed (deck transport,
  mixer, browser/nav, most beat-FX). Press them and watch the screen to confirm or
  correct; audio-only controls (EQ/faders/crossfader/mic) won't change the screen — those
  need the gdb event-code method noted in the control-map doc.

## Protocol

`POST /ctrl` body `"B 12 08"` → writes the spec to `/tmp/subucom_ctrl`.
`POST /release` → removes it. `GET /screen` → current framebuffer as PNG.
The harness (QEMU `xdj-subucom`, `qemu/patches/02`) interprets the spec each frame.
