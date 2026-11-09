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
