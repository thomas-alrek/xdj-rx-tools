# XDJ-RX subucom frame → control map (authoritative, from the firmware's section decoders)

The XDJ-RX panel MCU sends ONE 100-byte frame over `/dev/subucom_spi1.0` (single channel —
there is no SUB channel; every control is in this frame). rbp decodes it in three decoders,
each an unrolled `ldrb [cur,#OFF]; ldrb [prev,#OFF]; eor; tst #MASK; …` pattern:

- **Center/nav/beat-fx/source buttons** → code-emitting decoder `~0x282c00–0x283880`;
  `rbp_off = 0x44 + decoder_OFF`; emits 16-bit codes consumed by `FUN_0026ae7c`.
- **Deck buttons** → decoder `~0x2845xx–0x284d40`; `rbp_off = 0x04 + decoder_OFF` (deck1),
  **deck2 = deck1 + 0x10**; emits codes `0x41xx/0x42xx`; routed via `FUN_00222188(ctlr,
  deckIdx, evtType, index)`.
- **Mixer/analog continuous** → decoder near `0x282400–0x282800`; 16-bit LE ÷1023.0 → codes
  `0x50xx/0x60xx` via `FUN_002adee8`. *(offsets being finalized)*

Confidence: ✅ = screen-verified; ◆ = firmware-decoded + code matches RX3 (strong); ○ =
firmware-decoded, RX-specific code / positional label (verify before trusting the label);
inj = `b <rbp_off> <mask>` for buttons, `A <rbp_off> <val16>` for analog.

## Center section (rbp 0x59–0x5f), code → FUN_0026ae7c
| rbp.bit | code | control | conf |
|---|---|---|---|
| 0x5d.4 | 0x4205 | Browse encoder PUSH | ✅ |
| 0x54/0x55 | (counter) | Browse encoder ROTATE (16-bit, inc=down) | ✅ |
| 0x5d.0 | 0x8001 | USB1 STOP | ✅ |
| 0x5d.1 | 0x8002 | USB2 STOP | ✅ |
| 0x5d.5 / 0x5d.6 | 0x420d | LOAD1 / LOAD2 | ◆ |
| 0x5d.7 | 0x420b | (center key, name TBD) | ○ |
| 0x5e.0 | 0x203 | BACK | ✅ |
| 0x5e.1 | 0x409 | TAG LIST | ✅ |
| 0x5e.2 | 0x401 | (nav, name TBD) | ○ |
| 0x5e.4 | 0x8002 | (USB-class key, distinct from eject) | ○ |
| 0x5e.5 | 0x4488 | BEAT FX (sticky) | ○ |
| 0x5f.0 | 0x20a | MENU (opens SORT) | ✅ |
| 0x5f.1 | 0x204 | (center key, name TBD) | ○ |
| 0x5f.2 | 0x209 | (center key, name TBD) | ○ |
| 0x5f.3 | 0x208 | (switches browse view) | ○ |
| 0x59.4–7 | 0x5095–8 | mixer/cue keys | ○ |
| 0x5a.* | 0x4493/0x449x | BEAT FX select/channel | ○ |
| 0x5b.1–3 | 0x5014/0x4487/0x4486 | mixer / BEAT FX ON·OFF·ROLL | ◆ (0x5b.2/3 screen-seen) |
| 0x56 / 0x57 | — | BEAT FX beat◄/► + params (byte-confirmed) | ◆ |

## Deck section (deck1 rbp 0x10–0x13; deck2 = +0x10 → 0x20–0x23), code → FUN_00222188
| rbp.bit (d1) | code | control | conf |
|---|---|---|---|
| 0x10.7/.6/.5/.4 | 0x4117–a | HOT CUE pads A/B/C/D | ◆ (RX3-match) |
| 0x11.0 | 0x4116 | BEAT JUMP mode | ◆ |
| 0x11.1 | 0x4115 | SLIP LOOP mode | ◆ |
| 0x11.2 | 0x4114 | BEAT/AUTO LOOP mode | ◆ |
| 0x11.3 | 0x4113 | HOT CUE mode | ✅ ("NO HOT CUE POINT") |
| 0x11.4/.5 | 0x411b/c | pad-row E/F | ○ |
| 0x12.0 | 0x4103 | SHIFT | ◆ (RX3-match) |
| 0x12.1 | 0x4111 | REV / MASTER | ○ |
| 0x12.2 | 0x4109 | MASTER TEMPO | ○ |
| 0x12.3 | 0x4108 | TEMPO RANGE | ✅ |
| 0x12.4 | 0x4112 | MASTER (sync master) | ○ |
| 0x12.5 | 0x410b | SYNC | ✅ ("SYNC ON") |
| 0x12.6 | 0x4104 | VINYL | ◆ (RX3-match) |
| 0x12.7 | 0x4110 | RELOOP/EXIT | ○ |
| 0x13.7 | 0x4102 | CUE | ○ (code RX3-match, bit unverified) |
| 0x13.6 | 0x4101 | PLAY/PAUSE | ○ (inferred; injected → no LCD effect) |
| 0x13.0–5 | — | LOOP IN/OUT/RELOOP, CALL ◄/►, SEARCH/TRACK | *(decoding)* |

## Analog (16-bit LE, inj `A <off> <val16>`, value/1023)
- Deck1 continuous @ rbp 0x04,0x06,0x08,0x0a,0x0c,0x0e,0x12 — TEMPO fader = **0x04 (neg) + 0x0a
  (pos)** ✅ (complementary dual-field); the others are jog/vinyl-speed. Deck2 = +0x10.
- **Mixer analog** (TRIM/EQ/COLOR/CH-fader/crossfader/headphone/master) — separate decoder,
  base ~0x24+, codes 0x50xx/0x60xx. *(offsets being finalized — no LCD feedback, static-only.)*

## Not in this frame / unresolved
- **JOG rotation**: a separate protocol (not this 100-byte frame); unmapped.
- Top-panel source keys (rekordbox/MIDI/USB1/USB2/DECK1-2/TIME MODE) are **MAIN-UCOM GPIOs**
  (158–161 etc.), not subucom — already wired via `/tmp/mainkey`.
