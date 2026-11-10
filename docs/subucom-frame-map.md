# XDJ-RX subucom frame → control map (authoritative, from the firmware's section decoders)

The XDJ-RX panel MCU sends ONE 100-byte frame over `/dev/subucom_spi1.0` (single channel —
there is no SUB channel; every control is in this frame). rbp decodes it in three decoders,
each an unrolled `ldrb [cur,#OFF]; ldrb [prev,#OFF]; eor; tst #MASK; …` pattern:

- **Center/nav/beat-fx/source buttons** → code-emitting decoder `~0x282c00–0x283880`;
  `rbp_off = 0x44 + decoder_OFF`; emits 16-bit codes consumed by `FUN_0026ae7c`.
- **Deck buttons** → decoder `~0x2845xx–0x284d40`; `rbp_off = 0x04 + decoder_OFF` (deck1),
  **deck2 = deck1 + 0x10**; emits codes `0x41xx/0x42xx`. **Runtime note (2026-11-11):** the
  code→handler is NOT `FUN_00222188` as previously written — gdb breakpoints on `0x222188`
  do **not** fire for SYNC / TEMPO-RANGE / PLAY / CUE even though all four produce LCD
  reactions, so `FUN_00222188` is a *deck-engine state object*, not the button dispatch. The
  real deck-button handler is still TBD. Deck buttons ARE delivered and decoded correctly
  (verified by LCD effect); the earlier static "routed via FUN_00222188" was an unconfirmed
  inference.
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
| 0x13.0 | 0x410f | REV / DIRECTION | ◆ (RX3-match) |
| 0x13.1 | 0x410d | LOOP OUT | ◆ (RX3-match) |
| 0x13.2 | 0x4121 | VINYL SPEED ADJ | ◆ (RX3-match) |
| 0x13.3 | 0x4120 | SEARCH ◄ (reverse) | ◆ (RX3-match) |
| 0x13.4 | 0x431e | DELETE | ○ (positional) |
| 0x13.5 | 0x431f | MEMORY | ○ (positional) |
| 0x13.6 | 0x4101 | PLAY/PAUSE | ✅ received — deck reacts, but **position frozen** (audio-clock-gated, see below) |
| 0x13.7 | 0x4102 | CUE | ◆ (RX3-match) |

## Analog continuous (16-bit LE, inj `A <off> <val16>`, ÷1023) — base 0x44 CONFIRMED (0x600f@0x4e)
| rbp | code | control | conf |
|---|---|---|---|
| 0x04 / 0x0a (+ 0x14/0x1a d2) | — | TEMPO fader (dual complementary) | ✅ |
| 0x24 / 0x34 | 0x5008 | EQ HI (d1/d2) | ◆ |
| 0x26 / 0x36 | 0x5007 | TRIM | ○ (audio-only) |
| 0x28 / 0x38 | 0x500a | EQ MID | ◆ |
| 0x2a / 0x3a | 0x5009 | EQ LOW | ◆ |
| 0x2c / 0x3c | 0x5011 | COLOR / FILTER | ○ |
| 0x48 | 0x440c | BOOTH MONITOR | ○ |
| 0x4a | 0x4405 | HEADPHONE MIXING | ◆ (RX3-match) |
| 0x4c | 0x448b | HEADPHONE LEVEL | ○ |
| 0x4e | 0x600f | CROSSFADER | ✅ (offset, 0x600f) |
| 0x50 | 0x4403 | MASTER LEVEL | ◆ (RX3-match) |
| 0x52 | 0x4402 | BOOTH/MASTER-BAL | ○ |
| 0x44 / 0x46 | 0x805/0x804 | CH1/CH2 fader OR MIC1/2 (ambiguous) | ○ |

*(All analog are audio-only — no emulator feedback — so these are firmware-decoded, not screen-verified.)*

## Analog (16-bit LE, inj `A <off> <val16>`, value/1023)
- Deck1 continuous @ rbp 0x04,0x06,0x08,0x0a,0x0c,0x0e,0x12 — TEMPO fader = **0x04 (neg) + 0x0a
  (pos)** ✅ (complementary dual-field); the others are jog/vinyl-speed. Deck2 = +0x10.
- **Mixer analog** (TRIM/EQ/COLOR/CH-fader/crossfader/headphone/master) — separate decoder,
  base ~0x24+, codes 0x50xx/0x60xx. *(offsets being finalized — no LCD feedback, static-only.)*

## Coverage ceiling — why 100% of *panel controls* ≠ 100% subucom-injectable
Of the XDJ-RX's physical controls, these are **NOT subucom buttons** in this 100-byte frame
(verified by decoding the whole frame — no code/slot exists for them):
- **Slide switches** (3-position, read as GPIO/status levels, not momentary codes): DECK1/DECK2
  ↔ PHONO/LINE, CROSSFADER CURVE, MIC OFF/ON/TALKOVER.
- **Separate ADC path** (not in the mixer-analog group, which is full at rbp 0x44–0x52): MIC EQ
  HI/LOW, BEAT FX TIME knob.
- **MAIN-UCOM GPIO** (top panel, same path as the source keys, wired via `/tmp/mainkey`):
  rekordbox/MIDI/USB1/USB2 (done), MASTER REC/WAKE UP, TRACK MARK.
- **Not a set of buttons**: SOUND COLOR FX NOISE/GATE/CRUSH/FILTER (the per-channel COLOR pot at
  rbp 0x2c/0x3c is the only subucom part); LOOP SLICE (the RX has exactly 4 pad modes:
  HOT CUE/BEAT LOOP/SLIP LOOP/BEAT JUMP = codes 0x4113/14/15/16).
- **Separate protocol**: JOG-WHEEL **rotation** (jog *touch* is a normal deck button; rotation is
  not in this frame — see the RX3 "JogCom" notes).

**Label-ambiguous (in-frame, bits decoded, but RX reassigned the codes vs RX3 and deck transport
shows no LCD feedback, so the exact label per bit can't be fixed in the emulator):** TRACK SEARCH
◄◄/►►, SEARCH ►► forward, CUE/LOOP CALL ◄/►. These are among the decoded deck bits (0x10 low bits,
0x11 b4–7); assign by testing on real hardware.

## Not in this frame / unresolved
- **JOG rotation**: a separate protocol (not this 100-byte frame); unmapped.
- Top-panel source keys (rekordbox/MIDI/USB1/USB2/DECK1-2/TIME MODE) are **MAIN-UCOM GPIOs**
  (158–161 etc.), not subucom — already wired via `/tmp/mainkey`.

## PLAY works but playback position does not advance (audio DMA not emulated)

**Symptom (2026-11-11):** with a track loaded on a deck, pressing PLAY (`0x13.6`,
`b 13 40`) makes the deck react (view/state changes on the LCD) but the **REMAIN
time stays frozen** (e.g. `02:56.640`, unchanged 2.5 s later). The track never plays.

**Root cause — the deck playhead is slaved to the audio sample clock, which the
emulator has no backend for:**

- The QEMU machine (`hw/arm/fsl-imx6.c`) models **no audio peripheral** — the only
  SSI present is the *subucom panel SPI slave*. There is no i.MX6 **SSI audio** path
  and no **SDMA audio** channels.
- On the guest, ALSA enumerates (`card0: ak4387-audio`, codec + `imx-ssi` map OK) but
  the DMA platform fails: `dmesg` shows **`asoc: platform imx-pcm-audio.0 hw params
  failed`**, and every playback substream (`/proc/asound/card0/pcm{0,1,2}p/sub0/status`)
  reads **`closed`** — rbp never opens the audio device.
- CDJ/XDJ players advance the deck position from frames consumed by the audio hardware.
  With no SDMA audio DMA to drain PCM at 44.1 kHz, no period-elapsed IRQ fires, the ALSA
  pointer never moves, and rbp's playhead is stuck — even though "play" latched.

**So the PLAY mapping is correct; this is the (still-unimplemented) `audio` roadmap
feature.** To make playback advance, QEMU must model the i.MX6 **SSI + SDMA audio**
path enough that `snd_pcm_hw_params` succeeds and the DMA produces period IRQs at the
PCM rate (a real backend for sound, or a "null sink that drains at realtime" if only the
*visual* playhead advancing is wanted, no actual audio).
