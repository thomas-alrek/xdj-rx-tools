# XDJ-RX sub-ucom LED map (rbp → panel MCU, `SubTxData`)

The sub-ucom SPI link is full-duplex. MISO carries the panel MCU's control frame (`SubRxData`,
[subucom-frame-map.md](subucom-frame-map.md)). **MOSI carries rbp's `SubTxData`: the state of
every panel LED, the level meters and the jog-ring illumination.** The MCU lights what it is told;
rbp produces blinking by toggling the bit itself. To light an LED, mirror the bit.

`qemu/xdj/xdj-panel.c` captures this payload and publishes it; the virtual panel
(`tools/xdj-panel/panel.html`, each control's `led`) lights its buttons, meters and jog rings
from it.

## Payload and transport

- Same conventions as the control frame: each 32-bit word is byte-reversed on the wire. Offsets
  below are logical (rbp's buffer order).
- **Payload = 20 bytes (`0x00–0x13`).** **CRC-16/X-25** (poly `0x8408`, same table as the control
  frame) over the word-reversed payload, stored LE at **`0x16`**. `0x14–0x15` are 0. The rest of
  the 100-byte burst is stale buffer memory (it contains rbp pointers); ignore it. The 24-byte
  `SubTxData` object (`PanelComController` ctor `FUN_0021a4ac`, `memset(+0x6c, 0, 0x18)`) is this
  payload plus the CRC.
- rbp sends the payload from early boot. At idle it is constant; while a deck plays it changes
  about 20×/s (meters, jog ring).
- `xdj-panel.c` publishes each change as one line `T <40 hex>` — up the `xdjctrl` chardev in socket
  mode (the app), or atomically into `XDJ_TX_PATH` (default `/tmp/subucom_tx`) in file mode.
  `qemu/xdj/xdj-board.c` appends the source-key LED byte, giving `T <40 hex> <2 hex>`
  ([below](#source-key-leds-byte-0x14)). Payloads whose CRC does not match are dropped.
  `XDJ_TX_LOG=1` logs every changed burst to stderr as `SUBUCOM-TX <len> <hex>`.
- `tools/xdj-panel/server.py` re-serves it at **`GET /leds`** (SSE); `/leds?once=1` returns JSON.

## Map

**Deck 2 = deck 1 + 4 bytes** (`0x04–0x07` mirror `0x00–0x03`). `low` = active-low: 1 = off, and
the LED blinks on while the bit toggles.

| byte.bit | LED | notes |
|---|---|---|
| `0x00` | **Jog ring**, deck 1 | `1..36` = lit position; advances while playing, holds when paused. `0x00` = whole ring (flashes with `0x63` on track load). `0x63` = off (no track). See [Jog ring](#jog-ring). |
| `0x01.0` | ? | follows PLAY/VINYL state; not identified |
| `0x01.5` | MASTER (tempo master) | inferred: set when the deck became master on load, with MASTER on the LCD |
| `0x01.6` | SYNC | |
| `0x01.7` | SLIP | |
| `0x02.0` | LOOP SLICE (pad mode) | the three pad-mode LEDs are mutually exclusive |
| `0x02.1` | BEAT LOOP (pad mode) | |
| `0x02.2` | HOT CUE (pad mode) | on at idle |
| `0x02.3` | VINYL | |
| `0x02.4` / `.5` / `.6` / `.7` | PAD D / C / B / A | in HOT CUE mode, lit when that hot cue exists; rbp blinks some of them |
| `0x03.0` | REV | |
| `0x03.1` | PLAY/PAUSE | steady = playing, blinking = paused |
| `0x03.2` | CUE | steady at the cue point, blinking otherwise while paused |
| `0x03.3` | MASTER TEMPO | |
| `0x03.4` | RELOOP/EXIT | on while a loop is stored |
| `0x03.5` | LOOP OUT (low) | blinks while looping |
| `0x03.6` | LOOP IN (low) | blinks after IN is set and while looping |
| `0x03.7` | USB1 STOP | lit by USB1 STOP in test Mode 5; in normal use seen blinking in some paused states |
| `0x04–0x06`, `0x07.0–.6` | deck 2 | same layout as `0x00–0x03` |
| `0x07.7` | USB2 STOP | |
| `0x08–0x09` | **MASTER level meter** | 16-bit LE; follows MASTER level and the crossfader |
| `0x0a.4` | HEADPHONES CUE CH2 | |
| `0x0a.5` | HEADPHONES CUE CH1 | |
| `0x0a.6` | ? | on in normal operation, off in Mode 5: a status indicator, not a key LED |
| `0x0a.7` | MASTER REC | blinks while recording to USB2 (~0.5 s on / 0.5 s off), off when stopped |
| `0x0b.4` | MENU | |
| `0x0b.5` | INFO | |
| `0x0b.6` | TAG LIST | |
| `0x0b.7` | BROWSE | lit while the browser is up |
| `0x0c–0x0d` | **CH2 level meter** | pre-fader |
| `0x0e–0x0f` | **CH1 level meter** | pre-fader: follows TRIM, ignores the channel fader |
| `0x10` | — | always 0 so far |
| `0x11` | standby command | 0 normally; `0x53` ('S') once rbp is ready for standby, with every LED off — see [service-modes.md](service-modes.md) |
| `0x12` | ? | constant `0x08`, even with every LED off: not an LED |
| `0x13.0` | FILTER (low) | SOUND COLOR FX LEDs are mutually exclusive and blink while on |
| `0x13.1` | CRUSH (low) | |
| `0x13.2` | GATE (low) | |
| `0x13.3` | NOISE (low) | |
| `0x13.4` | LOAD (deck 2) | |
| `0x13.5` | LOAD (deck 1) | |
| `0x13.6` | BEAT FX ON/OFF (low) | blinks while on (~0.62 s period) |
| `0x13.7` | TAP | |

In normal operation no LED change was seen for TEMPO RANGE, SHIFT, QUANTIZE or TAP. TRACK MARK,
BACK, TAG TRACK, BEAT ◄/►, browse PUSH, SEARCH and the other keys without a LED of their own
have none in this payload.

### Level meters

Bit *i* of the 16-bit field is segment *i*, counted bottom-up from 0: a 12-segment bar graph
(bits 0–11) with a peak-hold dot. Example: `0x5ff` = segments 0–8 plus the hold at 10.

### Jog ring

Byte `0x00` (deck 1) / `0x04` (deck 2): `1..36` = the lit position, `0x00` = whole ring,
`0x63` = off. `0x3f` was seen with SLIP on and is not decoded; the orientation of position 1 is
unverified. The virtual panel draws positions as 10° steps.

### Source-key LEDs (byte `0x14`)

The rekordbox / MIDI / USB1 / USB2 key of the active source glows. These four LEDs are **not** in
`SubTxData`: rbp drives them as **GPIO outputs 162–165** (GPIO6_IO02..05, set up in
`PanelComController` `FUN_0021a4ac`), **active-high, one-hot**: rekordbox = 162, MIDI = 163,
USB1 = 164, USB2 = 165 (GPIO6 `DR` at `0x020B0000`, `GDIR` = `0x3c`). `qemu/xdj/xdj-board.c`
mirrors them and appends one byte to the published line (bit 0 rekordbox … bit 3 USB2);
`server.py` passes the 42 hex digits through and the panel reads it as **byte `0x14`**. DECK 1/2
and TIME MODE showed no GPIO-output change.

## How to re-check (test Mode 5)

Test mode's Mode 5 (KEY/LED TEST) lights each key's own LED, so it is an LED oracle
([service-modes.md](service-modes.md)):

1. Boot test mode (`POST /power on test`) and turn the browse encoder 4 detents to Mode 5. Browse
   PUSH changes the test mode, so test it last.
2. Hold each frame button bit / top-panel GPIO key for 0.6 s (via `/ctrl` or `/mainkey`) and
   sample `/leds?once=1` four times at 150 ms; record the bits that change.

In Mode 5 a key without a LED lights a neighbour's: TRACK MARK → `0x0a.7`, BEAT ► → `0x0b.4`,
BEAT ◄ → `0x0b.5`, TAG TRACK → `0x0b.6`, BACK → `0x0b.7`, browse PUSH → `0x13.7`, SEARCH ►► →
pad D `0x02.4`, QUANTIZE → `0x14.0`, DECK 1/2 → `0x14.1`, TIME MODE → `0x14.2`. Nothing lights
`0x0a.6`, `0x01.0` or `0x12`.

For normal-mode behaviour (blinking, mode LEDs) press each control on a loaded, playing deck and
diff the payload in steady state with `tools/xdj-panel/debug/ledprobe.py`.

## Open items

- `0x0a.6`: which status indicator (on in normal operation, off in Mode 5)?
- `0x01.0`: follows PLAY/VINYL state; not identified.
- `0x12`: constant `0x08`; meaning unknown.
- Jog ring: decode `0x3f` (seen with SLIP on) and check the orientation of position 1.
- Locate the encoder in rbp that writes the payload, for names. rbp's `PcControlLedData` string
  table (the MIDI-LED names) lists Play/Pause, Vinyl, Sync, MasterTempo, LoopIn, LoopOut, Reloop,
  Reverse, Slip, Load, HotCue, AutoBeatLoop, LoopSplit, Pad1–4, JogCenter, JogLoadIllumi,
  JogSlipInvert, CfxFilter/Crush/Gate/Noise, EffectOnOff, HeadphoneCue and LvMeter, matching the
  set found here.

## Virtual panel rendering (not firmware facts)

- COLOR FX and BEAT FX ON blink at ~0.31 s on / 0.31 s off while active. The panel shows them as a
  steady glow (blue / yellow), holding the light until the bit has stayed off for 550 ms
  (`steady:true`, `STEADY_HOLD_MS` in `panel.html`).
- MASTER REC is a red dot on the button and mirrors the blink.
- The three level meters are drawn as one centred LEVEL bank (CH1 | MASTER | CH2).
