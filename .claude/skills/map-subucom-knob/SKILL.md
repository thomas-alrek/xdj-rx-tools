---
name: map-subucom-knob
description: Find the sub-ucom frame offset + control code for an XDJ-RX rotary POT / KNOB (3-band EQ HI/MID/LOW, TRIM/gain, COLOR/FILTER, headphone mixing) so it can be injected from the virtual panel. Use when mapping a continuous *pot* (absolute position, often with a center detent) reported as a 16-bit analog value in the 100-byte frame - as opposed to a linear fader or an endless rotary encoder.
---

# Mapping a sub-ucom KNOB (rotary pot, absolute 16-bit)

A knob is mechanically a pot: an **absolute** 16-bit value (`÷1023.0`), like a fader, but it
usually has a meaningful **center** (`0x200` = flat/12-o'clock) rather than min=off. EQ, TRIM
and COLOR knobs all come through the mixer **analog block decoder**.

## Sub-ucom fundamentals (shared)
- 100-byte frame over `/dev/subucom_spi1.0`; decoders `~0x282000-0x28b000`.
- Inject (patch 02/07) via `/tmp/subucom_ctrl`: `A <off> <val16>` hold (`0x200`=center,
  `0x3ff`=max, `0`=min); `a`=one-shot. Tools: `tools/xdj-panel/debug/{inj.py,mon.py,rsp.py,tables.py}`.

## Knob anchors — the analog block decoder at `0x282800` (confirmed)
Per channel, a contiguous block of 16-bit fields is decoded; each slot emits a code
(`push {r4,r5,r6,r7,lr}`, `r5`=current `r4`=previous). **Block base `0x24` (d1) / `0x34` (d2):**

| block+off | rbp off d1/d2 | code | control |
|---|---|---|---|
| +0 | `0x24`/`0x34` | `0x5008` | EQ HI |
| +2 | `0x26`/`0x36` | `0x5007` | TRIM / gain |
| +4 | `0x28`/`0x38` | `0x500a` | EQ LOW |
| +6 | `0x2a`/`0x3a` | `0x5009` | EQ MID |
| +8 | `0x2c`/`0x3c` | `0x5011` | CH FADER (linear — see [map-subucom-fader]) |
| +0xa | `0x2e`/`0x3e` | `0x508b` | COLOR / FILTER pot |

Setter leaves: EQ `FUN_000836dc`, level `FUN_00083df8`. (EQ MID/LOW were once mislabelled —
trust the codes: `0x5009`=MID, `0x500a`=LOW.)

## Method
1. **Inject + setter breakpoint:** `A <off> 3ff` (or `0` / `0x200`) hold; gdb-RSP breakpoint on
   the EQ/level setter leaf; read `(channel/band, float)` to confirm which band and the curve
   (center `0x200` should map to the neutral float, e.g. `1.0f`=`0x3f800000` for unity EQ).
2. **Screen-diff:** some pots show no UI; EQ/COLOR may not paint. Prefer the setter breakpoint,
   or confirm audibly (sweep a band while a deck plays).
3. **Distinguish knob vs fader:** both are 16-bit analog in the same block; the knob's neutral is
   center (`0x200`), the fader's is min (`0`). Decode is identical — this skill vs
   [map-subucom-fader] differ only in which slots/codes and the neutral value.
4. Record in `docs/subucom-frame-map.md` (Analog continuous table).

## Gotchas
- EQ at 0 kills the channel (the on-air gate `FUN_00065028` requires EQ not all-zero) — hold EQ
  at center `0x200` when testing other controls.
- Change-gated: the block decoder fires on a value change; a stuck hold won't re-emit. Release
  (`inj.py pulse`) or vary the value.
- Don't hammer RSP (rbp watchdog). Space gdb stops.

## References
`docs/subucom-frame-map.md`, `docs/rbp-and-panel-protocol.md`, `docs/rx3-panel-crossref.md`.
Sibling skills: [map-subucom-fader], [map-subucom-button], [map-subucom-rotary], [map-gpio-control].
