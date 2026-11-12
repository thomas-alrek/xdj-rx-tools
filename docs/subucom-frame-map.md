# XDJ-RX sub-ucom frame → control map (authoritative, from the firmware's section decoders)

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
| 0x28 / 0x38 | 0x500a | EQ LOW | ◆ (was mislabelled MID) |
| 0x2a / 0x3a | 0x5009 | EQ MID | ◆ (was mislabelled LOW) |
| **0x2c / 0x3c** | **0x5011** | **CH FADER (d1/d2)** | ✅ **LIVE** (inject→`setChannelFader(ch,1.0)`) |
| 0x2e / 0x3e | 0x508b | COLOR / FILTER pot | ◆ (block+0xa) |
| 0x48 | 0x440c | BOOTH MONITOR | ○ |
| **0x4a** | **0x4405** | **MASTER LEVEL** | ✅ **LIVE** (inject→`setMasterVolume(m,1.0)`; was mislabelled HP MIXING) |
| 0x4c | 0x448b | LEVEL DEPTH | ○ (code 0x448b; was mislabelled HP LEVEL) |
| 0x4e | 0x600f | CROSSFADER | ✅ (offset, 0x600f) |
| 0x50 | 0x4403 | HP LEVEL | ○ (code 0x4403; was mislabelled MASTER LEVEL) |
| 0x52 | 0x4402 | HP MIXING | ○ (code 0x4402; was mislabelled BOOTH/MASTER-BAL) |
| 0x44 / 0x46 | 0x805/0x804 | MIC1 / MIC2 level | ○ (CH-fader ruled out — it's 0x2c/0x3c) |

*(All analog are audio-only — no emulator feedback — so these are firmware-decoded, not screen-verified.)*

**2026-11-12 — per-channel mixer block decoded from the frame decoder (static RE).** The
per-channel analog block is handled by the function at **VA 0x282800** (`push {r4,r5,r6,r7,lr}`;
r5=cur-frame block ptr, r4=prev; emits on change via `FUN_002adee8(obj, code, value16, flag)`).
It reads **block-relative** halfwords: EQ-HI block+0 (0x5008), TRIM block+2 (0x5007), EQ-LOW
block+4 (0x500a), EQ-MID block+6 (0x5009), **CH FADER block+8 (0x5011)**, COLOR block+0xa
(0x508b). Block base = **0x24 (ch1) / 0x34 (ch2)**, pinned by TRIM block+2 = the confirmed abs
0x26/0x36. ⇒ **CH FADER = abs 0x2c/0x3c** (this corrects the long-standing "0x2c = COLOR pot"
label; COLOR is 0x2e/0x3e). The master/booth/HP/**crossfader** block is the next function (VA
0x282a98), base 0x44, crossfader 0x600f at block+0xa = abs 0x4e (unchanged). This matches the
XDJ-RX3 field grouping (faders a distinct group from the EQ/trim block — see
docs/rx3-panel-crossref.md). **Open:** a prior boot-frame sweep of 0x0c–0x5c did not move
`ch+0x144`; with 0x2c now identified, retest = inject 16-bit **1023 at 0x2c** (and 0x3c) and
watch `ch+0x144` via gdb. If still 0, the gate is the code→`setChannelFader` dispatch (the
decoder emits 0x5011 only on *change*; the first accepted frame transitions via the
complement-seeded history, so runtime injection after boot may need a value *different from the
stored sample* to emit).

**2026-11-12 — ✅ LIVE CONFIRMED (fresh boot, gdb Z0 on the setter leaves + sub-ucom frame inject).**
Breaking at the setter *leaves* and injecting a `0→0x3ff` transition proved the mapping end-to-end
(the decoder-entry breakpoint at 0x282800 is NOT reached as a clean call on a changed frame — the
setter leaves are the real semantic endpoint, and Z0 software breakpoints catch them):
- `A 2c 3ff` → **`setChannelFader`** (`FUN_00083fcc`) fired, `r0=0x02a49ca0` (deck1 input ch),
  `r1=0x3f800000` = **1.0f**. ⇒ **frame 0x2c = CH FADER**, confirmed.
- `A 4a 3ff` → **`setMasterVolume`** (`FUN_00066bb8`) fired, `r0=0x02a571f0` (master obj),
  `r1=0x3f800000` = **1.0f**. ⇒ **frame 0x4a = MASTER LEVEL** (code 0x4405), confirmed — this also
  corrected the master/HP block labels (0x4a was mislabelled HP MIXING; 0x50=HP LEVEL, 0x52=HP
  MIXING, 0x4c=LEVEL DEPTH — the code column was right, the labels were scrambled).
Significance: the **real sub-ucom event path drives the mixer setters** (not just gdb pokes), so the
whole analog mix can be initialised through the frame like hardware — the production route to audio.

## Analog (16-bit LE, inj `A <off> <val16>`, value/1023)
- Deck1 continuous @ rbp 0x04,0x06,0x08,0x0a,0x0c,0x0e,0x12 — TEMPO fader = **0x04 (neg) + 0x0a
  (pos)** ✅ (complementary dual-field); the others are jog/vinyl-speed. Deck2 = +0x10.
- **Mixer analog** (TRIM/EQ/COLOR/CH-fader/crossfader/headphone/master) — separate decoder,
  base ~0x24+, codes 0x50xx/0x60xx. *(offsets being finalized — no LCD feedback, static-only.)*

## Coverage ceiling — why 100% of *panel controls* ≠ 100% subucom-injectable
Of the XDJ-RX's physical controls, these are **NOT sub-ucom buttons** in this 100-byte frame
(verified by decoding the whole frame — no code/slot exists for them):
- **Slide switches** (3-position, read as GPIO/status levels, not momentary codes): DECK1/DECK2
  ↔ PHONO/LINE, CROSSFADER CURVE, MIC OFF/ON/TALKOVER.
- **Separate ADC path** (not in the mixer-analog group, which is full at rbp 0x44–0x52): MIC EQ
  HI/LOW, BEAT FX TIME knob.
- **MAIN-UCOM GPIO** (top panel, same path as the source keys, wired via `/tmp/mainkey`):
  rekordbox/MIDI/USB1/USB2 (done), MASTER REC/WAKE UP, TRACK MARK.
- **Not a set of buttons**: SOUND COLOR FX NOISE/GATE/CRUSH/FILTER (the per-channel COLOR pot at
  rbp 0x2e/0x3e is the only sub-ucom part; note 0x2c/0x3c is CH FADER, not COLOR); LOOP SLICE (the RX has exactly 4 pad modes:
  HOT CUE/BEAT LOOP/SLIP LOOP/BEAT JUMP = codes 0x4113/14/15/16).
- **Separate protocol**: JOG-WHEEL **rotation** (jog *touch* is a normal deck button; rotation is
  not in this frame — see the RX3 "JogCom" notes).

**Label-ambiguous (in-frame, bits decoded, but RX reassigned the codes vs RX3 and deck transport
shows no LCD feedback, so the exact label per bit can't be fixed in the emulator):** TRACK SEARCH
◄◄/►►, SEARCH ►► forward, CUE/LOOP CALL ◄/►. These are among the decoded deck bits (0x10 low bits,
0x11 b4–7); assign by testing on real hardware.

## 2026-11-12 ✅ SOLVED — INPUT SELECT (source switch) is a sub-ucom frame BIT

The per-channel **source switch** (DECK ↔ LINE/PHONO, firmware `InputSourceSW`) is a plain
sub-ucom frame bit — injectable like any button, and it **re-routes live** on change:

| rbp bit | control | value |
|---|---|---|
| **`0x33` bit 7 (0x80)** | CH0 / deck1 source | `1`=DECK (`ch0→src0`), `0`=LINE/PHONO (`ch0→src3`) |
| **`0x43` bit 7 (0x80)** | CH1 / deck2 source | `1`=DECK (`ch1→src1`), `0`=LINE/PHONO (`ch1→src4`) |

Stride `+0x10` (deck1→deck2, like the faders/EQ). **Default DECK = hold both bits.**
Inject: `B 33 80` + `B 43 80` (persistent holds); release → LINE. Live-verified by reading the
channel routing table (`tools/xdj-panel/debug/tables.py`; ch0.routed @`0x107acdc+4`, ch1 @`0x107ace8+4`)
while toggling. Found with `tools/xdj-panel/debug/sweep_full.py` (sweep every frame byte, watch routed_src).

**NOT a GPIO.** GPIO 117/118 (and 134/135, 162-165) are rbp *output* pins (mux/control lines),
proven inert for routing. The only switch *inputs* via `/dev/gpiodrv` are USB-detect (126/204)
and the EXT-TYPE/LINE-PHONO switch (GPIO 119/120 → switchState `+0xa0`/`+0xb0`). The deck-vs-ext
*source* select is this frame bit, applied to the channel's `+0x254` requested-source by rbp's
own (C++ vtable) applier → router `FUN_00080a74`. See the engine mechanism below for the data path.

## 2026-11-12 — INPUT SELECT / deck→channel routing: the engine mechanism (static RE)

> ⚠️ **Reconciliation:** the sections below were written *before* the frame bit was found and
> conclude the source select "is **NOT** a sub-ucom frame control" / "dead-ends at a C++ vtable"
> / "boot-time LINE default to patch". **That conclusion is superseded** — see the SOLVED block
> above (`rbp[0x33].7` / `rbp[0x43].7`). The *data-path* facts here (source/channel tables,
> router `FUN_00080a74`, channel `+0x254` requested-source, the vtable applier) remain correct
> and explain how the frame bit reaches the routing; only "not a frame control" is wrong.

The gdb routing hack (`wr32(DAT_0107acdc + chan*12, <deck buffer>)`) pokes the *result* of
the engine's routing. The engine's own machinery is now decoded, which gives a cleaner,
"no-raw-poke" lever and explains why the channel is silent on boot.

**Two tables, 12-byte stride (`{buf_ptr, src_idx, flag}` each):**
- **Source table** base `DAT_0107ac88` — 7 slots (indices 0–6), one per physical source
  (decks / line / phono / mic …). Slot word0 = live render buffer ptr, word1 = source index.
- **Channel table** base `DAT_0107acdc` — 6 channels (the thing the gdb hack pokes). `DAT_0107acdc`
  is channel[0].buf (deck1), channel[1].buf = `DAT_0107ace8` (deck2), stride 12. word1 =
  the source index this channel is routed to.

**The three engine functions (all screen-free / audio-graph):**
- `FUN_00080920` — **init** (runs at graph setup). Zeroes every buf ptr; seeds each channel's
  routed-source word (`channel[n].src_idx`) to **7** = a self/empty slot ⇒ **every channel boots
  routed to nothing → silence.** This is the boot-silence root cause, independent of the mixer gains.
- `FUN_000809ec(src_idx, buf)` — a **source** registers/updates its live buffer; if any channel is
  currently routed to `src_idx` it also writes that channel's `buf` (propagation). Called from the
  per-source render teardown/setup wrappers at `0x6a230 / 0x6a38c / 0x6a424 / 0x6a478` (right before
  the ExtIn/line render `FUN_0006a4a8`); passing `buf=0` **un**registers (teardown path in `FUN_0006a230`).
- `FUN_00080a74(src_idx, chan_idx)` — the **router**: copies source slot `src_idx`'s 3-word descriptor
  into channel slot `chan_idx`. This is the primitive the INPUT SELECT ultimately drives.

**Channel object fields (MixerEngine channel, e.g. `0x02a49cc8` d1 / `0x02a4cb58` d2):**
- `+0x188` = selected source index (written by `FUN_00085d44` = **setChannelSource**, which also
  routes immediately).
- `+0x254` = **requested** source index, `+0x250` = **applied** source index. The per-frame apply
  `FUN_00085a10` notices `+0x254 != +0x250`, calls `FUN_00080a74(+0x254, chan)`, then latches
  `+0x250 = +0x254`. ⇒ **`+0x254` is the clean lever**: set it to the deck's source index and the
  engine re-routes through its own path (no raw `DAT_0107acdc` poke).

**Both `setChannelSource` (`FUN_00085d44`) and the apply (`FUN_00085a10`) have NO direct callers —
they are vtable-dispatched (rbp is C++).** So the INPUT-SELECT→`+0x254` write is an indirect/virtual
call; a static call-graph walk back to a frame byte dead-ends here. This corroborates that LINE/PHONO
is not a plain decoder bit. Finishing the trace needs the live loop: gdb `Z2` write-watch on a
channel's `+0x254` (and on `DAT_0107acdc`) while toggling sources / loading a deck, to see **what
value** denotes each deck and **who** writes it (panel event vs. deck-load/play auto-route).

### 2026-11-12 — LIVE-VERIFIED (gdb RSP + sub-ucom inject, deck1 playing, audio heard on host)
Booted, registered USB2, PUSH-loaded "Play It Hard" to deck1 (it **autoplays on load**), then read
the global tables over raw GDB-RSP (no host gdb — a Python RSP client; `-gdb tcp::1234`):

- **The deck's live audio renders into `src[0]`.** With deck1 playing, only `src[0].buf`
  (`0x02abf5f0`) held non-zero float PCM; `src[1..6]` were silent zeros. `src[0]` = deck1 player.
- **On load the firmware MIS-routes the deck channels.** Idle (pre-load): `ch0.routed=0`,
  `ch1.routed=0` (both at the deck bank). After loading deck1 **both** deck channels moved:
  `ch0.routed 0→3`, `ch1.routed 0→4` — i.e. onto the **silent external/LINE bank (src 3,4)**.
  So the deck audio sits in `src[0]` but `ch0` listens to `src[3]` ⇒ master peak = 0 (silent).
  This is the INPUT-SELECT-defaults-to-LINE bug, now confirmed end-to-end.
- **Fix proven, audio heard.** Routing `ch0 → src[0]` (write `ch0.buf = src0.buf`,
  `ch0.routed = 0`) **and** bringing the analog gains up (holds `A 2c 3ff` fader, `A 4a 3ff`
  master, `A 4e 200` xfader, EQ `A 24/2a/28 200`, trim `A 26 3ff`) made deck1 **audible on the
  host** (master peak `0 → full-scale`; user-confirmed). Reverting `ch0 → src[3]` silenced it
  again (peak→0). **Both routing AND gains are required** (gains alone = silent because ch0 still
  points at the silent src3; routing alone = silent because faders/master boot at 0).
- **The correct deck-channel routing is `ch_n → src[n]`** (deck1→src0, deck2→src1). The load-time
  code instead sends them to `src[n+3]` (the LINE bank) — an effective "+3" INPUT-SELECT=LINE
  default. Idle already had the *correct* `ch0→src0`; the LOAD is what breaks it.
- **The route is a one-time setup at the FIRST load** (it set BOTH ch0 and ch1 at once), NOT
  re-applied on reload — so `FUN_00080a74` / `FUN_00085a10` do **not** fire during steady play or a
  re-load (gdb breakpoints there stayed silent). To catch the writer + its INPUT-SELECT read, set
  `Z0` on `FUN_00080a74` (0x80a74) **before the first load** on a fresh boot and read `lr`.

**Practical fixes (either ends the gdb routing hack):** (a) model the INPUT-SELECT switch state as
DECK where the firmware reads it at load (a GPIO/flag — the read site is the one remaining trace),
or (b) post-load, force `ch_n.routed = n` (+ propagate `ch_n.buf = src[n].buf`) via the engine; the
init already defaults idle routing correctly, so the minimal patch is to stop the load-time LINE
re-route. Mixer gains must also be asserted in the boot frame (item 2).

### 2026-11-12 (cont.) — the mis-route is a BOOT-TIME default; the engine path, pinned
- **Idle routing (post-boot, pre-load) is already `ch0.routed=3`, `ch1.routed=4`.** So the deck
  channels are put on the silent LINE bank **at boot channel-strip setup**, not at track load.
  (Earlier "idle=0" was a transient read between the constructor and the override.) This is why
  every load-time breakpoint stayed silent — nothing re-routes at load.
- **`routed_src` is written ONLY by the init `FUN_00080920` and the router `FUN_00080a74`**
  (static xref of `0x107ace0`). The router is clean ARM; a Z0/Z1 bp on `0x80a74` DOES fire
  (confirmed — single-step and bp both work over raw RSP).
- **Channel-strip constructor `FUN_00084e20` routes the channel to src0 (DECK) correctly:** it sets
  the channel object's **requested source `obj+0x254` (= `param_1[0x95]`) = 0** and applied
  `obj+0x250` (`param_1[0x94]`) = 0, and calls `FUN_00080a74(0, chan)`. It also computes the
  per-channel LINE index (**3 for ch0, 4 for ch1**) and stows a line flag via `FUN_00080b28`.
- **The per-channel mixdown `FUN_00085a54` / apply `FUN_00085a10` route from `obj+0x254`:**
  `if (obj+0x254 != obj+0x250) FUN_00080a74(obj+0x254, chan); obj+0x250 = obj+0x254;`. So whatever
  sets **`obj+0x254` to the LINE index (3/4)** is the INPUT-SELECT default — that is the single
  remaining unknown. It is set after construction, before audio starts.
- **Catching that setter live is blocked:** it runs during early boot channel setup; a bp there
  wedges rbp (the halts broke its real-time init into a ~500k-iteration retry of the constructor
  route `FUN_00080a74(0,0)` from `lr=0x850dc`, boot never progressed). Statically it is behind a
  C++ vtable write to `obj+0x254`, not resolvable by address xref.

**Bottom line for item 1:** the deck→channel routing is a **boot-time INPUT-SELECT=LINE default**
baked into the mixer channel objects (`obj+0x254` = LINE idx). It is NOT a sub-ucom frame control
and NOT driven by the D1/D2 SRC keys (GPIO 119/120 — tested, no effect on `routed_src`). So the
clean "no gdb" fix is emulator/firmware-side, not a panel operation:
- **Recommended QEMU fix** (no rebuild of rbp, no per-boot step): the inject harness (patch 07)
  already rewrites guest memory each frame — have it also enforce the correct deck routing by
  writing the global channel table at fixed addresses: for the deck channels, `ch_n.buf =
  src[n].buf` and `ch_n.routed = n` (n=0 deck1, n=1 deck2), i.e. model INPUT SELECT = DECK. Pair
  with baking the analog-gain defaults into the boot frame (item 2). This reproduces exactly the
  live-verified fix (ch→src0 + gains = audio) with no gdb.
- Alternative: find and patch the firmware `obj+0x254` default (set it to the channel's deck index
  instead of the LINE index) — needs the vtable setter, which a future session can get by a bp that
  does not wedge boot (e.g. hardware bp + let rbp settle, or patch the boot route to not loop).

**gdb-free tooling built this session** (scratchpad): `rsp.py` (raw RSP: mem/reg read-write, Z0/Z2,
continue/stop), `tables.py` (dump both tables), `inj.py` (gain-preserving one-shot/hold/mainkey
injects), `mon.py`/`ser.py` (HMP monitor + guest console), `relaunch.sh` (debug launcher). USB
bring-up per `docs/usb-mount-howto.md` (hub-only boot → post-boot `device_add` → `touch
/tmp/usb2_insert`); known "USB Error" is cleared by pressing the USB2 source key again. Load a track
by PUSH-drilling to it then one more PUSH → TRACK MENU → PUSH on LOAD TO DECK1.

## Not in this frame / unresolved
- **JOG rotation**: a separate protocol (not this 100-byte frame); unmapped.
- Top-panel source keys (rekordbox/MIDI/USB1/USB2/DECK1-2/TIME MODE) are **MAIN-UCOM GPIOs**
  (158–161 etc.), not sub-ucom — already wired via `/tmp/mainkey`.

## PLAY works but playback position does not advance (audio DMA not emulated)

**Symptom (2026-11-11):** with a track loaded on a deck, pressing PLAY (`0x13.6`,
`b 13 40`) makes the deck react (view/state changes on the LCD) but the **REMAIN
time stays frozen** (e.g. `02:56.640`, unchanged 2.5 s later). The track never plays.

**Root cause — the deck playhead is slaved to the audio sample clock, which the
emulator has no backend for:**

- The QEMU machine (`hw/arm/fsl-imx6.c`) models **no audio peripheral** — the only
  SSI present is the *sub-ucom panel SPI slave*. There is no i.MX6 **SSI audio** path
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
