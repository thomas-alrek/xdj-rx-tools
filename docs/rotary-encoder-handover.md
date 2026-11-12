# Handover: browse rotary + push ("select tracks" knob) in the panel

**For:** a focused session that ONLY needs to make the central **browse rotary** (rotate =
move the library selection) and its **push** (= select/enter) work from the virtual panel.
Everything else works: the emulator boots to the live UI, USB is solved (a library is
loaded), MAIN-UCOM source keys and the mapped analog/button controls work. This is the one
control the user explicitly needs for track selection.

**HARD CONSTRAINT (unchanged):** do NOT patch the `rbp` binary. QEMU-side harness changes
only (the sub-ucom injection harness in `hw/arm/fsl-imx6.c`), so vanilla rbp still runs.

## 0. One-paragraph status
The entire firmware mechanism is reverse-engineered and the browser is identified. What's
missing is **how to synthesize the encoder's field in the raw sub-ucom frame** — because the
frame is a *structured per-control protocol*, not a flat byte array, so the existing
`/tmp/subucom_ctrl` "poke a byte/analog at offset X" harness cannot produce a browse-encoder
event. Your job: decode the frame→state mapping in `FUN_0028a7b8` (+ `FUN_00286e1c`) to find
the encoder control's exact frame field (offset + signed-counter/quadrature encoding), then
extend the QEMU sub-ucom harness to present that field as a changing signed value, and wire a
rotary + push control into `tools/xdj-panel/panel.html`.

## 1. What is SOLID (RE complete — don't re-derive). rbp = `build/extract/rbp`, REA/ghidra.
The browse-rotary data path, fully traced:
- **Active browser = the legacy `@Ui_BrowseKey` browser.** Its per-event handler/dispatcher is
  **`FUN_0026ae7c`** (a PAGE vtable virtual; no direct callers). CONFIRMED active on a clean
  instance: it fires on injected panel events while the `ARTIST/ALBUM/…` category list is on
  screen. (An earlier session wrongly concluded it was "a different browser" — that was a
  degraded injection harness, see §4.)
- **EncoderRotate handler = `FUN_000e159c`** (logs `@Ui_BrowseKey> KEYCMD EncoderRotate`, str
  `0x39dd3c`). Moves the list cursor by the signed rotation accumulated in **`DAT_017f3234`**,
  dispatching it to the mode-specific nav fn (`FUN_000d6950/69f0/6ac4/6b4c/6cb8/6bdc/6c84/6d4c`).
- `DAT_017f3234` += signed delta via **`FUN_000e12e0(delta)`**; `delta = DAT_0107be1c`.
- `DAT_0107be1c` set by the dispatcher **`FUN_0026ae7c`** on **code `0x4205`**, reading a
  **signed delta word at `event+0xc`** (`ldr r1,[r4,#0xc]; bl FUN_000b36e0`). So EncoderRotate
  is code **`0x4205` carrying a signed delta** — NOT a button bit, NOT `0x6010` (an old wrong label).
- **EncoderPush** name str `0x39db94`; its handler is one of the dispatcher's button codes
  (`0x201 0x202 0x203 0x204 0x208 0x209 0x20a 0x420b 0x420c 0x420d 0x4206`) — not yet pinned.
  (`0x20a` is confirmed = MENU, so Push is one of the others.)

The event SOURCE (frame decode → event):
- **Frame decoder = `FUN_0028a7b8`** (REA takes bare hex as FILE OFFSET +0x8000, so decompile
  it as REA `0x282a98`; its VA is `0x28a7b8`). It walks per-control descriptors; for the encoder
  it emits `FUN_002adfa0(ev, 0x4205, 0x8188000, 0,0,0)` at the point `piVar12 == &UNK_00427728`,
  plus a SECOND `0x4205` event (flag=1) when `*(ev+8) & 0x8000` — i.e. the two rotation
  half-steps / directions.
- **Encoder event constructor = `FUN_002adfa0`** — builds the 0x24-byte event with code `0x4205`.
  It is used **ONLY for the encoder** (tempo/buttons use the sibling `FUN_002adee8`), and is
  **silent at idle**. ⇒ `Z1,2adfa0,4` (gdb) is your clean, encoder-only oracle: when your
  injection makes it fire, you've produced a rotation. Its args carry the direction.
- **Control code table @ `0x427700`** (10 × u32): `0x4405 0x440c 0x4403 0x4402 0x420b 0x4206`
  **`0x4205`**`(@0x427718) 0x4488 0x430c 0x420c`; `0x427728` begins a `PcControlLedData` object
  (RTTI `N2ui16PcControlLedDataE` @0x427740). The decoder matches frame elements against this set.
- The tempo-fader section handler is **`FUN_0028411c`** — note it HARDCODES code `0x410a` and
  tracks its own previous value at `+0x74` to compute a delta (the encoder is structurally
  similar: a value with delta-tracking, not a momentary bit). This is why lone-bit injection
  always collapsed to `0x410a`.

## 2. THE REMAINING TASK (what actually unblocks it)
Find the encoder control's **raw sub-ucom frame field** and its encoding, then present it.
1. Decode **`FUN_0028a7b8`** lines before the emit (the frame→state setup, roughly the first
   ~600 pseudocode lines) and **`FUN_00286e1c(dev, idx)`** (called at the top of each outer
   iteration) to learn how a raw frame byte range maps to the control whose code is `0x4205`.
   The decompile is large (~62 KB) — REA saves oversized results to a file; read it with
   `jq`/python, grep for `0x4205`, `UNK_00427728`, `FUN_00286e1c`, `piVar11/piVar12`.
2. From that, get the encoder's **frame offset + representation** (likely a signed counter byte,
   possibly 2-phase/quadrature given the "second event when &0x8000" branch). Convert to the
   harness's rbp-buffer coordinate: the harness builds `rbp[]` then byte-swaps per word into the
   frame the firmware reads — `s->frame[i] = rbp[(i & ~3) + (3 - (i & 3))]` (so frame offset F ⇐
   rbp offset `(F&~3)+(3-(F&3))`), and recomputes the CRC over bytes 0–96. See the inject block
   in `build/qemu-src/qemu-11.1.0/hw/arm/fsl-imx6.c` (search `subucom_ctrl`).
3. Extend the harness with a dedicated encoder injector that presents a **changing signed value**
   in that field (a real encoder sends increments; a static hold = no rotation). E.g. a new
   `/tmp/subucom_enc` that advances the counter by ±N per step, or an `E <steps>` spec.
4. Confirm with the oracle: `Z1,2adfa0,4` must fire on your injection, AND the on-screen
   highlight must move `ARTIST→ALBUM` (screendump). Then find EncoderPush's code the same way
   (bp `FUN_0026ae7c`, press candidates, watch which button code enters the category) and wire
   both into `tools/xdj-panel/panel.html` (a `type:'rotary'` for rotate, a `type:'btn'` for push).

## 3. How to run / drive (shared with the USB handover — see docs/usb-accept-handover.md §1)
- Launch command, endpoints (`/tmp/d_mon4.sock` monitor, `/tmp/d_con4.sock` serial, `tcp::1234`
  gdb), the `con4.py` guest-console helper, the gdb `G` class, and the screendump one-liner are
  all in `docs/usb-accept-handover.md` §1 — reuse them verbatim.
- Panel server: `python3 tools/xdj-panel/server.py` → http://127.0.0.1:8765 . It writes panel
  actions to `/tmp/subucom_ctrl` (sub-ucom) and `/tmp/mainkey` (MAIN-UCOM GPIO). Inject via
  `POST /ctrl` body `"B <off> <bit>"` (hold bit) / `"A <off> <val16>"` (hold analog) / lowercase
  = one-shot; `POST /release` clears. These are rbp-buffer offsets + the per-word byte-swap above.
- The library must be loaded to test navigation. USB is solved: boot hub-only, wait for rbp's
  `UsbMountManager` thread (`/proc/<pidof rbp>/task/<tid>/wchan == poll_schedule_timeout`), then
  `device_add usb-storage,bus=usb-bus.1,port=1.1,drive=usbstick,id=xdjusb,removable=on` + `touch
  /tmp/usb2_insert` (patch 10, FPRES-only). Do NOT cycle device_del/add on a live instance
  (wedges Host1 — usb-accept-handover §0c). The screen should show `USB1`/`USB2` + the category
  list with artist `BITCRUSHR`.

## 4. RULED OUT / gotchas (don't repeat these)
- **Lone-bit and single-offset analog pokes do NOT produce the encoder.** `b <off> ff` collapses
  to `0x410a` (tempo); `A <off> 0200` across rbp `0x00–0x60` never fired the encoder ctor
  `FUN_002adfa0` and made the dispatcher emit a fixed `0x409`. The frame is structured; you must
  target the encoder's actual field, found via §2.
- **The control-map's "frame region" offsets are a DIFFERENT coordinate** than the harness
  `B <off>` rbp offset (e.g. the harness MENU anchor is rbp `0x1b`→`0x20a`, but the map labels
  MENU "0x5f"). Trust the RE + the byte-swap formula in §2, not the map's offset column.
- **gdb hygiene:** clear ALL `Z1` breakpoints between probes — a leftover bp at `sendKey`
  `0x2a47c0` makes every `continue` stop there and report its `r1`, which masquerades as a stuck
  code and wasted a lot of time. Verify with an idle `continue` (should time out = quiet).
- **Don't mass-inject.** Hundreds of rapid injects degrade the sub-ucom harness into returning
  stuck/offset-independent codes (the "~150-inject quiescence" behaviour). Probe sparingly with
  releases between; prefer the gdb oracle over screendump sweeps.
- **REA address quirk:** bare hex to procedure/decompile tools = file offset (+0x8000 bias);
  `read_bytes`/`xrefs`/gdb use real VAs. Decompile by VA via `resolve_containing_procedure`.

## 5. Pointers
- `docs/xdj-rx-control-map.md` — the "Browse encoder (rotary + push) RE" section (sessions 2 & 3)
  has the full chain, the code table, and the blocker, with addresses.
- `docs/usb-accept-handover.md` — shared run/launch/helpers (§1), USB status.
- `~/Downloads/da43a7.pdf` — XDJ-RX service manual (panel/MCU wiring).
