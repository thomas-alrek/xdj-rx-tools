---
name: map-subucom-button
description: Find the sub-ucom frame bit + control code for a momentary XDJ-RX panel BUTTON (play, cue, sync, hot-cue, beat-fx, menu, back, load, master rec, power, etc.) so it can be injected from the virtual panel. Use when mapping a press/toggle control that the panel MCU reports as a bit in the 100-byte frame (not a GPIO, not a continuous fader/knob, not the rotary encoder).
---

# Mapping a sub-ucom BUTTON

A button is one **bit** in the 100-byte sub-ucom frame. On a bit change a decoder emits a
16-bit **control code** to a dispatcher. Goal: find `(byte_offset, bit)` and the code, then
inject it.

## Sub-ucom fundamentals (shared)
- ONE 100-byte frame over `/dev/subucom_spi1.0`; parsed by `PanelComController`
  (`FUN_0021a4ac`) and decoders at **~0x282000-0x28b000**.
- **Inject harness** (`qemu/xdj/xdj-panel.c`) reads `/tmp/subucom_ctrl` (file mode) or the
  `xdjctrl` chardev (socket mode) each frame and rewrites the named field + recomputes CRC. Spec
  lines (hex): `A <off> <val16>` = hold a 16-bit analog; `a` = one-shot; `B <off> <mask>` = hold a
  button bitmask; `b` = one-shot. Offsets are **rbp frame offsets** (same as the frame-map table).
- CRC = CRC-16/X-25 (reflected poly `0x8408`, init `0xffff`, final XOR) with a **per-32-bit-word
  byte reversal** before CRC. The harness handles it; you just name offset+value.
- Tools: `tools/xdj-panel/debug/inj.py` (gain-preserving one-shot/hold/mainkey),
  `mon.py` (HMP monitor, `screendump`), `ser.py`, `rsp.py`, `tables.py`, `relaunch.sh`,
  `sweep_full.py`/`sweep_input.py`, `ledprobe.py`.
- **Change-gated:** decoders fire on a bit *transition*. A stale hold blocks re-firing —
  `rm /tmp/subucom_ctrl` (or `inj.py pulse`, which preserves the gain holds) before a new press.

## Button-specific anchors (`docs/subucom-frame-map.md` is authoritative)
- **Deck buttons:** deck1 `0x10-0x13`, deck2 = deck1 + 0x10 (`0x20-0x23`); codes `0x41xx/0x42xx/0x43xx`.
- **Centre buttons:** `0x59` (SOUND COLOR FX keys), `0x5a.0/.1` BEAT ►/◄, `0x5b.0-.3` (CUE CH2/CH1,
  BEAT FX ON, TAP), `0x5d` (.0 POWER, .4 BROWSE PUSH `0x4205`, .5/.6 LOAD, .7 TAG TRACK), `0x5e`
  (.0 BACK, .1 TRACK MARK, .2 MASTER REC `0x401`, .3 USB2 STOP, .4 USB1 STOP, .5 QUANTIZE), `0x5f`
  (.0 MENU, .1 INFO, .2 TAG LIST, .3 BROWSE).
- **POWER `0x5d.0` is active-low** (code `0x8001`); the harness inverts it, so `B 5d 01` = pressed.
  A press while rbp runs starts the standby handshake (rbp answers TX byte `0x11 = 'S'`;
  `docs/service-modes.md`).
- **Held, not tapped:** MENU must be held (~2 s) to open UTILITY; a short tap leaves it.
- **Multi-position switches** share bytes with buttons (`0x5a` CH/FX SELECT, `0x5b` XF CURVE / MIC,
  `0x33.7`/`0x43.7` INPUT SELECT) — those are field values, not presses (panel `S <off> <mask> <bits>`).
- **Code dispatchers:** center/mixer codes (`0x2xx/0x44xx/0x50xx`) → **`FUN_0026ae7c`**;
  deck codes (`0x41xx/0x42xx`) go to a separate deck handler (not `FUN_00222188` — a bp there does
  not fire; unpinned). Every code also passes the generic dispatcher `0x2a47c0` (`r1`=code,
  `r2`=state, `r3`=deck) — the cheapest place to confirm a mapping.
- Code→name: the debug-console vocabulary (`docs/debug-console-protocol.md`); config-switch names
  cluster at `0x42b3a4-0x42b454` (BeatEffectSW/BfxChSW/MicSW/FaderCurveSW/InputSourceSW/LinePhonoSW).

## Method
1. **Check the frame map first** (`docs/subucom-frame-map.md`) — most bits are mapped.
2. **Empirical screen-diff (fastest, no gdb):** boot, open the relevant UI, then for each
   candidate bit inject `b <off> <mask>` (one-shot) and `screendump` before/after. The UI
   reaction (highlight, mode change, "SYNC ON", etc.) identifies the control. Release
   between tries. Drive the source keys via `/tmp/mainkey` (those are GPIO).
3. **Confirm the code (gdb):** break on `0x2a47c0` (or `FUN_0026ae7c`); inject the bit; read the
   16-bit code argument (a core-reg integer). Step off each bp, keep halts sparse, and after any
   inferior work resume via the **monitor** `cont`.
4. **Full-byte sweep watching a downstream side-effect (when there's no obvious UI reaction).**
   Some "buttons" are really held config bits (e.g. the per-channel DECK/LINE INPUT SELECT).
   Script a sweep over every frame byte: inject `B <off> ff` (or per-bit `B <off> <mask>`), then
   read a **downstream state** the control changes, and flag any offset that moves it. Worked
   example (`tools/xdj-panel/debug/sweep_full.py`): sweeping all 100 bytes while reading the audio
   routing table found INPUT SELECT at **`0x33.7` (CH1)** / **`0x43.7` (CH2)** (`1`=DECK,
   `0`=LINE). Space the RSP reads (don't hammer).
5. Record `(off, bit, code, control)` in `docs/subucom-frame-map.md`.

## Test-mode oracle
Test **Mode 5 (KEY/LED TEST)** lights the LED belonging to each pressed key; Mode 6 (SW TEST)
shows switch positions. Entry: skill [run-xdj-emulator], "Service modes"; details
`docs/service-modes.md`.

## The button's LED (other direction)
Button LEDs are NOT in the RX frame: rbp drives them through the MOSI `SubTxData` payload
(`xdj-panel.c`). After mapping the press, find its LED with
`tools/xdj-panel/debug/ledprobe.py b_<off>_<mask>` (press twice: on + off), add
`led:[byte,bit,low]` to the panel entry, and record it in `docs/subucom-led-map.md`. rbp blinks
LEDs by toggling the bit (e.g. MASTER REC `0x0a.7` while recording).

## Gotchas
- A bit may need a **loaded track** to show any effect (deck transport). Load first.
- Writing a *bare* one-shot to `/tmp/subucom_ctrl` replaces the analog-gain holds and
  zeroes the mix — use `inj.py pulse`.
- A one-shot left in the file next to holds re-fires every ~30 frames — rewrite the holds alone
  right after (`inj.py pulse` does), or a toggle flips back and forth.
- Don't hammer RSP (rbp watchdog → crash). Prefer the screen-diff oracle for buttons.

## References
`docs/subucom-frame-map.md`, `docs/subucom-led-map.md`, `docs/rbp-and-panel-protocol.md`,
`docs/debug-console-protocol.md`, `docs/service-modes.md`. Sibling skills: [map-subucom-fader],
[map-subucom-knob], [map-subucom-rotary], [map-gpio-control], [xdj-virtual-panel].
