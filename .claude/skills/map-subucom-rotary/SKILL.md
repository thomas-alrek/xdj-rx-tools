---
name: map-subucom-rotary
description: Find the sub-ucom frame field + encoding for an XDJ-RX endless ROTARY ENCODER or counter (the central browse encoder rotate, BEAT FX TIME, the jog-wheel position counter) so it can be injected from the virtual panel. Use when mapping a continuous *endless* control reported as a relative counter/delta (the firmware diffs successive values) - NOT an absolute pot/knob, NOT a fader, NOT a momentary button. The encoder PUSH is a button (use map-subucom-button).
---

# Mapping a sub-ucom ROTARY ENCODER (endless, relative/counter)

An endless encoder is **not** a level. The panel reports a running **16-bit counter** and the
firmware acts on the **delta** (new − previous). Driving it means *advancing the counter*, not
setting a value — inject a ramp, one step per detent, with a dwell long enough that each value is
polled.

## Sub-ucom fundamentals (shared)
- ONE 100-byte frame over `/dev/subucom_spi1.0`; dispatcher for center codes = **`FUN_0026ae7c`**.
- Inject via the `xdj-panel.c` harness: `/tmp/subucom_ctrl` (file mode) or the `xdjctrl` chardev
  (socket mode). For a counter, write successive `A <off> <val>` holds (a ramp) with a dwell; the
  panel server's `POST /browse <detents>` ramps the browse counter ±1 per detent (~45 ms dwell).
  Tools: `tools/xdj-panel/debug/{inj.py,mon.py,rsp.py}`.
- **Once a counter has been sent it must be in every frame** — dropping it falls back to 0 and
  reads as a huge delta. The panel server's `_compose_ctrl` keeps it; don't bypass it.

## Known counters (`docs/subucom-frame-map.md`, Encoders + Jog)
| field | control | notes |
|---|---|---|
| `0x54/0x55` | BROWSE rotate | counting up scrolls down/forward |
| `0x56/0x57` | BEAT FX TIME (`0x448a`) | relative |
| `0x0e` (+`0x0c` period, `0x10.0` touch, `0x10.1` reverse) | JOG (`0x4306` via `FUN_00283dc4`) | position counter wraps at 65535; speed = 416.67 / period (0 or `0xffff` = stopped); deck 2 = +0x10. Panel: `POST /jog`, 1024 ticks/turn |

- **Browse event path:** `FUN_0026ae7c` builds a 0x24-byte encoder event via `FUN_002adfa0`, signed
  delta at `event+0xc`; the firmware keeps the previous value (control object `+0x74`) and applies
  the delta (e.g. `DAT_017f3234 += delta` via `FUN_000e12e0(delta)`, `delta = DAT_0107be1c`).
- **PUSH** = `0x5d.4`, code `0x4205` — a **button**, map it with [map-subucom-button].

## Method
1. **Drive the counter, watch the list:** ramp the counter by ±1 per step with a ~45 ms dwell
   (`/browse`, or `inj.py`) and `screendump` the library list scrolling. Too fast collapses detents.
2. **Find a NEW counter's field:** inject a monotonic ramp at candidate offsets while watching the
   counter-driven UI element (list cursor, value); the offset whose ramp advances it is the counter.
   Confirm it is a *delta* (reversing the ramp reverses the UI), not a level.
3. **Test-mode oracle:** test **Mode 8** shows the rotary SELECTOR/TIME counters (skill
   [run-xdj-emulator], "Service modes").
4. **Confirm in firmware:** breakpoint `FUN_0026ae7c` and read the delta at `event+0xc` (REA
   pseudocode of the dispatcher; grep it for `FUN_002adfa0`, `DAT_0107be1c`). Resume via monitor `cont`.
5. Record offset + encoding in `docs/subucom-frame-map.md`.

## Gotchas
- **Long inject bursts:** after a long stream of injects the UI can stop reacting — keep ramps
  short and let it settle.
- **Browser-state dependent:** rotate only scrolls a *fresh, interactive* browser list. Open the
  browser first (BROWSE `0x5f.3`); it lists **USB1**.
- Don't hammer RSP (rbp watchdog → crash); prefer the screen-diff oracle for encoders.

## References
`docs/subucom-frame-map.md`, `docs/rbp-and-panel-protocol.md`. Sibling skills:
[map-subucom-button], [map-subucom-fader], [map-subucom-knob], [map-gpio-control],
[xdj-virtual-panel].
