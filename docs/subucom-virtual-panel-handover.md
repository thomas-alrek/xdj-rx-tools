# Handover: make the XDJ-RX emulator fully operable via the real sub-ucom panel (kill the gdb hacks)

**Goal of the next session:** everything we currently force with gdb — the mixer gains,
the deck→channel routing, track loading — must instead be driven by the **real sub-ucom
panel protocol** and the **virtual web panel** (`tools/xdj-panel/`), so the device is
operated like hardware with **no gdb pokes**. `rbp` stays vanilla; all changes are
QEMU-side (the sub-ucom inject harness in `qemu/patches/02`+`07`, and the panel server).

Read first: `docs/subucom-frame-map.md` (authoritative frame→control map),
`docs/rbp-and-panel-protocol.md` (the panel class/dataflow), `docs/audio-silence-handover.md`
(the audio win + data path), `docs/xdj-rx-control-map.md`, and the HW diagram
(`XDJ-RX_Hardware_Diagram_en.pdf`, MIXER group = items 1–17).

---

## What is already SOLVED — do NOT re-derive

- **Audio output to host works.** Real deck audio from stock `rbp` reaches the Mac speakers,
  both decks. The sonifier is pinned to **SDMA ch1 = MasterOut** (hard-coded default in
  `qemu/patches/11`; `XDJ_AUDIO_OUT_CH` overrides). `XDJ_AUDIO_DEBUG=1` prints per-channel
  `[sdma-ch] chN bufpeak=` and `[sdma-au] master=N peak=`.
- **Audio data path is fully traced** (see `audio-silence-handover.md` SOLVED box): deck
  **Player render `FUN_00075eb0`** → channel **source pointer `DAT_0107acdc[chan*3]`**
  (deck1 = entry @ +0, deck2 = entry @ +12) → channel strip `FUN_00085bc0` → playengine
  mixdown `FUN_00043a2c`/`FUN_000506b0` → master DMA buffer (`FUN_000506b0` arg `param_4[0]`)
  → pcm0/MasterOut → SSI → SDMA ch1 → host.
- **Panel framing / CRC** solved: reflected CRC16 poly `0x8408`, init `0xffff`, final XOR,
  with a **per-32-bit-word byte reversal** before CRC (patch 02). 100-byte frame, one SPI
  channel (`/dev/subucom_spi1.0`; the RX has NO second channel — RX3 does, we don't).
- **Confirmed sub-ucom controls (live):** browse ROTATE = 16-bit counter at `0x54/0x55`,
  browse PUSH = `0x5d` bit4, **CH FADER = `0x2c/0x3c`** (→`setChannelFader`), **MASTER LEVEL
  = `0x4a`** (→`setMasterVolume`), TRIM `0x26/0x36`, EQ-HI `0x24/0x34`, EQ-LOW `0x28/0x38`,
  EQ-MID `0x2a/0x3a`, COLOR `0x2e/0x3e`, CROSSFADER `0x4e`. Full table + confidence in
  `docs/subucom-frame-map.md`.
- **Inject harness exists** (host-side, read by the QEMU patch each panel frame): write a
  one-line spec to **`/tmp/subucom_ctrl`** — `A <off_hex> <val_hex>` = HOLD a 16-bit analog
  while the file exists; `a` = one-shot; `B`/`b` = hold/one-shot a button bitmask. The web
  panel POSTs these. CRC is recomputed by the harness, so injects are accepted frames.

## The gdb hacks that must become real panel operations

### 1. INPUT SELECT — the #1 blocker (deck→channel routing)
**The gdb hack:** `wr32(DAT_0107acdc + chan*12, <deck Player buffer>)` — repoint each
channel's source pointer to its deck.
**Why:** the per-channel **INPUT SELECT switch** (firmware **`LinePhonoSW`, control code
`0x1013`**; HW-diagram "INPUT SWITCH", MIXER item 11) **defaults to LINE**, so the channel's
source slot is filled by the ExtIn/line capture render (`FUN_0006a4a8`, silent) instead of
the deck Player. Strings `"INPUT : LINE"`, `"EXT TYPE : LINE1/2"`, `"LinePhonoSW"` (@`0x42b454`)
confirm; the code→name table at `FUN @0x2a60e0` maps code `0x1013`→`LinePhonoSW`.
**What to find:** the **sub-ucom frame bit/offset for the INPUT SWITCH**. It is NOT emitted
by the analog/button decoders in `0x282000–0x28b000` (no `movw #0x1013` there — those hits
are the numeric constant `0x1013`, not the control code). So the switch is decoded elsewhere
or sets a state the engine queries.
**Approach options:**
  - Find the firmware handler that writes the channel source pointer / selects deck-vs-line
    (start from the ExtIn render `FUN_0006a4a8` and the source table `DAT_0107acdc`; find who
    writes `DAT_0107acdc[chan*3]` when the input changes), then trace the condition back to
    the frame byte.
  - OR empirical sweep: while a deck plays, inject each candidate switch bit (`b <off> <mask>`
    / `B <off> <mask>`) across the unmapped frame bytes and watch the deck channel's input
    peak `[channel+0x108]` or `DAT_0107acdc[0]` flip from the line buffer to the deck buffer.
  - Deck object anchors this boot (addresses move per boot; re-derive via the gate
    `FUN_00065028`): MixerEngine channels `0x02a49cc8`(deck1)/`0x02a4cb58`(deck2)/…; deck
    Player output buffers `0x02abfa58`(deck1)/`0x02abfc60`(deck2) (find via the mixdown
    source list of `FUN_00043a2c`, render `== 0x75eb0`; **note deck2 uses NO timestretch so
    it never hits `FUN_0007ddfc`** — use the source list, not the timestretch bp).
**Acceptance:** injecting the INPUT SWITCH to DECK makes a loaded+playing deck audible with
NO gdb repoint.

### 2. Mixer analog defaults (gains/EQ/crossfader)
**The gdb hack:** `gdbmixfull.py` sets master/fader/level/EQ/crossfader to unity via the
firmware's own leaf setters (`FUN_00066bb8` master, `FUN_00083fcc` fader, `FUN_00083df8`
level, `FUN_000836dc` EQ, `FUN_00066c2c` crossfader).
**Why:** on boot the modeled panel frame carries 0 for the analog section, so every channel
is faders-down / master-0 / EQ-cut → silence even once routing is fixed. On hardware the
panel continuously reports the physical pot positions.
**What to do:** have the inject harness **present sane analog defaults in the boot frame** —
faders up (`0x2c/0x3c` = ~0x3ff), master up (`0x4a`), crossfader center (`0x4e` = ~0x200),
EQ center, trim unity — using the confirmed offsets. Either bake defaults into the harness's
base frame, or have the web panel assert them at startup. Verify the on-air gate
`FUN_00065028`'s 5 conditions pass (master>22, level, EQ-not-all-0, fader>=6, xfader).
**Acceptance:** fresh boot has a usable mix with no `gdbmixfull.py`.

### 3. Virtual panel completeness (`tools/xdj-panel/`)
The web panel already serves the LCD + POSTs `/tmp/subucom_ctrl`. Extend it to expose **every**
control so the device is fully operable from the browser: channel faders, trim, 3-band EQ,
COLOR, crossfader, **INPUT SWITCH (once its bit is found)**, master/booth/HP, browse
rotate/push, BACK, LOAD, PLAY/CUE, tempo. Map each to its frame offset/bit from
`docs/subucom-frame-map.md`. Treat the inject spec as the single source of truth.

## Other open panel items (secondary)
- **Deck transport (PLAY/CUE/jog):** PLAY is received and reacts on-screen but the real
  deck-button runtime handler is NOT `FUN_00222188` (gdb bp there doesn't fire). Find the
  real handler; needed so transport works from the panel. Jog rotation is a separate 24-byte
  "JogCom" ready-line protocol (undecoded even in RX3) — lowest priority.
- **Track load without manual steps:** browse/load currently done by hand in the UI. For a
  scripted/panel flow, map LOAD (`0x5d` bit6/bit5 per RX3, verify) + the browse path.
- **Version display** (`Ver --.--`): cosmetic; see `docs/version-display-and-override.md`.

## Tools & method (all proven this project)
- **gdb RSP** on `tcp::1234` (raw packets; helpers in the session scratchpad). Break at a
  setter leaf or the gate `FUN_00065028`; **hardware/software breakpoints AND write
  watchpoints (`Z2`) both work**. Soft-float ABI: float args are IEEE-754 bit patterns in
  core regs. After any inferior-call, **resume the VM via the monitor socket `cont`** (a bare
  gdb `c` can leave it paused).
- **Inject:** host file `/tmp/subucom_ctrl` (format above). **Release stale holds** (`rm`)
  before a new one — a hold persists and blocks the change-gated decoders from re-firing.
- **Monitor** `/tmp/d_mon4.sock` (HMP; `device_add`/`screendump`/`cont`), **serial**
  `/tmp/d_con4.sock` (guest console; login root).
- **USB mount:** monitor `device_add usb-storage,bus=usb-bus.1,port=1.1,drive=usbstick,
  id=xdjusb,removable=on` then host `touch /tmp/usb2_insert`.
- **REA/Ghidra** for firmware tracing (`provider_id: ghidra`). CLOSE it before launching qemu
  — Ghidra + rekordbox starve the single-core TCG boot (black-screen hang at high host load).
- **Launch:** `qemu/watch.sh` (window + serial on stdio) or the socketed debug launcher in
  the session scratchpad (`relaunch.sh`: `-display cocoa` + gdb/monitor/serial sockets +
  `XDJ_AUDIO_DEBUG=1`).

## Definition of done
A fresh boot where the operator, using only the **web panel** (no gdb), can: set the INPUT
SWITCH to DECK, load + play a track, bring up the channel fader / EQ / master, and **hear it**
— on both decks, through the crossfader — exactly as on hardware.
