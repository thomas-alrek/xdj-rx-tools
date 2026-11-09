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

### The real gate (next)
Opening the browser = the **main-UI page manager** switching the foreground page to the
browser (so its update()→`FUN_000e1334` starts ticking). That switch is triggered by the
deck-screen USB1/USB2 key through `IKeyManager`, handled in the **deck/page framework**,
a different module than the browser-key table above. Map the deck page's key handler and
its page-switch call (or find the page-manager "current page" pointer and foreground the
browser page directly) — that, not the panel bit, is what opens BROWSE→LOAD.
gdb helpers: scratchpad/{gdbcap.py,setflag.py,usbkey.py}.
