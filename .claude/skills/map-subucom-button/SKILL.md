---
name: map-subucom-button
description: Find the sub-ucom frame bit + control code for a momentary XDJ-RX panel BUTTON (play, cue, sync, hot-cue, beat-fx, menu, back, load, source keys that are sub-ucom, etc.) so it can be injected from the virtual panel. Use when mapping a press/toggle control that the panel MCU reports as a bit in the 100-byte frame (not a GPIO, not a continuous fader/knob, not the rotary encoder).
---

# Mapping a sub-ucom BUTTON

A button is one **bit** in the 100-byte sub-ucom frame. On a bit change a decoder emits a
16-bit **control code** to a dispatcher. Goal: find `(byte_offset, bit)` and the code, then
inject it.

## Sub-ucom fundamentals (shared)
- ONE 100-byte frame over `/dev/subucom_spi1.0`; parsed by `PanelComController`
  (`FUN_0021a4ac`) and decoders at **~0x282000-0x28b000**.
- **Inject harness** (QEMU patch 02/07) reads `/tmp/subucom_ctrl` each frame and rewrites
  the named field + recomputes CRC. Spec lines (hex): `A <off> <val16>` = hold a 16-bit
  analog; `a` = one-shot; `B <off> <mask>` = hold a button bitmask; `b` = one-shot.
  Offsets are **rbp frame offsets** (same as the frame-map table).
- CRC = reflected CRC16 poly `0x8408`, init `0xffff`, final XOR, with a **per-32-bit-word
  byte reversal** before CRC. The harness handles it; you just name offset+value.
- Tools: `tools/xdj-panel/debug/inj.py` (gain-preserving one-shot/hold/mainkey),
  `mon.py` (HMP monitor, `screendump`), `ser.py`, `rsp.py`, `tables.py`, `relaunch.sh`.
- **Change-gated:** decoders fire on a bit *transition*. A stale hold blocks re-firing —
  `rm /tmp/subucom_ctrl` (or `inj.py pulse`, which preserves the gain holds) before a new press.

## Button-specific anchors
- **Button bytes:** center/nav/beat-fx/mixer-keys live in rbp `0x59-0x5f`; browse PUSH and
  USB STOP in `0x5d` (`0x5d.4`=PUSH code `0x4205`, `0x5d.0/1`=USB1/2 STOP). Deck buttons:
  **deck1 `0x10-0x13`, deck2 = deck1 + 0x10 (`0x20-0x23`)**.
- **Code dispatchers:** center/mixer codes (`0x2xx/0x44xx/0x50xx`) → **`FUN_0026ae7c`**;
  deck codes (`0x41xx/0x42xx`) → the deck handler (**NOT** `FUN_00222188` — a bp there does
  not fire; the real runtime handler is still to be pinned).
- Code→name table exists (debug naming); the config-switch names cluster at `0x42b3a4-0x42b454`
  (BeatEffectSW/BfxChSW/MicSW/FaderCurveSW/InputSourceSW/LinePhonoSW).

## Method
1. **Cross-reference RX3 first** (`docs/rx3-panel-crossref.md`, `docs/subucom-frame-map.md`)
   — many bits/codes are already known or match the RX3.
2. **Empirical screen-diff (fastest, no gdb):** boot, open the relevant UI, then for each
   candidate bit inject `b <off> <mask>` (one-shot) and `screendump` before/after. The UI
   reaction (highlight, mode change, "SYNC ON", etc.) identifies the control. Release
   between tries. Drive the needed source/USB keys via `/tmp/mainkey` (those are GPIO).
3. **Confirm the code (gdb):** set a breakpoint on `FUN_0026ae7c` (or the deck handler once
   found); inject the bit; read the 16-bit code argument. Soft-float/ARM ABI — the code is a
   core-reg integer. After any inferior work resume via the **monitor** `cont`.
4. **Full-byte sweep watching a downstream side-effect (when there's no obvious UI reaction).**
   Some "buttons" are really held config bits (e.g. the per-channel DECK/LINE **source switch**).
   Script a sweep over every frame byte: inject `B <off> ff` (or per-bit `B <off> <mask>`), then
   read a **downstream state** that the control changes, and flag any offset that moves it. Worked
   example (`tools/xdj-panel/debug/sweep_full.py`): sweeping all 100 bytes while reading the audio
   routing table found the source switch at **`rbp[0x33]` bit 7 = CH0 deck/line**, **`rbp[0x43]`
   bit 7 = CH1** (`1`=DECK, `0`=LINE; live-toggles). Space the RSP reads (don't hammer).
5. Record `(off, bit, code, control, confidence)` in `docs/subucom-frame-map.md`.

## Gotchas
- A bit may need a **loaded track** to show any effect (deck transport). Load first.
- Writing a *bare* one-shot to `/tmp/subucom_ctrl` replaces the analog-gain holds and
  zeroes the mix — use `inj.py pulse`.
- Don't hammer RSP (rbp watchdog → crash). Prefer the screen-diff oracle for buttons.

## References
`docs/subucom-frame-map.md`, `docs/rx3-panel-crossref.md`, `docs/rbp-and-panel-protocol.md`,
`docs/xdj-rx-control-map.md`. Sibling skills: [map-subucom-fader], [map-subucom-knob],
[map-subucom-rotary], [map-gpio-control].
