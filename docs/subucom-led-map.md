# XDJ-RX sub-ucom LED map (rbp → panel MCU, `SubTxData`)

The sub-ucom SPI link is full-duplex. MISO carries the panel MCU's control frame
(`SubRxData`, see `subucom-frame-map.md`). **MOSI carries rbp's `SubTxData`: the
state of every panel LED, the level meters and the jog-ring illumination.** The MCU
just lights what it is told, and rbp produces blinking by toggling the bit itself.
To light an LED, mirror the bit.

`xdj-panel.c` (was patch 15)** captures this frame and publishes it (below). The web panel lights its
buttons, meters and jog rings from it.

## Transport (verified live 2026-11-15)
- Same conventions as RX: each 32-bit word is byte-reversed on the wire. "Logical" offsets
  below are after un-swapping, i.e. in rbp's buffer order.
- **Payload = 20 bytes (0x00–0x13).** **CRC-16/X-25** over the word-reversed payload, stored LE
  at **0x16**. This is the same CRC and swap as RX, but over 20 bytes instead of 96. 0x14/0x15 are 0.
  The rest of the 100-byte burst is stale buffer memory (it contains rbp pointers), so ignore it.
  The 24-byte `SubTxData` object (`PanelComController` ctor `FUN_0021a4ac`, `memset(+0x6c,0,0x18)`)
  is this payload plus the CRC.
- rbp sends the frame from early boot; at idle it is constant. While a deck plays it changes about
  20×/s, because of the meters and the jog ring.
- `xdj-panel.c` (was patch 15) publishes each change as one line `T <40 hex>\n`. It goes up the `xdjctrl` chardev in
  socket mode (the app), or atomically into `XDJ_TX_PATH` (default `/tmp/subucom_tx`) in file mode.
  Frames whose CRC doesn't match are dropped. `XDJ_TX_LOG=1` logs every changed burst to stderr
  as `SUBUCOM-TX <len> <hex>`.
- `tools/xdj-panel/server.py` re-serves it at **`GET /leds`** (SSE), and `/leds?once=1` returns JSON.

## Map
Method: each control was pressed once through the inject harness, and the payload was diffed
in steady state (`tools/xdj-panel/debug/ledprobe.py`), using a loaded, playing deck from a real
rekordbox USB.
**Deck 2 = deck 1 + 4 bytes** (verified: SYNC 05.6, REV 07.0, MT 07.3, HOT CUE 06.2, pads 06.4-7).
- ✅ = state change observed.
- ◆ = inferred from correlation.
- `low` = active-low: 1 = off, and a blinking bit means the LED is blinking on.

| byte.bit | LED | notes |
|---|---|---|
| `0x00` | **Jog ring** (deck 1) | ✅ `1..36` = lit position; it advances while playing and holds when paused. `0x00` = whole ring (it flashes with `0x63` on track load). `0x63` = off (no track). `0x3f` was seen with SLIP on, meaning not yet decoded. The orientation of position 1 is unverified. |
| `0x01.5` | MASTER (tempo master) | ◆ set when the deck became master on load; the LCD shows MASTER |
| `0x01.6` | SYNC | ✅ |
| `0x01.7` | SLIP | ✅ |
| `0x01.0` | ? | follows PLAY/VINYL state; not identified |
| `0x02.0` | LOOP SLICE (pad mode) | ✅ the pad modes are mutually exclusive |
| `0x02.1` | BEAT LOOP (pad mode) | ✅ |
| `0x02.2` | HOT CUE (pad mode) | ✅ on at idle |
| `0x02.3` | VINYL | ✅ |
| `0x02.7/.6/.5/.4` | PAD A / B / C / D | ✅ in HOT CUE mode it is lit when that hot cue exists (SHIFT+pad delete cleared A → .7, C → .5). rbp blinks some of them. |
| `0x03.0` | REV | ✅ |
| `0x03.1` | PLAY/PAUSE | ✅ steady = playing, blinking = paused |
| `0x03.2` | CUE | ✅ steady at the cue point, blinking otherwise while paused |
| `0x03.3` | MASTER TEMPO | ✅ |
| `0x03.4` | RELOOP/EXIT | ✅ on while a loop is stored |
| `0x03.5` | LOOP OUT (low) | ✅ blinks while looping |
| `0x03.6` | LOOP IN (low) | ✅ blinks after IN is set and while looping |
| `0x03.7` | ? | blinks in some paused states; not identified |
| `0x04–0x07` | deck 2 | same layout as `0x00–0x03` |
| `0x08–0x09` | **MASTER level meter** | ✅ 16-bit LE, follows MASTER level + crossfader |
| `0x0a.4` | CUE CH2 (headphone cue) | ✅ |
| `0x0a.5` | CUE CH1 | ✅ |
| `0x0a.6` | ? | constant 1 so far |
| `0x0c–0x0d` | **CH2 level meter** | ✅ pre-fader |
| `0x0e–0x0f` | **CH1 level meter** | ✅ pre-fader: follows TRIM, ignores the channel fader |
| `0x10–0x11` | ? | always 0 so far |
| `0x12` | ? | constant `0x08` |
| `0x13.0` | FILTER (low) | ✅ sound-colour FX are mutually exclusive; the LED blinks while on |
| `0x13.1` | CRUSH (low) | ✅ |
| `0x13.2` | GATE (low) | ✅ |
| `0x13.3` | NOISE (low) | ✅ |
| `0x13.6` | BEAT FX ON/OFF (low) | ✅ blinks while on (~0.62 s period, measured) |
| `0x13.4/.5/.7` | ? | toggle at times; candidates are BEAT ◄/► and TAP. Not identified. |

**Meters:** bit *i* of the 16-bit field is segment *i*, counted bottom-up from 0. That gives a
12-segment bar graph (bits 0–11) with a peak-hold dot; for example `0x5ff` = segments 0–8 plus the
hold at 10.

**No LED change observed for:** TEMPO RANGE, SHIFT, QUANTIZE and TAP. The display keys
(rekordbox / MIDI / USB1 / USB2 / DECK 1/2 / TIME MODE) are GPIO keys and are not in this frame.

### Source-key LEDs (GPIO, not SubTxData) — verified live 2026-11-15
The rekordbox / MIDI / USB1 / USB2 key that is the active source glows. rbp drives these four LEDs
as **GPIO outputs 162–165** (GPIO6_IO02..05, `PanelComController` `FUN_0021a4ac`), **active-high,
one-hot**: rekordbox = **162**, MIDI = **163**, USB1 = **164**, USB2 = **165**. Found by reading
GPIO6 `DR` (`0x020B0000`; `GDIR` = `0x3c`) over the monitor while pressing each key.
`xdj-board.c` (was patch 17)** mirrors them and appends one byte to the LED line: `T <40 hex> <2 hex>` (bit0
rekordbox … bit3 USB2); `server.py` passes 42 hex through and the panel reads it as **byte `0x14`**.
DECK 1/2 and TIME MODE showed no GPIO-output change (not checked further).

## Open items
- Identify `0x01.0`, `0x03.7`, `0x0a.6`, `0x12`, and `0x13.4/.5/.7`.
- Test **Mode 5** (KEY/LED TEST) is the oracle for the rest: the service manual names LEDs not
  mapped yet — USB1/2 STOP, MASTER REC, BROWSE, TAGLIST, INFO, MENU, TAP, LOAD1/2 — candidates for
  the unknown bits (`docs/service-modes.md`).
- `0x0b.7` went on after the first USB1 press with a stick inserted (2026-11-15) — candidate USB
  indicator; not yet characterised.
- Decode the jog ring's non-position values (`0x3f`, `0x63`) and check its orientation.
- Statically locate the encoder that writes the payload, for names. rbp's `PcControlLedData`
  string table lists Play/Pause, Vinyl, Sync, MasterTempo, LoopIn, LoopOut, Reloop, Reverse, Slip,
  Load, HotCue, AutoBeatLoop, LoopSplit, Pad1–4, JogCenter, JogLoadIllumi, JogSlipInvert,
  CfxFilter/Crush/Gate/Noise, EffectOnOff, HeadphoneCue and LvMeter. These are the MIDI-LED names,
  and they match the set found here.

## Panel rendering choices (not firmware facts)
- COLOR FX and BEAT FX ON blink at ~0.31 s on / 0.31 s off while active. The web panel shows them
  as a **steady** glow instead (blue / yellow), holding the light until the bit has stayed off for
  550 ms (`steady:true`, `STEADY_HOLD_MS` in `panel.html`).
- The three level meters are drawn as one centred LEVEL bank (CH1 | MASTER | CH2).
