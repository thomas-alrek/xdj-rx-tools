# XDJ-RX sub-ucom frame map (panel MCU → rbp, `SubRxData`)

The authoritative map of every front-panel control the panel MCU ("sub-ucom") reports to the
main application `rbp`. The other direction (rbp → MCU: LEDs, level meters, jog ring) is the
`SubTxData` payload, mapped in [subucom-led-map.md](subucom-led-map.md).

Every entry in the map below was injected into a running rbp and the event it produced was
captured at the panel event dispatcher (`0x2a47c0`). The codes were named with rbp's own
debug-console vocabulary ([debug-console-protocol.md](debug-console-protocol.md)); switch
positions were read off the LCD. The virtual panel (`tools/xdj-panel/panel.html`, the `C`
object: each control's `spec`) drives exactly these offsets.

## Frame format

The XDJ-RX has **one** sub-ucom channel: a 100-byte full-duplex SPI burst on
`/dev/subucom_spi1.0` (ECSPI slave, bus 1 / CS 0). Its MISO half is this control frame.

| offset | content |
|---|---|
| `0x00–0x01` | not decoded (the emulator sends 0) |
| `0x02–0x03` | panel MCU firmware version word, LE. rbp keeps it from the first frame (`FUN_00282630` → `+0x94`) and shows it as "SUB Ver." (`%x.%02x`) in test mode; it rejects only `0xffff`. The emulator reports the version of the firmware in the emulated MCU flash (`$XDJ_PANELCOM_IMG`), 0 if none; `XDJ_SUBUCOM_VER=<hex>` overrides. |
| `0x04–0x5f` | controls (map below) |
| `0x62–0x63` | CRC, LE |

- **Offsets** throughout are rbp buffer offsets ("logical"). On the wire each 32-bit word is
  byte-reversed: wire byte `4w+j` = logical byte `4w+(3−j)`.
- **CRC** = CRC-16/X-25 (reflected poly `0x8408`, init `0xffff`, xorout `0xffff`) over the first
  **96** bytes after the per-word byte reversal, stored at `0x62`. rbp decodes a frame only when
  the CRC matches; a bad CRC drops the whole frame.
- **Buttons** are bits; an event fires when a bit **changes** against the previous frame.
- **Analog** controls are 16-bit LE values `0..1023` (normalised `/1023.0`); an event fires on
  change.
- **Encoders** are 16-bit LE counters the firmware diffs frame to frame (wrapping at 65535).

## How rbp decodes it

- `FUN_00284d44` parses the frame (CRC via `FUN_0028259c`, which reads and byte-reverses the
  words) and runs the section decoders in the cluster `0x282600–0x284d44`. Each decoder is an
  unrolled `ldrb [cur,#OFF]; ldrb [prev,#OFF]; eor; tst #MASK` sequence that emits a 16-bit code:
  - `FUN_00282800` — per-channel mixer analog, block base `0x24` (ch 1) / `0x34` (ch 2); codes
    `0x50xx` via `FUN_002adee8`.
  - `FUN_00282a98` — master analog block (base `0x44`) and the centre-section buttons (unrolled
    through `~0x282c00–0x283880`; the POWER bit is decoded at `0x283678`).
  - `FUN_00283dc4` — jog (code `0x4306`).
  - `FUN_002841e4` — deck buttons and deck analog, section base `0x04` (deck 1) / `0x14` (deck 2);
    codes `0x41xx/0x42xx/0x43xx`.
- Every event reaches the dispatcher at **`0x2a47c0`** as `(obj, code, state, deck, …)`, with
  `state` 0 = press, 2 = release, 3 = relative (encoder), 4 = absolute value (analog). Centre-section
  codes are consumed by `FUN_0026ae7c`. The debug console's `button`/`knob`/`rotary` commands
  (handlers `FUN_0001ec50`/`FUN_0001e69c`/`FUN_0001e49c`) dispatch the same codes.

## Control map

Notation: `0x12.5` = byte `0x12`, bit 5 (mask `0x20`). Codes in parentheses.

### Deck (deck 1 shown; deck 2 = +0x10)

| offset | control |
|---|---|
| `0x04` / `0x0a` | TEMPO fader, a complementary pair (`0x04` negative side, `0x0a` positive side; `0x0a` = 1023 − `0x04`). Send both together: forcing only one reads as a pegged fader. |
| `0x06` | VINYL SPEED ADJ TOUCH/BRAKE knob (`0x411d`). `0x08` is unused. |
| `0x0c` / `0x0e` / `0x10.0–.1` | JOG — see [Jog](#jog) |
| `0x10` | .4 pad D (`0x411a`) · .5 pad C (`0x4119`) · .6 pad B (`0x4118`) · .7 pad A (`0x4117`) |
| `0x11` | .0 SLIP (`0x4116`) · .1 LOOP SLICE (`0x4115`) · .2 BEAT LOOP (`0x4114`) · .3 HOT CUE (`0x4113`) · .4 SEARCH ►► (`0x411b`) · .5 SEARCH ◄◄ (`0x411c`) · .6 TRACK SEARCH ►►\| (`0x421b`) · .7 TRACK SEARCH \|◄◄ (`0x421c`) |
| `0x12` | .0 SHIFT (`0x4103`) · .1 REV (`0x4111`) · .2 MASTER TEMPO (`0x4109`) · .3 TEMPO RANGE (`0x4108`) · .4 MASTER (`0x4112`) · .5 SYNC (`0x410b`) · .6 VINYL (`0x4104`) · .7 RELOOP/EXIT (`0x4110`) |
| `0x13` | .0 LOOP OUT (`0x410f`) · .1 LOOP IN (`0x410d`) · .2 MEMORY (`0x4121`) · .3 DELETE (`0x4120`) · .4 CUE/LOOP CALL ► (`0x431e`) · .5 CUE/LOOP CALL ◄ (`0x431f`) · .6 PLAY/PAUSE (`0x4101`) · .7 CUE (`0x4102`) |

Deck 2: TEMPO `0x14`/`0x1a`, VINYL SPEED ADJ `0x16`, jog `0x1c`/`0x1e`/`0x20.0–.1`, buttons
`0x20–0x23`.

### Jog

| offset (deck 1) | field |
|---|---|
| `0x0c` | pulse period, 16-bit. Speed = 416.67 / period; 0 or `0xffff` = stopped. |
| `0x0e` | position counter, 16-bit; the firmware diffs it (wraps at 65535) |
| `0x10.0` | JOG TOUCH (`0x430e`) — platter top touched |
| `0x10.1` | direction: 1 = reverse |

`FUN_00283dc4` turns these into code `0x4306`. Deck 2 = +0x10. The virtual panel (`POST /jog` in
`server.py`) models 1024 counter ticks per platter turn, scales the period so 33⅓ rpm = speed 1.0,
and drops the period to 0 shortly after the platter stops.

### Mixer (channel 1 shown; channel 2 = +0x10)

| offset | control |
|---|---|
| `0x24` | EQ HI (`0x5008`) |
| `0x26` | TRIM (`0x5007`) |
| `0x28` | EQ LOW (`0x500a`) |
| `0x2a` | EQ MID (`0x5009`) |
| `0x2c` | CHANNEL FADER (`0x5011`) → `setChannelFader` (`FUN_00083fcc`) |
| `0x2e` | COLOR knob (`0x508b`) |
| `0x33.7` | INPUT SELECT, 1 = DECK (`0x5112`) — see [Input select](#input-select-deck--linephono) |

Channel 2: `0x34` HI · `0x36` TRIM · `0x38` LOW · `0x3a` MID · `0x3c` FADER · `0x3e` COLOR ·
`0x43.7` INPUT SELECT.

### Master / centre section

| offset | control |
|---|---|
| `0x44` / `0x46` | MIC EQ LOW (`0x805`) / MIC EQ HI (`0x804`) |
| `0x48` | BOOTH MONITOR level (`0x440c`) |
| `0x4a` | MASTER LEVEL (`0x4405`) → `setMasterVolume` (`FUN_00066bb8`) |
| `0x4c` | BEAT FX LEVEL/DEPTH (`0x448b`) |
| `0x4e` | CROSSFADER (`0x600f`) |
| `0x50` | HEADPHONES LEVEL (`0x4403`) |
| `0x52` | HEADPHONES MIXING (`0x4402`) |
| `0x54` | BROWSE encoder rotate — see [Encoders](#encoders) |
| `0x56` | BEAT FX TIME encoder (`0x448a`) — see [Encoders](#encoders) |
| `0x59` | .4 FILTER (`0x5095`) · .5 CRUSH (`0x5096`) · .6 GATE (`0x5097`) · .7 NOISE (`0x5098`) — SOUND COLOR FX keys |
| `0x5a` | .0 BEAT ► (`0x4494`) · .1 BEAT ◄ (`0x4493`) · bits 2–4 CH SELECT · bits 5–7 FX SELECT — see [Switches](#switches) |
| `0x5b` | .0 HEADPHONES CUE CH2 · .1 CUE CH1 (`0x5014`) · .2 BEAT FX ON/OFF (`0x4487`) · .3 TAP (`0x4486`) · bits 4–5 CROSSFADER CURVE · bits 6–7 MIC switch |
| `0x5d` | .0 POWER (`0x8001`, active-low) · .4 BROWSE PUSH (`0x4205`) · .5 LOAD to deck 2 (`0x420d`) · .6 LOAD to deck 1 (`0x420d`) · .7 TAG TRACK (`0x420b`) |
| `0x5e` | .0 BACK (`0x4206`) · .1 TRACK MARK (`0x409`) · .2 MASTER REC (`0x401`) · .3 USB2 STOP (`0x8002`) · .4 USB1 STOP (`0x8002`; the slot is the event's parameter) · .5 QUANTIZE (`0x4488`) |
| `0x5f` | .0 MENU (`0x20a`) · .1 INFO (`0x204`) · .2 TAG LIST (`0x209`) · .3 BROWSE (`0x208`) |

### Switches

Multi-position switches are bit fields sharing a byte with buttons; send the field value, not a
press. The virtual panel uses `S <off> <mask> <bits>` (see [Injection](#injection-syntax)).

| field | positions (field value → position) | code |
|---|---|---|
| `0x5a` bits 2–4, mask `0x1c` | v → position 5 − v: 1, 2, MIC, CF.1, CF.2, MASTER — BEAT FX CH SELECT | `0x448e` |
| `0x5a` bits 5–7, mask `0xe0` | v → position 7 − v: DELAY, ECHO, SPIRAL, REVERB, TRANS, FLANGER, PITCH, ROLL — BEAT FX SELECT | `0x448d` |
| `0x5b` bits 4–5, mask `0x30` | v=1 → curve 1, v=0 → curve 2, v=2 → curve 3 — CROSSFADER CURVE | `0x6010` |
| `0x5b` bits 6–7, mask `0xc0` | v=1 → OFF, v=0 → ON, v=2 → TALK OVER — MIC switch | `0x803` |
| `0x33.7` / `0x43.7` | 1 = DECK, 0 = LINE/PHONO — INPUT SELECT CH1 / CH2 | `0x5112` |

### Encoders

- **BROWSE rotate** = 16-bit counter at `0x54/0x55` (counting up scrolls down). Once the counter
  has been sent it must be present in every frame: dropping it reads as a large rotate delta.
  **BROWSE push** = `0x5d.4` (`0x4205`).
- **BEAT FX TIME** = 16-bit counter at `0x56/0x57` (`0x448a`, relative).

### POWER key (`0x5d.0`, code `0x8001`)

The POWER key is **active-low** on the real panel MCU: the bit idles at 1 and reads 0 while
pressed (decoder `0x283678`: bit clear = press, set = release). A press starts rbp's **standby
handshake**: `ui::PowerManager` prepares for standby, then every TX frame carries the LEDs off and
payload byte `0x11 = 0x53` ('S'), telling the MCU to cut power. The same path serves AUTO STANDBY.
Details: [service-modes.md](service-modes.md).

`qemu/xdj/xdj-panel.c` inverts this bit so injection names presses like every other button: no
hold → 1 (idle), `B 5d 01` → 0 (pressed). The virtual panel's POWER off presses the key while rbp
runs and cuts power when rbp answers 'S' (or after 10 s with no answer).

### MASTER REC (`0x5e.2`, code `0x401`)

A tap starts recording the master output to the stick in **USB2** (with the stick in USB1 a tap
does nothing); a second tap stops it. The file is a 16-bit stereo 44.1 kHz WAV,
`/PIONEER REC/REC%03d.WAV`. There is no on-screen indicator; the MASTER REC LED (`SubTxData`
`0x0a.7`) blinks while recording. In rbp the recording starts in `RecStart` (`0x5a388`); the
toggle is `0x276fec`, vtable slot 3 of `ui::MasterInnards` (vtable `0x426e08`).

### MENU (`0x5f.0`, code `0x20a`)

In the browser a tap is SORT. To open the **UTILITY** menu, **hold MENU for about 1 s or more**;
a short tap leaves UTILITY again. A short tap on the home screen does nothing.

## Input select (DECK ↔ LINE/PHONO)

The per-channel source switch (firmware `InputSourceSW`) is a frame bit and re-routes live on
change:

| bit | channel | 1 = DECK | 0 = LINE/PHONO |
|---|---|---|---|
| `0x33.7` | CH1 | ch0 → src 0 | ch0 → src 3 |
| `0x43.7` | CH2 | ch1 → src 1 | ch1 → src 4 |

The virtual panel holds both bits by default (DECK); `POST /input` toggles them. With the bits
clear both channels play the (silent) external inputs. The separate rear **LINE/PHONO** selector
(EXT-TYPE) is a GPIO, not this bit — see below.

Data path, for reading routing live (`tools/xdj-panel/debug/tables.py`):
- Source table at `0x107ac88`, 7 slots; channel table at `0x107acdc` (channel 1 = `0x107ace8`),
  6 slots. Both have a 12-byte stride `{buf_ptr, src_idx, flag}`. Deck 1's player renders into
  `src[0]`, deck 2's into `src[1]`; slots 3/4 are the external inputs.
- Router `FUN_00080a74(src_idx, chan_idx)` copies a source slot into a channel slot.
  `FUN_000809ec(src_idx, buf)` registers a source's live buffer and propagates it to channels
  routed to it. Init `FUN_00080920` clears the buffer pointers and leaves every channel routed to an empty slot.
- Channel object: `+0x254` = requested source, `+0x250` = applied source. The per-frame apply
  `FUN_00085a10` calls the router when they differ and latches `+0x250`. The INPUT SELECT bit
  reaches `+0x254` through a C++ virtual call.

Audio also needs the gains up (channel fader `0x2c`, MASTER `0x4a`, crossfader `0x4e`).
`xdj-panel.c` asserts those defaults for every control the operator is not driving.

## Controls that are not in this frame

**MAIN-UCOM keys** are i.MX6 GPIO inputs rbp reads through `/dev/gpiodrv` (idle HIGH, pressed
LOW), not sub-ucom bits:

| key | GPIO |
|---|---|
| rekordbox | 159 |
| MIDI | 161 |
| USB1 | 160 |
| USB2 | 158 |
| DECK 1/2 | 149 |
| TIME MODE | 146 |
| rear LINE/PHONO (EXT-TYPE) switch | 119 / 120 (HIGH = PHONO) |

Inject one with `echo <gpio> > /tmp/mainkey` (empty or removed = release), or over the `xdjkey`
chardev in socket mode. LINE/PHONO is not emulated (no external audio). The rekordbox/MIDI/USB1/USB2
key LEDs are rbp GPIO outputs 162–165 (see [subucom-led-map.md](subucom-led-map.md)). How to map
a GPIO control: skill `map-gpio-control`.

**Mic:** the MIC 1 / MIC 2 LEVEL knobs are not in this frame, and the virtual panel leaves them
unwired. MIC EQ HI/LOW (`0x46`/`0x44`) and the MIC switch (`0x5b` bits 6–7) are in the frame and
wired. The emulator has no audio inputs, so none of them change what is heard.

## Injection syntax

`qemu/xdj/xdj-panel.c` builds each frame from a snapshot of lines, read from the `xdjctrl`
chardev (socket mode, the app) or from `XDJ_CTRL_PATH` (default `/tmp/subucom_ctrl`, file mode).
Offsets and values are hex.

| line | meaning |
|---|---|
| `B <off> <mask>` | hold button bit(s) down while the line is present |
| `A <off> <val16>` | hold a 16-bit LE analog/counter value |
| `b <off> <mask>` | one-shot press, then release (~30 frames) |
| `a <off> <val16>` | one-shot analog value, then release |
| `S <off> <mask> <bits>` | switch field (virtual-panel server only; it folds the field into the `B` lines it writes) |

- A snapshot is the whole truth: anything left out returns to 0 (released) in the next frame.
- In file mode, a one-shot left in the file next to holds re-fires every 30 frames; rewrite the
  holds alone right after (`tools/xdj-panel/debug/inj.py pulse`).
- Examples: `B 13 40` press PLAY on deck 1; `A 2c 3ff` channel 1 fader up; `B 33 80` + `B 43 80`
  both channels on DECK; `printf 'B 13 04\nB 12 08\n'` before boot = test mode
  ([service-modes.md](service-modes.md)).

How to map a new control: skills `map-subucom-button`, `map-subucom-fader`, `map-subucom-knob`,
`map-subucom-rotary`.

## Related

- [subucom-led-map.md](subucom-led-map.md) — the rbp → MCU payload (LEDs, meters, jog ring)
- [rbp-and-panel-protocol.md](rbp-and-panel-protocol.md) — the SPI transport and rbp's panel classes
- [debug-console-protocol.md](debug-console-protocol.md) — rbp's UDP debug console and its code tables
- [service-modes.md](service-modes.md) — test mode, update mode, the standby handshake
- [usb.md](usb.md) — mounting USB sticks (needed for MASTER REC and track loads)
- [emulator-status.md](emulator-status.md) — feature board
