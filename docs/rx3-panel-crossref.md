# XDJ-RX3 → XDJ-RX panel cross-reference (hypotheses to verify on the RX)

Source: the **XDJ-RX3** emulator `github.com/nzoschke/xdj-emu`
(`docs/xdj-rx3-panel.md`, `guest/rx3/panel_frames.py`, `crates/cdj3k-emu-panel/src/rx3.rs`,
`app/cdj3k-emu/src/rx3/hardware.rs`). Everything below is **RX3 firmware 1.20**, NOT the RX.
Use it as a **hypothesis list**: the frame-byte layout partly transfers to our XDJ-RX, but
**every entry must be verified on the live RX** (inject `b <off> <bitmask>` / `A <off> <v16>`
and watch the screendump). Key IDs differ between models and are only for reference.

## Transfer evidence so far (RX, live)
| Control | RX3 offset | RX (verified) | Match |
|---|---|---|---|
| Browse encoder PUSH | byte 93 (`0x5d`) bit4 | `0x5d` bit4 (code 0x4205 type0) | ✅ |
| Browse encoder ROTATE | 16-bit counter @ 84–85 (`0x54/0x55`) | `0x54/0x55` counter, inc=down dec=up | ✅ |
| BACK | byte 94 (`0x5e`) bit0 | `0x5e` bit0 → navigated up a level | ✅ |
| USB1/USB2 STOP | byte 94 (`0x5e`) bit4/bit3 | `0x5d` bit0/bit1 (verified by eject) | ❌ differs |

**Conclusion:** the browse/nav cluster (~`0x54`–`0x5e`) largely matches; USB STOP is one known
exception. Treat the table as strong leads, confirm each. Inject bit N as `b <off> <2^N>`
(e.g. `0x5e` bit0 = `b 5e 01`, `0x5c` bit2 = `b 5c 04`, `0x5d` bit6 = `b 5d 40`).

## Global / nav keys — EUP "deck 0" (RX3 [CODE], author-chosen, UNVERIFIED on RX)
| Control | RX3 byte.bit | Inject | RX status |
|---|---|---|---|
| SOURCE | `0x5c`.7 | `b 5c 80` | untested |
| BROWSE (menu btn, not encoder) | `0x5c`.6 | `b 5c 40` | untested |
| TAG LIST | `0x5c`.5 | `b 5c 20` | untested |
| PLAYLIST | `0x5c`.4 | `b 5c 10` | untested |
| SEARCH | `0x5c`.3 | `b 5c 08` | untested |
| MENU | `0x5c`.2 | `b 5c 04` | tried once, no change in device-selector ctx (retest in a list) |
| BACK | `0x5e`.0 | `b 5e 01` | ✅ CONFIRMED (up one level) |
| TAG TRACK | `0x5d`.7 | `b 5d 80` | untested |
| SHORT CUT | `0x5d`.2 | `b 5d 04` | untested |
| TRACK FILTER | `0x5d`.3 | `b 5d 08` | untested |
| Browse PUSH | `0x5d`.4 | `b 5d 10` | ✅ CONFIRMED |
| LOAD 1 / LOAD 2 | `0x5d`.6 / `0x5d`.5 | `b 5d 40` / `b 5d 20` | untested (needs a track selected to see effect) |
| BEAT FX on/off | `0x5b`.2 | `b 5b 04` | untested |
| BEAT FX ◀ / ▶ | `0x5a`.1 / `0x5a`.0 | `b 5a 02` / `b 5a 01` | untested |
| BEAT FX TAP | `0x5b`.3 | `b 5b 08` | untested |
| Master CUE | `0x58`.7 | `b 58 80` | untested |
| Mixer CUE d1/d2 | `0x5b`.1 / `0x5b`.0 | `b 5b 02` / `b 5b 01` | untested |

## Per-deck EUP keys (RX3 [FW], firmware-decoded; deck1 base 4, deck2 base 20 = +16)
Absolute bytes shown. Deck2 = deck1 byte + 16.
| Control | Deck1 byte.bit | Deck2 byte.bit |
|---|---|---|
| Jog touch | `0x10`.0 | `0x20`.0 |
| Slip | `0x11`.0 | `0x21`.0 |
| VINYL SPEED ADJ (unnamed in doc) | `0x11`.2 | `0x21`.2 |
| QUANTIZE (RX3 [LIVE]) | `0x11`.3 | `0x21`.3 |
| Search fwd / rev | `0x11`.4 / `0x11`.5 | `0x21`.4 / `0x21`.5 |
| Track fwd / rev | `0x11`.6 / `0x11`.7 | `0x21`.6 / `0x21`.7 |
| SHIFT | `0x12`.0 | `0x22`.0 |
| REV (reverse) | `0x12`.1 | `0x22`.1 |
| Master Tempo | `0x12`.2 | `0x22`.2 |
| Tempo range | `0x12`.3 | `0x22`.3 |
| Master | `0x12`.4 | `0x22`.4 |
| Sync | `0x12`.5 | `0x22`.5 |
| Vinyl | `0x12`.6 | `0x22`.6 |
| Reloop | `0x12`.7 | `0x22`.7 |
| Loop Out / Loop In | `0x13`.0 / `0x13`.1 | `0x23`.0 / `0x23`.1 |
| CUE/LOOP call ▶ / ◀ | `0x13`.2 / `0x13`.3 | `0x23`.2 / `0x23`.3 |
| DELETE / MEMORY | `0x13`.4 / `0x13`.5 | `0x23`.4 / `0x23`.5 |

## Per-deck SUB keys (RX3 [FW]; SUB channel 104-byte frame; deck1 base 4, deck2 base 8 = +4)
NB our RX harness/doc has the SUB channel less explored — verify the SUB frame injection path first.
| Control | Deck1 SUB byte.bit | Deck2 SUB byte.bit |
|---|---|---|
| Pads A–H | `0x06`.0 … `0x06`.7 | `0x0a`.0 … `0x0a`.7 |
| Beat Jump mode | `0x07`.0 | `0x0b`.0 |
| Slip Loop mode | `0x07`.1 | `0x0b`.1 |
| Auto/Beat Loop mode | `0x07`.2 | `0x0b`.2 |
| Hot Cue mode | `0x07`.3 | `0x0b`.3 |
| PLAY/PAUSE | `0x07`.6 | `0x0b`.6 |
| CUE | `0x07`.7 | `0x0b`.7 |

## Analog (10-bit 0..1023, u16 LE at offset) — RX3 [CONSUMER]/[CODE], calibration unfinished
Inject with `A <off> <value16>`. Deck2 EUP = deck1 + 16.
| Control | Channel/byte | RX note |
|---|---|---|
| Channel fader d1/d2 | SUB `0x0e` / SUB `0x12` | SUB frame |
| Crossfader | SUB `0x16` | SUB frame |
| Trim d1/d2 | EUP `0x26` / `0x36` | — |
| EQ HI d1/d2 | EUP `0x24` / `0x34` | — |
| EQ MID d1/d2 | EUP `0x28` / `0x38` | — |
| EQ LOW d1/d2 | EUP `0x2a` / `0x3a` | — |
| COLOR/filter d1/d2 | EUP `0x2c` / `0x3c` | — |
| Master level | EUP `0x4a` | — |
| Headphone LEVEL / MIXING | EUP `0x50` / `0x52` | — |
| Browse counter (the ROTATE) | EUP `0x54/0x55` | ✅ SOLVED — inc=down, dec=up |

NB our existing RX control map already has **tempo faders** (deck1 `0x04/0x0a`, deck2 `0x14/0x1a`,
split neg/pos halves) which do NOT line up with this RX3 trim/EQ layout — another reminder the
two models diverge on the mixer/analog block. Verify analogs individually.

## JOG — the RX3 repo does NOT solve jog rotation
- **Jog TOUCH** is a normal EUP button: `0x10`.0 (d1) / `0x20`.0 (d2) — testable like any button.
- **Jog ROTATION is a SEPARATE, UNDECODED protocol even in RX3**: 24-byte "JogCom" packets on
  `subucom_spi_rdy3.0` / `subucom_spi_rdy4.0`. The RX3 doc says these "remain undecoded"; no byte
  layout, field, or CRC is given, and the emulator binds no input to the platter. The doc's
  "EUP offset 12 bit 1 enters jog rotation processing" is ambiguous (absolute byte 12 vs
  deck-relative) and is only a mode-enter flag, not the rotation value.
- **Implication for RX:** jog rotation will need its own RE on the RX's ready-line channels, not the
  subucom EUP/SUB frames. The RX3 repo gives us nothing reusable for jog rotation. (Our RX panel's
  current `d1jog/d2jog` guesses `b 13 20 / b 13 10` are unverified and likely wrong — those bytes are
  Loop In/Out per the RX3 layout.)

## Verification method
For each candidate: put the UI in a context where the control has a visible effect, inject the
bit/analog, screendump, compare. Buttons that need a loaded track (LOAD, transport, pads) need a
track-load path first. The browse/nav cluster is testable from the USB browser now.
