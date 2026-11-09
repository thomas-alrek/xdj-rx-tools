# XDJ-RX panel control map

Status: **2026-11-09.** Empirical map of the Subucom panel frame → XDJ-RX physical
controls, built by injecting controls into the emulated panel and observing the live
UI (md5-diffed screendumps). Pairs the *verified-visual* controls (below) with the
static byte/bit→code tables in [`rbp-and-panel-protocol.md`](rbp-and-panel-protocol.md)
("FULL control map"). See also [`gpu2d-handover.md`](gpu2d-handover.md) Follow-up 5 and
[`emulator-status.md`](emulator-status.md) feature C.

## How this was derived

The QEMU `xdj-subucom` slave builds a 100-byte frame in `rbp_buf` coordinates (control
sections at `0x04/0x14/0x24/0x34/0x44`, CRC-16/X-25 at `[0x62]`) and injects a control
from host `/tmp/subucom_ctrl`:

- `b <off> <bitmask>` — one-shot button press+release (auto, ~30 frames)
- `a <off> <value>` — one-shot 16-bit LE analog, then release
- `B <off> <bitmask>` — **hold** a button while the file exists (release on `rm`)
- `A <off> <value>` — **hold** a 16-bit LE analog while the file exists

(`B`/`A` persistent-hold mode was added to the harness for this mapping — it lets a
held/analog/momentary state be screendumped. Offsets/values hex.) Each probe captures
**pre / held / post** and md5-diffs them: `held≠pre` = visible while held (momentary or
analog); `post≠pre` = a sticky toggle.

## ✅ Verified controls (confirmed by a live on-screen change)

| frame offset / bit | XDJ-RX control | observed effect | kind |
|---|---|---|---|
| `[0x04/0x05]` and `[0x0a/0x0b]` (16-bit LE) | **DECK 1 TEMPO fader** | DECK 1 `TEMPO` % sweeps −16…0…+16 | analog |
| `[0x12]` bit3 (`0x08`) | **DECK 1 TEMPO RANGE** | cycles ±6 → ±10 → ±16 → WIDE | button (sticky) |
| `[0x14/0x15]` and `[0x1a/0x1b]` (16-bit LE) | **DECK 2 TEMPO fader** | DECK 2 `TEMPO` % sweeps | analog |
| `[0x22]` bit3 (`0x08`) | **DECK 2 TEMPO RANGE** | cycles | button (sticky) |
| `[0x56]` (8 active bits) | **BEAT FX beat ◄/► + beat-value** | `BEAT EFFECTS` ms/beat steps (e.g. 500 ms ↔ 10 ms); bit6 (`0x40`) is a sticky mode toggle | mixed |
| `[0x57]` bits 0–2 | **BEAT FX** (further params) | distinct `BEAT EFFECTS` states | momentary |
| `[0x5a]` (7 active bits) | **BEAT FX SELECT (type) + CH SELECT** | effect ROLL↔DELAY↔…, CH SELECT MASTER↔1↔2; bits 0 (`0x01`) & 5 (`0x20`) sticky | mixed |
| `[0x5b]` bits 2–3 (`0x04`,`0x08`) | **BEAT FX ROLL / TAP** | `ROLL` turns orange; `TAP` BPM turns green | button (sticky) |

Notes:
- The two TEMPO faders live in the **deck Fmt/Common sections** (`+0x04` deck 1, `+0x14`
  deck 2) as 16-bit values — **not** the `+0x24`/`+0x34` "per-deck analog" group the
  older static table assumed. This also resolves the old `[0x1a/0x1b]` contradiction:
  it is a 16-bit analog (the deck-2 tempo fader), not "MENU".
- **TEMPO fader is a complementary dual-field pot** (verified by screendump sweep): each
  fader reports TWO 16-bit fields that rbp reads *together* — deck 1 `[0x04/05]` = negative
  side, `[0x0a/0b]` = positive side (deck 2: `[0x14/15]` / `[0x1a/1b]`). With only one
  field forced and the other 0, rbp reads the pot as **fully pegged** (any nonzero positive
  field → +max; in WIDE it reads ratiometrically). The physical fader always sends both as
  a matched pair, so the emulator must too: drive `0x0a = round(pos·1023)` and
  `0x04 = 1023 − 0x0a` (0a=04 at centre → 0%; 04=max/0a=0 at top → −range; 0a=max/04=0 at
  bottom → +range). This is why the injection harness grew multi-field holds (qemu patch 07).
  Confirmed: `(04=0x3ff,0a=0)`→−16%, `(04=0,0a=0x3ff)`→+16%, `(04≈0a)`→0%.
- `[0x12]`/`[0x22]` bit3 = TEMPO RANGE is the cleanest single-bit result (isolated by
  the per-bit sweep).
- The BEAT FX bytes (`0x56/0x57/0x5a/0x5b`) each drive several on-screen sub-functions;
  the per-bit held hashes are recorded in `scratchpad/map/bits_5x.out`. Exact per-bit
  physical labels (BEAT ◄ vs ►, FX ON/OFF, QUANTIZE, LEVEL/DEPTH) need a few more
  screendump reads to pin — the bytes and their byte-level function are confirmed.

## Raw sweep data (for refinement)

`scratchpad/map/`:
- `byte_sweep.out` — byte-level hold sweep (pre/held/post), all offsets 0x04–0x5f.
- `bits_5x.out` — per-bit sweep of the BEAT FX/Other section 0x54–0x5f.
- `bits_deckglob.out` — per-bit deck/tempo-range (partial; the run's QEMU died under
  host load after 0x22 — see "Coverage boundaries").
- `*_held.ppm` / `swx_*.ppm` — the screendumps behind each hit.

## Coverage boundaries (what screen-diff mapping CANNOT reach)

Screen-diff can only map controls that have an **on-screen indicator**. Two large
classes do not, and need a different method:

1. **Audio-only controls** — channel faders, crossfader, 3-band EQ, TRIM, COLOR knob,
   headphone CUE/MIX/LEVEL, MASTER/BOOTH level, MIC. These change audio, not the deck
   screen, so no screen-diff fires. Their frame offsets exist in the static decoder
   tables (Mixer `FUN_00282800` `+0x24/+0x34`, Other `FUN_00282a98` `+0x44`) with event
   codes, but a physical label needs **gdb event-code capture**: breakpoint the control
   dispatcher (`0x28411c` for buttons; the analog handler) and read the emitted code per
   injected byte/bit, then map codes→labels. Immune to the desync/render issues.
2. **Deck transport & jog** — PLAY/CUE, hot cues, SYNC, SHIFT, loop (IN/OUT/RELOOP,
   auto-beat-loop), beat jump, and the **JOG wheel** produce no visible change with
   **no track loaded** (the deck shows "Not Loaded."). Mapping them needs a **loaded
   track**, which needs BROWSE → navigate → LOAD. That path is currently blocked (below).

## Known blockers / environment notes

- **`iPodMgr` startup SIGSEGV (nondeterministic, load-sensitive).** On a loaded host,
  rbp's `iPodMgr` thread segfaults at startup (`signal 11 @ 0x24b700`), killing rbp
  before it paints — the guest stays on the boot splash. On a lightly-loaded host a
  clean boot succeeds first try. Keep host load low (kill stray helper processes; the
  hundreds of generated `.ppm` files trigger macOS thumbnail/Spotlight load). Verify the
  deck UI (`fb md5 == b2cd7e0c…`, `pidof rbp_solo` non-empty) before sweeping;
  `scratchpad/bootverify.sh` retries until it boots. This startup crash is also why
  **BROWSE/track-load with USB mounted is flaky** — `iPodMgr` is in the USB-probe path.
- **Panel input goes quiescent after ~150 inject/release cycles.** rbp's change-gated
  poll stops dispatching after long runs of near-identical injected frames (real
  hardware streams continuous frames). Workaround: **fresh boot per sweep phase**, keep
  each phase ≤ ~90 probes. A frame "heartbeat" at byte `0x60` to keep frames distinct
  was tried and **reverted** — byte `0x60` is **inside rbp's frame-validation window**
  (it broke the CRC and stalled boot), so it is not spare padding; a heartbeat needs a
  genuinely-unused byte, TBD.

## Next steps

1. **gdb event-code capture** → map the audio-only mixer/EQ/fader controls (codes already
   known; attach physical labels via a press→code table).
2. **Pin BROWSE + load a track** (needs a reliable USB boot past the `iPodMgr` crash) →
   then screen-diff-map the whole deck transport + JOG layer against the loaded track.
3. Finish the BEAT FX per-bit labels (a handful of screendump reads).

## Browser / navigation subsystem — from rbp RE (2026-11-09)

The screen-diff sweeps found no byte that opens the media browser, because BROWSE and
the other navigation keys are handled by a self-contained **browser state machine** in
rbp (text `0x000df…0x000e1`), not a flat panel→screen toggle. RE (REA/Ghidra) of it:

- The browser-key **dispatch table** is populated by `FUN_000e11a4` at `0x017f3140`
  (stride `0x10`); `FUN_000e1334` polls a per-key "pressed" flag and calls the handler:
  ```
  slot(flag@+0x8, handler@+0xc)   handler        key (from @Ui_BrowseKey> KEYCMD strings)
  0x017f3184                       FUN_000e051c   BROWSE
  0x017f3134 … 0x017f3224          FUN_000e1010, 0e0f94, 0e0f18, 0e0e9c, 0e0e10, 0e05dc,
                                   0e0390, 0e0024, 0e0cfc, 0e108c, 0dfe94, 0e08e8, 0e07f0,
                                   0dfbf8  (LOAD1/LOAD2/USB/SD/LINK/MENU/BACK/INFO/TAG/
                                            EncoderRotate/EncoderPush/DECK_SELECT/ADD_TAG…)
  ```
  The exact key↔handler labels are in the `@Ui_BrowseKey> KEYCMD <name>` debug strings
  (`0x39db50`–`0x39dd3c`): DECK_SELECT, ADD_TAG, EncoderPush, MENU, BROWSE, INFO,
  TIME MODE, BACK, LOAD1, TAG LIST, USB, SD, LINK, rekordbox, LOAD2, EncoderRotate.
- The "pressed" flags (e.g. BROWSE `0x017f318c`) are set/cleared only from within the
  module (~29 sites — the browse state machine), so the panel-bit→key link is not a
  static table; it runs through this machine.
- Track load path (for a direct-load shortcut): `reqMusicLoad(deck, enDbMsg_Browse2Play,
  musicid)` (log str `0x39fba8`), `notifyMusicSelect(id,player,device,browse,content_id)`
  (`0x39f91c`), `MenuSelReqLoadTrack()` (`0x41a01c`); DB at `/PIONEER/LIBRARY/PDTL.DB`.

**Definitive mapping method (next):** breakpoint the named handler addresses in gdb
(`-gdb tcp::1234`) and sweep panel bits — the injection that hits `FUN_000e051c` is
BROWSE, the one that hits the LOAD handler is LOAD1/2, etc. This maps every
browser/transport control directly, without unwinding the state machine, and works for
the audio-only controls too (they emit codes even with no on-screen change).

**Test USB:** `test/usb/rekordbox_test.img` is a valid rekordbox export (FAT32) with
`PIONEER/` DB + one track `Contents/BITCRUSHR/Play It Hard/BITCRUSHR - Play It Hard.mp3`.
Mount it with QEMU `snapshot=on` (now default in launch) so boots never dirty it.

## ✅ COMPLETE byte→code map — captured LIVE via gdb (2026-11-09)

Breakthrough method: the panel section handlers all funnel through one observer sink,
`IKeyManager::sendKey` = **`FUN_002a47c0`** (found live: at a section handler, the
receiver's `+0x3c` observer's `vtable[8]` resolves to runtime `0x2a47c0`). The control
**code is r1 (2nd arg)**. Breakpointing `0x2a47c0` in gdb and injecting each panel byte
(`B <off> ff`) and reading r1 gives the authoritative code for every control — **idle
noise = 0** (it fires only on injected controls). Raw capture: `scratchpad/codesweep.out`.

Codes by frame region (`0xff` sets all 8 bits, so a byte lists all its bits' codes;
adjacent bytes share codes where a value spans two bytes):

| frame region | codes | meaning |
|---|---|---|
| `[0x04/05]`,`[0x0a/0b]` (D1), `[0x14/15]`,`[0x1a/1b]` (D2) | `0x410a` | TEMPO fader (analog) |
| `[0x06-0x08]` / `[0x16-0x18]` | `0x411d` | deck control |
| `[0x0c-0x0f]` / `[0x1c-0x1f]` | `0x4306` | deck control |
| `[0x10-0x13]` / `[0x20-0x23]` | `0x4103-0x411c, 0x4306, 0x430e, 0x421b, 0x421c` | **deck transport/hot-cue/loop buttons** (PLAY/CUE/SYNC/SHIFT/HOTx/LOOP…) |
| `[0x24-0x30]` / `[0x34-0x40]` | `0x5007,0x5008,0x5009,0x500a,0x5011,0x508b` | **deck analogs** (jog/params) |
| `[0x33]` / `[0x43]` | `0x5112` | deck button |
| `[0x44-0x52]` | `0x805,0x804,0x440c,0x4405,0x448b,0x4403,0x4402,0x448a` | **mixer** (channel faders/EQ/trim + buttons) |
| `[0x4e-0x50]` | `0x600f` | **encoder/rotary** |
| `[0x59-0x5a]` | `0x5095,0x5096,0x5097,0x5098` | COLOR-FX / param analogs |
| `[0x5b-0x5c]` | `0x803, 0x6010 (encoder), 0x4486, 0x4487, 0x5014` + ROLL/TAP (bit2/3) | beat-fx + **browser rotary 0x6010** |
| `[0x5d-0x5e]` | `0x420b, 0x420d, 0x4205, 0x8001, 0x8002, 0x4488` | beat-fx / crossfader |
| `[0x5f]` | `0x204, 0x208, 0x209, 0x20a (MENU), 0x401, 0x409, 0x4206, 0x4488` | **GLOBAL nav buttons** (MENU/BROWSE/BACK/TAG/USB…) |

Code→name: `IKeyManager::sendKey` routes the code to the active screen's key handler;
the browser handler names its keys via `@Ui_BrowseKey> KEYCMD <name>` strings. MENU =
`0x20a` (confirmed). The remaining nav code↔name labels (BROWSE/USB/LOAD vs
0x204/0x208/0x209/0x401/0x409) need one more RE pass on the key→handler registration.

## ★ Browse encoder (rotary + push) RE — 2026-11-09 (corrects the "0x6010" guess)
Traced the legacy `@Ui_BrowseKey` browser path end-to-end in rbp:
- **EncoderRotate handler = `FUN_000e159c`** (logs `KEYCMD EncoderRotate`, str `0x39dd3c`).
  Acts only when **`DAT_017f3234`** (accumulated signed rotation) != 0, dispatching that
  signed delta to the mode-specific list-nav fn (`FUN_000d6950 / 69f0 / 6ac4 / 6b4c / …`).
- `DAT_017f3234` accumulated by **`FUN_000e12e0(delta)`** (`DAT_017f3234 += clamp(delta)`);
  `delta = DAT_0107be1c`.
- `DAT_0107be1c` set by the browser event dispatcher **`FUN_0026ae7c`** when it receives an
  event with **u16 code `0x4205`** and a **SIGNED delta word at event+0xc**
  (`ldr r1,[r4,#0xc]; bl FUN_000b36e0`). So the real EncoderRotate code is **`0x4205`
  carrying a signed delta** — NOT a single bit, and NOT `0x6010` (the old sendKey-sweep
  label was a different, inactive code).
- **EncoderPush** name str `0x39db94`; its handler is one of the dispatcher's button codes
  (`0x201/0x202/0x203/0x204/0x208/0x209/0x20a/0x420b/0x420c/0x420d/0x4206`) — not yet pinned.

**TWO OPEN BLOCKERS before this drives the live library from the panel** (verified live, gdb):
1. **The active rekordbox library browser** (the `ARTIST/ALBUM/TRACK/…` category screen) does
   **NOT** route through `FUN_0026ae7c` — a clean bp on `0x26ae7c` never fires on this screen,
   even for MENU. So the on-screen category browser is a **different implementation** than the
   legacy `@Ui_BrowseKey` path above; its encoder/push handler is still unidentified.
2. **Panel-injection offset coordinate mismatch.** The QEMU harness MENU anchor is **rbp
   offset `0x1b` → code `0x20a`** (fsl-imx6.c `inj_off=0x1b`), but the byte→code map's column
   labels MENU as "0x5f". So the `B <off> <bit>` **rbp offset ≠ the map's offset column**;
   with the current harness, single-bit holds across rbp 0x4a–0x5f all decode to a FIXED mixer
   code (`0x440c` for bit 0x01, `0x4487` for bit 0x04) regardless of offset. The
   rbp-offset ↔ map-offset ↔ byte-swapped-frame-offset mapping must be pinned before any
   browse control can be injected reliably. **Calibration anchor: MENU @ rbp `0x1b`.**
   (gdb note: clear ALL stale `Z1` breakpoints between probes — a leftover bp at `sendKey`
   0x2a47c0 makes every `continue` stop there and report its `r1`, which looks like a stuck code.)

### Session 3 (2026-11-09, clean post-USB-fix instance) — more RE, still blocked on frame synthesis
On a FRESH instance (USB solved, library loaded, NOT harness-degraded):
- **`FUN_0026ae7c` IS the active library browser** — it fires on injected panel events here, so
  blocker #1 above ("different implementation") was **a degraded-harness artifact, not real**. The
  legacy `@Ui_BrowseKey` path drives the on-screen `ARTIST/ALBUM/…` category list.
- **Encoder event constructor = `FUN_002adfa0`** (builds the 0x24-byte event with code `0x4205`;
  tempo/buttons use the sibling `FUN_002adee8`). It is **quiet at idle** and fires ONLY for the
  encoder — so `Z1,2adfa0,4` is a clean encoder-only probe. `FUN_0028411c` (the old "panel
  receiver" anchor) is actually the **TEMPO-fader** section handler — it hardcodes code `0x410a`;
  that's why lone-bit injection always collapsed to 0x410a.
- **Frame decoder = `FUN_0028a7b8`** (REA file-offset `0x282a98`). It emits `0x4205` at the point
  `piVar12 == &UNK_00427728` via `FUN_002adfa0(ev,0x4205,0x8188000,0,0,0)`, plus a SECOND 0x4205
  event (flag=1) when the event's `+8 & 0x8000` is set — i.e. the two rotation half-steps/directions.
- **Control code table @ `0x427700`** (10 × u32): `0x4405 0x440c 0x4403 0x4402 0x420b 0x4206`
  **`0x4205`**`(@+0x18) 0x4488 0x430c 0x420c`; `0x427728` begins a `PcControlLedData` object
  (RTTI `N2ui16PcControlLedDataE` @0x427740). Decoder matches frame elements against these.
- **STILL BLOCKED:** synthesizing the encoder's subucom frame field. `FUN_002adfa0` did NOT fire for
  any analog `A <off> 0200` injection across rbp off `0x44–0x60`, and `A`/`b` injection into the
  browser dispatcher collapses to fixed codes (`0x409`/`0x410a`) regardless of offset. The subucom
  frame is a **structured per-control protocol** (descriptor/code-table + per-element change word
  `&0x8000` + per-word byte-swap + CRC), NOT a flat byte array — so you can't poke a byte and get the
  encoder. **Next step to finish:** decode `FUN_0028a7b8`'s pre-line-646 frame→state mapping and
  `FUN_00286e1c(dev,idx)` to learn the encoder control's exact frame field (offset + signed-counter/
  quadrature encoding); then extend the QEMU subucom harness to present that field as a changing
  signed value. Clean probe to confirm once you have a candidate: `Z1,2adfa0,4` must fire with the
  injection (it's encoder-only), and the on-screen highlight must move ARTIST→ALBUM.

## ★ BREAKTHROUGH: USB1/USB2 are MAIN-UCOM GPIO keys, NOT subucom (2026-11-09)
From the **XDJ-RX service manual** (`~/Downloads/da43a7.pdf`, 210pp). The unit has
**two MCUs**, and the key routing is split:
- **EUP UCOM** (= the subucom SSI I inject, `/dev/subucom_spi1.0`): matrix keys incl.
  `BROWSE, TAG LIST, INFO, MENU, BACK, LOAD1, LOAD2, BROWSE PUSH`, all deck/mixer
  analog+buttons, jogs. (So BROWSE itself *is* a subucom key.)
- **MAIN UCOM** (= the i.MX6 reading direct GPIOs via **`/dev/gpiodrv`**): the
  **`DPNL1` source-select keys `rekordbox, MIDI, USB1, USB2`**, plus `DPNL2 DECK1/2,
  TIME MODE`, and the jack Phono/Line switches. These are discrete switches
  (`S5601/2/4`, CN5601) pulled to +5 → **idle HIGH, pressed LOW**.

**This is why no subucom frame bit could ever be USB1/USB2** — they are not in the
subucom frame at all. All the panel-injection work above applies only to EUP keys.

### gpiodrv mechanism (rbp RE, verified live)
- `GpioManager` ctor `FUN_0002be28`: `open("/dev/gpiodrv")`, **`lseek(fd, gpio_number)`**
  (the GPIO number IS the file offset), `read(fd,1)` → 0/1 level, `ioctl(…,0x4004f101,…)`.
  So each rbp gpiodrv fd's `fdinfo` **`pos`** = the Linux GPIO number it watches.
- Live, rbp watches 18 GPIOs: `117 118 119 120 126* 134 135 146 149 158 159 160 161
  162 163 164 165 204*` (*=USB1/USB2 **detect** = GPIO4_IO30/ GPIO7_IO12, driven by
  patch 06).
- **`FUN_00274a38`** builds the **8 MAIN-UCOM direct keys** via `FUN_0002be28` with GPIO
  numbers `0x77,0x78,0x9f,0xa1,0xa0,0x9e,0x95,0x92` = **119,120,159,161,160,158,149,146**
  (GPIO4_IO23/24, GPIO5_IO31/30/21/18, GPIO6_IO00/01). The 8 keys are {rekordbox, MIDI,
  USB1, USB2, DECK1/2, TIME MODE, Phono1/Line1 SW, Phono2/Line2 SW}. USB1/USB2 are 2 of
  these 8 — identify by driving each in QEMU (see below) and watching for the USB browser.

### ✅ CONFIRMED live (2026-11-09): the 4 DPNL1 source keys
Built a QEMU MAIN-UCOM key hook (drive GPIO N low=pressed via `echo N > /tmp/mainkey`)
and pressed each candidate. **The browser opens from these GPIO presses** — proving
USB1/USB2 are the MAIN-UCOM GPIO keys, exactly as the service manual says:
- **GPIO 158 (GPIO5_IO30) = USB2** → center shows `⇔ NO USB( USB2 )` browse screen.
- **GPIO 160 (GPIO6_IO00) = USB1** → center shows `⇔ NO USB( USB1 )` browse screen.
- **GPIO 159 (GPIO5_IO31) = rekordbox**, **GPIO 161 (GPIO6_IO01) = MIDI** (`USB MIDI /
  MIXER MODE: XDJ-RX / NOT CONNECTED`). The 4 map sequentially to CN5601 pins 7-10
  (KEY_USB2, KEY_RB, KEY_USB1, KEY_MIDI).

Full 8-key GPIO identity (all tested live via /tmp/mainkey):
| GPIO | i.MX6 pad | key | confirmed by |
|---|---|---|---|
| 158 | GPIO5_IO30 | **USB2** | `NO USB( USB2 )` browse screen |
| 159 | GPIO5_IO31 | **rekordbox** | source screen |
| 160 | GPIO6_IO00 | **USB1** | `NO USB( USB1 )` browse screen |
| 161 | GPIO6_IO01 | **MIDI** | `USB MIDI / MIXER MODE: XDJ-RX` |
| 119 | GPIO4_IO23 | ch1 Phono/Line SW | DECK1 `INPUT : LINE/PHONO` |
| 120 | GPIO4_IO24 | ch2 Phono/Line SW | DECK2 `INPUT : LINE/PHONO` |
| 146 | GPIO5_IO18 | DECK1/2 (tentative) | — |
| 149 | GPIO5_IO21 | TIME MODE (tentative) | time-area/`A.CUE` change |
(146 vs 149 for DECK1/2 vs TIME MODE is a best guess — the Phono/Line input state
accumulates across presses, blurring the diff; refine later.) The web panel
(tools/xdj-panel) wires the 4 source keys + DECK1/2 + TIME MODE through `/mainkey`.

So the browser DOES open via the real USB1/USB2 buttons. (The list shows "NO USB" only
when the library hasn't registered that boot — the separate flaky USB-accept issue; with
a clean boot + late insert the USB2 list populates.)

### Emulating a MAIN-UCOM key press (the accurate fix)
GPIO number N → bank `N/32+1`, line `N%32` → QEMU `s->gpio[N/32]`, pin `N%32`. Press =
drive the input LOW (`qemu_set_irq(qdev_get_gpio_in(gpio_bank, pin), 0)`), release = HIGH,
exactly as patch 06 does for the detect lines. These lines must idle HIGH at reset (as
patch 04 does for the detect lines) or rbp sees them stuck-pressed at boot. Next: a QEMU
hook to drive any of the 8 key GPIOs from a `/tmp` trigger, then press the one that opens
the browser = USB1/USB2.

## ⚠ (superseded) browser PAGE not foregrounded — only relevant to EUP/BROWSE key
The notes below concern the EUP-side BROWSE key / in-browser nav; USB1/USB2 open the
browser via the MAIN-UCOM GPIO path above, which is the user's actual button.

## ⚠ Current blocker: the browser PAGE is not foregrounded (USB IS registered)
**Update 2026-11-09:** the old hypothesis ("USB not registered") is DISPROVEN. With the
patch-08 hub topology + a genuine baken `export.pdb`, rbp **does** register the USB as a
browsable source and parses `export.pdb` (verified live; see docs/usb-slot-topology.md).
Yet panel nav presses still open nothing — because the browser is a self-contained,
**flag-polled page** that only processes keys while it is the foreground/updating page,
and on the deck screen it is **not ticking**.

### Browser key subsystem (rbp RE, fully mapped)
- Dispatcher **`FUN_000e1334`**: `for i in 0..15: if flag[i] != 0 then handler[i](&state[i])`.
  It is a browser-page **virtual** (no direct callers) — called from the page's update()
  only while the browser page is active.
- Table at `0x17f3140` (populated by `FUN_000e11a4`): handler ptr at `0x17f3140+0x10*i`,
  pressed-flag at `handler-4` = `0x17f313c+0x10*i`, per-key state at `handler-0xc`.
- Source keys call `FUN_000d5fcc(N)`: USB=`0xe0e9c`→`(2)` (flag `0x17f315c`),
  `0xe0f18`/`0xe0f94`/`0xe1010` = the other three sources (SD/LINK/rekordbox).
- **Proof it's gated:** writing `0x17f315c=1` via gdb is **never consumed**
  (flag stays 1); a bp on `FUN_000e1334` **never fires** on the deck screen.

### Input architecture — fully mapped (2026-11-09, rigorous)
Confirmed live + static, authoritative:
- **`IKeyManager::sendKey` = `FUN_002a47c0`** is the universal key entry (throws the
  literal `"IKeyManager::sendKey"`). It is the receiver-observer `vtable[8]` target.
- A real single-bit button DOES produce a distinct code here: **tempo-range `[0x12]`
  bit3 → code `0x4108`** (verified repeatably). Injecting all 8 bits of a byte (`ff`)
  instead triggers that byte's analog interpretation (code `0x410a`) and masks the
  button — so map buttons with **single-bit** injects, well spaced (the harness one-shot
  needs the ~30-frame press to release before the next, or the new inject is dropped and
  the stale code re-read; that is the `0x430e`/`0x410a` "stuck" artifact in batch sweeps).
- sendKey dispatches to the focused key target's `onKey` (`FUN_002ac848` = deck/LED
  feedback: maps `0x410a`/`0x420c`/`0x5112`/… to `FUN_002ac1d4` deck-value updates) AND
  broadcasts to observer lists (one is the active UI page).
- **Browser in-page key dispatcher `FUN_000e1334` is gated behind the browser page being
  foregrounded** — it never ticks on the deck screen (gdb bp never fires), setting a key
  flag there is inert, and a write-watchpoint on the USB flag `0x17f315c` never fires from
  the deck screen (the code→flag setter is gated too). So the deck→browser transition is
  upstream, in the main-UI page manager, triggered by the deck page's handling of the
  USB/BROWSE key. (Browser init `FUN_000d5714` populates the key table `FUN_000e11a4` then
  allocates its object via `FUN_00115f5c`; `FUN_00122d80` there is only the alloc-fail
  error logger — not a message bus, earlier guess retracted.)

**NEXT (authentic open path):** identify the deck page's key observer (one of the UI
observers sendKey broadcasts to, distinct from the `FUN_002ac848` deck-LED handler) and
the USB/BROWSE code it switches pages on. Cleanest method on the now-verified-clean state:
breakpoint the browser dispatcher `FUN_000e1334` and inject nav buttons **one at a time,
well spaced** (single bit, ≥0.8 s apart to avoid the drop/stale artifact); whichever press
makes `FUN_000e1334` begin ticking opened the browser — that panel bit is USB/BROWSE.
Then the in-browser nav keys (table `0x17f3140`, USB flag `0x17f315c`) drive scroll/LOAD.
gdb helpers: scratchpad/{sendkeymap.py,robustprobe.py,watchusb.py,browseopen.py,flagcheck.py}.

### The real gate (next)
Opening the browser = the **main-UI page manager** switching the foreground page to the
browser (so its update()→`FUN_000e1334` starts ticking). That switch is triggered by the
deck-screen USB1/USB2 key through `IKeyManager`, handled in the **deck/page framework**,
a different module than the browser-key table above. Map the deck page's key handler and
its page-switch call (or find the page-manager "current page" pointer and foreground the
browser page directly) — that, not the panel bit, is what opens BROWSE→LOAD.
gdb helpers: scratchpad/{gdbcap.py,setflag.py,usbkey.py}.

### Live KeyManager trace — findings (2026-11-09)
- **`IKeyManager::sendKey` = `FUN_002a47c0`** (confirmed: it throws the literal
  `"IKeyManager::sendKey"`). The key **code = param_2 (r1)**, u16. It is the receiver
  observer `vtable[8]` target (`gdbcap` resolved the runtime ptr to 0x2a47c0). This is the
  real global-key entry — NOT `FUN_00222188`, `0x2832b8`, or `FUN_0028cea4` (all three
  breakpointed live on panel injects and **never fired** — matching the 2026-11-06 notes).
- **The old `[0x15]..[0x1b]` "global button" map is wrong.** Via `sendKey` (bp r1):
  at idle, `sendKey` is **quiet** (no events). But injecting ANY single frame byte
  `0x04..0x61` as `b <off> ff` yields exactly one global key event, always code
  **`0x410a`** — never a discrete nav/source code (BROWSE/USB/LOAD/MENU). So the
  global-section decoder collapses a single-bit frame change into one analog-style
  `0x410a` event; it does not expose the 16 nav keys as single bits.
- Implication: the nav/source buttons need a **specific multi-field frame value** (not a
  lone bit), or route through a different observer. The 16 browser KEYCMD keys
  (DECK_SELECT…USB/SD/LINK/rekordbox/LOAD/Encoder) have unknown numeric sendKey codes.
  Next: trace the global-section decoder `FUN_00282a98` to learn how frame bytes map to
  nav codes and what value yields the USB/BROWSE code; then breakpoint the active deck
  page's key handler to find its code→page-switch. Convergent open-test ready:
  scratchpad/browseopen.py (inject a control with a bp on `FUN_000e1334`; it reports if
  the browser page starts ticking). Helpers: scratchpad/{sendkeymap.py,fullsweep.py}.
