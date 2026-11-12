---
name: map-subucom-fader
description: Find the sub-ucom frame offset + control code for a linear XDJ-RX FADER (channel fader, crossfader, tempo/pitch fader, master/booth/headphone level) so it can be injected from the virtual panel. Use when mapping a continuous linear control reported as a 16-bit analog value in the 100-byte frame.
---

# Mapping a sub-ucom FADER (linear 16-bit analog)

A fader is a **16-bit little-endian** value in the frame, scaled `÷1023.0` to a 0..1 float,
decoded into a `0x50xx`/`0x44xx`/`0x600f` control code, then applied by a C++ setter leaf.

## Sub-ucom fundamentals (shared)
- ONE 100-byte frame over `/dev/subucom_spi1.0`; parsed by `PanelComController` (`FUN_0021a4ac`),
  decoders at **~0x282000-0x28b000**.
- **Inject harness** (QEMU patch 02/07) reads `/tmp/subucom_ctrl` each frame, rewrites the field,
  recomputes CRC. `A <off> <val16>` = hold a 16-bit analog (`0x3ff`≈full, `0x200`≈center, `0`=min);
  `a` = one-shot. Offsets are rbp frame offsets.
- Tools: `tools/xdj-panel/debug/{inj.py,mon.py,rsp.py,tables.py,relaunch.sh}`. `inj.py pulse`
  preserves other holds (writing a bare line to the ctrl file replaces all holds and zeroes the mix).

## Fader-specific anchors (confirmed)
| rbp off (d1/d2) | code | control | setter leaf |
|---|---|---|---|
| `0x2c` / `0x3c` | `0x5011` | CH FADER | `FUN_00083fcc` setChannelFader |
| `0x4e` | `0x600f` | CROSSFADER | `FUN_00066c2c` |
| `0x04`/`0x0a` (+`0x14`/`0x1a` d2) | — | TEMPO/pitch fader (dual-complementary pair) | — |
| `0x4a` | `0x4405` | MASTER LEVEL | `FUN_00066bb8` setMasterVolume |
| `0x48` | `0x440c` | BOOTH MONITOR | — |
| `0x50` | `0x4403` | HP LEVEL | — |
| `0x4c` | `0x448b` | LEVEL DEPTH | — |

- Continuous-analog decoder: **`FUN_002adee8`** region `~0x282400-0x282800`; the mixer analog
  **block decoder at `0x282800`** emits per-block codes (see [map-subucom-knob] for the block layout).
- Many channel faders are **dual-complementary** (a main field + an inverted copy); the decoder
  reads both. The TEMPO fader is a known complementary pair.

## Method
1. **Inject + setter breakpoint (most precise):** `A <off> 3ff` hold, then gdb-RSP breakpoint on
   the setter leaf (`FUN_00083fcc` fader, `FUN_00066bb8` master, …). Read the args: an index + a
   **float** (soft-float ABI: the float is an IEEE-754 bit pattern in a core reg; `1.0f`=`0x3f800000`,
   `0.5f`=`0x3f000000`). That identifies the control + confirms scaling. Resume via monitor `cont`.
2. **Screen-diff (no gdb):** inject a ramp (0x000→0x3ff) and `screendump`; watch the on-screen
   level meter / TEMPO % / crossfader move. Good for coarse identification.
3. **New fader:** sweep candidate even offsets with `A <off> 3ff` holds and watch the target UI
   element or the audio gate; confirm 16-bit by checking `<off>` and `<off>+1` both move it.
4. Record `(off_d1, off_d2, code, control, setter, confidence)` in `docs/subucom-frame-map.md`.

## Gotchas
- Audio **on-air gate `FUN_00065028`** needs master>22, level set, EQ not all-zero, fader≥6,
  crossfader — set sane gains (CH fader + master + xfader center + EQ center) or nothing is audible.
- Don't hammer RSP (rbp real-time watchdog → crash). Prefer screen-diff; use single/spaced gdb stops.

## References
`docs/subucom-frame-map.md` (Analog continuous table), `docs/rbp-and-panel-protocol.md`,
`docs/rx3-panel-crossref.md`. Sibling skills: [map-subucom-knob], [map-subucom-button],
[map-subucom-rotary], [map-gpio-control].
