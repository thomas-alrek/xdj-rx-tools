---
name: map-subucom-rotary
description: Find the sub-ucom frame field + encoding for an XDJ-RX endless ROTARY ENCODER (the central browse/select encoder rotate; and, where it ever gets decoded, jog rotation) so it can be injected from the virtual panel. Use when mapping a continuous *endless* control reported as a relative counter/delta (the firmware diffs successive values) - NOT an absolute pot/knob, NOT a fader, NOT a momentary button. The encoder PUSH is a button (use map-subucom-button).
---

# Mapping a sub-ucom ROTARY ENCODER (endless, relative/counter)

An endless encoder is **not** a level. The panel reports a running **counter** and the firmware
acts on the **delta** (new − previous). Driving it means *advancing the counter*, not setting a
value — inject a ramp, one step per detent, with a dwell long enough that each value is polled.

## Sub-ucom fundamentals (shared)
- 100-byte frame over `/dev/subucom_spi1.0`; dispatcher for center codes = **`FUN_0026ae7c`**.
- Inject (patch 02/07) via `/tmp/subucom_ctrl`. For a counter, write successive `A <off> <val>`
  holds (a ramp) with a dwell; the panel server's `/browse` endpoint already ramps the browse
  counter ±1 per detent. Tools: `tools/xdj-panel/debug/{inj.py,mon.py,rsp.py}`, `encoder_bridge.py`.

## Browse encoder anchors
- **ROTATE** = a 16-bit **counter at `rbp[0x54]/[0x55]`** (frame-map: inc = scroll **down/forward**,
  dec = up/reverse). The firmware tracks the previous value (delta-tracking, cf. the control object's
  prev at `+0x74`) and applies `delta` — e.g. `DAT_017f3234 += delta` via `FUN_000e12e0(delta)`,
  `delta = DAT_0107be1c`.
- **Event path:** `FUN_0026ae7c` builds a 0x24-byte encoder event via `FUN_002adfa0`, with a signed
  delta word at `event+0xc`.
- **PUSH** = `rbp[0x5d]` bit 4 — a **button**, map it with [map-subucom-button].
- **Code-number caveat:** `0x4205` has been attributed to both PUSH and EncoderRotate across docs;
  the later live work pinned **PUSH = `0x5d` bit 4 / code `0x4205`** and ROTATE = the `0x54/0x55`
  counter. When in doubt, trust a live screen-diff over the stale code label (reconcile in docs).

## Method
1. **Drive the counter, watch the list:** ramp `rbp[0x54]/[0x55]` by ±1 per step with a ~45 ms dwell
   (the `/browse` ramp, or `inj.py`), and `screendump` the library list scrolling. Too fast collapses
   detents; too many injects hit the quiescence limit (below).
2. **Find a NEW encoder's field:** inject a monotonic ramp at candidate offsets while watching the
   counter-driven UI element (list cursor, value); the offset whose ramp advances it is the counter.
   Confirm direction and that it is a *delta* (reversing the ramp reverses the UI), not a level.
3. **Confirm in firmware:** breakpoint `FUN_0026ae7c` and read the delta at `event+0xc`
   (`read_full_pseudocode` of the large dispatcher via REA; grep the saved file for `0x4205`,
   `FUN_002adfa0`, `DAT_0107be1c`). Resume via monitor `cont`.
4. Record offset + encoding in `docs/subucom-frame-map.md` and `docs/rotary-encoder-handover.md`.

## Gotchas
- **Emulator quiescence (~150 injects):** the harness/firmware stops reacting after a long inject
  burst; the encoder's continuous stream is hard to sustain. Keep ramps short; let it settle.
- **Browser-state dependent:** rotate only scrolls a *fresh, interactive* USB browser; on a
  non-interactive/stale list it does nothing. Open the browser first (source key via `/tmp/mainkey`).
- A separate **real-time quadrature stream at `rbp[0x5a]`** exists (once hit a track-load/select
  path) — exact 2-phase encoding still unpinned; note it if you touch it.
- **JOG rotation** is a *different* 24-byte "JogCom" ready-line protocol (not this 100-byte frame),
  undecoded even in RX3 — out of scope for this skill; lowest priority.
- Don't hammer RSP (rbp watchdog → crash); use the screen-diff oracle for encoders.

## References
`docs/rotary-encoder-handover.md`, `docs/subucom-frame-map.md`, `docs/rbp-and-panel-protocol.md`,
`tools/xdj-panel/encoder_bridge.py`. Sibling skills: [map-subucom-button], [map-subucom-fader],
[map-subucom-knob], [map-gpio-control].
