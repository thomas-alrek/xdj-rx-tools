# rbp (main app) architecture & the panel-MCU control protocol

Reverse-engineering notes for `/root/pioneer/rbp`, the XDJ-RX's main application —
the program that draws the UI, runs the decks, and talks to the hardware. This
documents its structure and the physical-control input path, to support emulating
the controls and injecting new features (exFAT, FLAC, Pro DJ Link, …).

- **Binary:** `pioneer/rbp`, ARM 32-bit ELF, dynamically linked (`/lib/ld-linux.so.3`),
  C++ (GCC), ~4.6 MB, built 2017-10-18. Stripped of debug symbols, but C++ RTTI
  type-info names and `__func__`/assert strings survive, which is how the structure
  below was recovered. Load base `0x8000` (so `vaddr = file_offset + 0x8000`).
- **Analyze with:** REA/Ghidra on the extracted binary (xrefs resolve well — rbp
  uses PC-relative literal pools, unlike the kernel). Addresses here are vaddrs.

## 1. High-level structure

Recovered from embedded `../../../Source/...` paths and `ui::`/other RTTI names:

| Area | Source root | Role |
|------|-------------|------|
| **Nexus / DB** | `Source/Allinone/Nexus/DB/cache/cue/` | track database + cue/analysis cache; `disc_*`, `msc_anlz_*_usb` (USB media analysis), `ex_fsys` |
| **Nexus / GUI** | `Source/Allinone/Nexus/GUI/suntec/glib/` | the GUI toolkit ("suntec glib") on DirectFB; `DS_HW_Glib3_DFB.c` is the DirectFB HW plugin |
| **UI** | `Source/Allinone/UI/` | screens, TestMode, DirectFbIf; the `ui::` C++ namespace (242 classes) |
| **Engine / PlayEngine** | `Source/Engine/PlayEngine/` | the DJ deck engine: `playengine_Player`, `playengine_LoopQuantize`, DSP `TimeStretch` |
| **Common** | `Source/Common/` | `native/linux` (GpioManagerLinux), `reader/FileReader` (PageBuffered / Remote input streams) |

Rendering is DirectFB software (confirmed separately); the Vivante GPU (libGAL) is
only used for an unreached `g2d_blit`. The GUI is "suntec glib" on top of DirectFB.

## 2. The physical control surface → app path

The XDJ-RX has **no touchscreen**; every control (buttons, jog wheels, tempo
sliders, the crossfader, EQ/trim/FX knobs) is read by a **front-panel
microcontroller ("Subucom")** and delivered to rbp over **SPI**:

```
physical controls ─► Subucom panel MCU ─► SPI (ECSPI2 / SPI bus 1)
   ─► kernel subucom_spi driver ─► /dev/subucom_spi1.0
   ─► rbp: PanelComPeerLinux (reads/writes the device)
   ─► PanelComReciever (parses RX frames) ─► panel_protocol::SubRxData
   ─► KeyManager / KeyInput / PlayEngine (dispatch to UI + decks)
```

### Key classes (`ui::` namespace)

- **Transport:** `PanelComPeerLinux` (opens `/dev/subucom_spi1.0`, does the SPI
  transfers; has a `MemorandomTimer`), behind `IPanelComPeer`/`PanelComPeer`.
- **Controller/receiver:** `PanelComController`, `PanelComReciever`
  (`IPanelComReciever`).
- **Protocol:** `panel_protocol::Base`, `::TxData`, `::SubTxData`, `::SubRxData`.
  - **TX** (app → MCU): `SubTxData` (LED/display state back to the panel; note the
    assert `"SubTxData() not 4bytes alignment 0x%x"` — TX frames are 4-byte aligned).
  - **RX** (MCU → app = control state): `SubRxData`, parsed via a `SubRxFormat`
    interface with sections **CommonData**, **FmtData**, **OtherData**, and
    **MixerData** (`refCommonData() / refFmtData() / refOtherData() / tMixerData()`),
    plus an XOR checksum (`SubRxData::xor()`, `getCrcAdress()`). `SubRxFormat` is an
    abstract base (its accessors are `throw`-stubs at ~`0xf2f0`); the concrete byte
    layout lives in a derived class's vtable — **TODO: decompile that subclass to
    get exact section offsets/sizes.**
- **Decoded control data:** `PcControlKeyData` (buttons/switches),
  `PcControlJogData` (jog wheels), `PcControlTempoSliderData` (tempo faders),
  `PcControlKeyServer`, `KeyManager`, `KeyInput`, `XFaderInnards` (crossfader),
  `TempoSlider`, `JogSpeedGuesser`. Self-test modes exist and are useful oracles
  for the mapping: `KeyTestMode`, `SwitchTestMode`, `JogTestMode`.

### Boot handshake (blocks full boot after galcore)

At startup rbp waits for the MCU's `ui::StaticInfomation` exchange; if it doesn't
complete it logs `"Subucom StaticInfomation is not completed. retry..."`. The QEMU
SSI slave stub currently answers `0xff`, so this handshake must be modelled for the
app to proceed past panel init. **This is the same protocol as button emulation**,
so one RE effort serves both. (Device: ECSPI2 attached in `hw/arm/sabrelite.c`,
patch 03; the Subucom SSI slave is the XDJ-RX panel MCU.)

## 3. Audio decode (for FLAC, feature F)

- Formats handled: **.MP3 (via `libmpg123.so.0`), .AAC, .M4A, .AIFF, .WAV** — **no
  FLAC**. Two decoder slots: `PLAYER_DECODER_0/1` (one per deck).
- Recording writes `/PIONEER REC/REC%03d.WAV`.
- Adding FLAC = provide a FLAC decoder and register it in the player's
  format-selection path (where the extension/codec is chosen). Investigate
  `playengine_Player` and the decoder factory next.

## 4. USB media (for exFAT, feature D)

- Two ports (USB1/USB2); mount logic logs `"mount success"/"mount falied"` and a
  per-player mapping `"PlNo=%d,Dev=%d,Lett=0x%x(%c),Mount=%d,PcLett=0x%x(%c)"`.
- USB media is analysed into the cue/DB cache (`msc_anlz_*_usb.c`).
- exFAT support is a **kernel** capability (the 3.0.35 kernel lacks it); rbp's
  mount path then needs to accept the exFAT volume. (Driver/mount, not an rbp change.)

## 5. Injecting new features later

- rbp is **dynamically linked**, so `LD_PRELOAD` shims are the least invasive hook
  (intercept libc/`libmpg123`/DirectFB calls) — but that is a rootfs change, which
  conflicts with the "stock firmware" constraint; prefer it only for development.
- **Input injection** for the emulator: have the QEMU Subucom SSI slave emit
  `SubRxData` frames encoding button/jog/fader state (needs the concrete frame
  layout from §2). This is the clean, firmware-stock way to drive the UI in QEMU.
- **Codecs:** a FLAC decoder behind the player's decoder factory (§3).
- Binary patching of rbp is possible but is a firmware modification; keep QEMU-side
  and kernel/driver approaches first.

### Concrete details recovered so far

- `PanelComController` ctor is `FUN_0021a4ac` (rbp vaddr). It embeds a **`SubTxData`
  TX buffer of 24 bytes (`0x18`)** (`memset(this+0x6c, 0, 0x18)`) and asserts that
  buffer is 4-byte aligned. SPI is full-duplex, so the **RX frame is very likely
  also 24 bytes** exchanged per transfer (TX app→MCU LED/state ⇄ RX MCU→app control
  state). Config bytes set in the ctor: `+0x5e = 0x42`, `+0x5d = 6`, `+0x5f = 0`
  (likely a frame/command header — confirm against the SPI transfer length).
- On first construction it registers **4 periodic panel tasks** via `new`(0x68) +
  `FUN_0002be28(obj, id, 1, 100, 0, 0x1e)` with ids `0xa2,0xa3,0xa4,0xa5` (panel
  poll/refresh timers; `100`/`0x1e` look like periods).

### Receiver dispatch chain (recovered)

- `PanelComReciever` vtable at rbp `0x4272cc` (primary) / `0x4272e0` (secondary,
  `IPanelComReciever` interface thunks). RTTI name `N2ui16PanelComRecieverE`
  @`0x4272f0`, typeinfo @`0x427308`. Methods: `0x285b6c`/`0x285df0` (destructors),
  **`0x285160` = the single virtual (`onReceive`-style callback)**.
- `0x285160` is a thin forwarder: for event type 0 it calls the real handler
  (`bl 0x284d44`), which **dispatches to observer lists** (iterates arrays at
  `this+0x7c/0x84`, `+0x10c/0x114`, `+0x118/0x120`, calling each observer's
  vtable+0xC) and pushes control changes down to the **decks/PlayEngine**
  (`FUN_002ac1d4(deck, index, 100, 0x7f)` for deck indices < 4). So the path is:
  SPI RX frame → onReceive → per-section dispatch → deck/mixer/key observers.
- (Note: Ghidra doesn't auto-disassemble these vtable-referenced targets — reach
  them via `objdump -d --start-address=` on the rbp ELF, base `0x8000`.)

### RX decode architecture (recovered 2026-11-05)

The decode is three layers; to **inject a button** the QEMU SSI slave must
synthesize a valid `SubRxData` frame, and rbp then walks it:

1. **Per-section decoders** — `FUN_00222188`'s callers `0x220314 / 0x220514 /
   0x220780 / 0x220818` (plus `0xd534c / 0xd5520 / 0x208d18`). One per SubRxData
   section (Common/Fmt/Other/Mixer). Each reads the section bytes, diffs vs. the
   previous frame, and for every changed control calls the event dispatcher. **These
   hold the exact byte/bit → control map — decompile them next.**
2. **Event dispatcher** `FUN_00222188(ctlr, deckIdx, evtType, value, extra)` —
   `evtType` is a small enum: **1 = key/button down** (→ key handler `FUN_0028cc18`
   → observers; also `FUN_00271c7c`), **2/3 = toggle / press-state** (stores at
   `+0x67 / +0x65`, fires `0x2000`-flagged observers), **4 = aux byte pair**
   (`+0x69/+0x6b`), **5 = a global toggle** (→ `FUN_002ac1d4(deck,4,0x65,…)`),
   **0 = release**. `deckIdx` selects deck 0/1 via `(deckIdx-1)&1`.
3. **Key handler** `FUN_0028cc18` → iterates observer lists (`+0x7c/+0x84`,
   `+0x10c/+0x114`, `+0x118/+0x120`, each observer's vtable `+0xC`) → decks via
   `FUN_002ac1d4(deck,index,100,0x7f)`.

Decoded control structs (RTTI in rbp): `PcControlKeyData` (buttons),
`PcControlJogData`, `PcControlTempoSliderData`, `PcControlDeckSelectData`,
`PcControlPadData`, `PcControlBfxRotaryData`, `PcControlLongPressData`,
`PcControlLedData`, `PcControlKeyServer`. SubRxData accessors: `refCommonData` /
`refFmtData` / `refOtherData` / `tMixerData` / `getCrcAdress` / `xor` (RTTI
`N2ui14panel_protocol9SubRxDataE` @0x4272a8).

### Plan to implement button injection (next task)
1. Decompile the four section decoders (above) → exact byte offset + bit for each
   button/jog/fader, and the frame header + `getCrcAdress`/`xor` checksum.
2. Confirm the raw 24-byte frame from `PanelComPeerLinux`'s `SPI_IOC_MESSAGE`
   transfer and the `StaticInfomation` boot handshake rbp expects.
3. In QEMU (`hw/arm/fsl-imx6.c` `xdj_subucom_transfer`, currently returns `0xff`):
   drive a real `SubRxData` state block on MISO (handshake + idle), and expose an
   injection path (keymap via the display, a monitor command, or a control file)
   that flips button bits/jog deltas in that block. This stays firmware-stock.
4. This also unlocks **rekordbox LAN link** (feature B) — enabling it is a menu
   action that needs these buttons (`LnkHand` / `(rekordbox)` in rbp).

## Physical control surface (labels, to tag the decoded byte-map)

Target vocabulary for the decode: every `(evtType, deckIdx, index)` /
`(section, byte, bit)` the section decoders produce maps to one of these real
panel controls. Grouped by how they likely appear in the protocol — **buttons**
(digital on/off → Key/Common bit flags, evtType 1/2) vs **continuous** (analog →
Fmt/Mixer bytes) vs **jog** (Other section).

### Per-deck (DECK 1 = left, DECK 2 = right; symmetric)
- Transport buttons: `PLAY/PAUSE`, `CUE`, `DIRECTION/REV`, `SLIP`, `JOG MODE
  (VINYL)`, `BEAT SYNC: SYNC`, `BEAT SYNC: MASTER`, `MASTER TEMPO`, `TEMPO RANGE
  (±6/10/16/WIDE)`.
- Loop/cue buttons: `IN/4BEAT`, `OUT`, `RELOOP/EXIT`, `CUE/LOOP CALL ◄ (1/2X)`,
  `CUE/LOOP CALL ► (2X)`, `DELETE`, `MEMORY`.
- Search buttons: `TRACK SEARCH ◄◄`, `TRACK SEARCH ►►`, `SEARCH ◄◄`, `SEARCH ►►`.
- Performance pads + mode buttons: `HOT CUE`, `AUTO BEAT LOOP`, `LOOP SLICE`,
  `SHIFT`, and pads `A (1/16)`, `B (2/8)`, `C (4/4)`, `D (8/2)` (4 per deck).
- Continuous: `JOG WHEEL` (platter, touch + rotation → Other/jog), `VINYL SPEED
  ADJUST` knob, `TEMPO` slider (fader).

### Center mixer
- Buttons: `BROWSE`, `TAG LIST`, `INFO`, `MENU/UTILITY`, `BACK`, `TAG
  TRACK/REMOVE`, rotary-selector **push** (+ turn = encoder), `LOAD ◄`, `LOAD ►`,
  `SOUND COLOR FX: NOISE / GATE·COMP / CRUSH / FILTER`, `BEAT EFFECTS: BEAT ◄ /
  BEAT ► / TAP(AUTO) / ON·OFF`, `HEADPHONES CUE CH1`, `HEADPHONES CUE CH2`.
- Switches: `DECK1 ↔ PHONO1/LINE1`, `DECK2 ↔ PHONO2/LINE2`, `CROSS FADER CURVE`.
- Continuous: `TRIM` ×2, EQ/ISO `HI/MID/LOW` ×2, `COLOR` ×2, channel faders
  `CH1`/`CH2`, `CROSSFADER`, `HEADPHONES MIXING`, `HEADPHONES LEVEL`, BEAT-EFFECTS
  effect-select + channel-select (`MIC/CF1/1/2/CF2/MASTER`) + `TIME` + `LEVEL/
  DEPTH`, `MASTER LEVEL`, `BOOTH MONITOR`.

### Top panel
- Source/select buttons: `rekordbox`, `MIDI`, `USB1`, `USB2`, `QUANTIZE`,
  `DECK1/2`, `TIME MODE (AUTO CUE)`, `MASTER REC (WAKE UP)`, `TRACK MARK`,
  `USB1 STOP`, `USB2 STOP`.
- MIC: `MIC OFF/ON/TALK OVER` switch; continuous `MIC1 LEVEL`, `MIC2 LEVEL`, mic
  EQ `HI`, mic EQ `LOW`.

(`rekordbox`, `MIDI`, `USB1/2`, `LINK` select the player mode — `rekordbox` +
`LnkHand` is the rekordbox LAN-link path, feature B.)

## Other open follow-ups
- Decompile `PanelComPeerLinux` SPI transfer + `StaticInfomation` handshake
  (what the QEMU Subucom SSI slave must satisfy).
- Map `playengine_Player` decoder selection (for FLAC).

## Panel input path — empirical findings (buttons bring-up)

Live analysis of the running emulator (rbp + /proc + SUBUCOM_TRACE + QEMU
monitor) corrected the earlier assumptions:

- **Buttons come from the subucom MCU over SPI, as 24-byte frames.** Confirmed:
  a guest `dd if=/dev/subucom_spi1.0 bs=24 count=3` clocked exactly a 24-byte
  SPI transaction per read into the QEMU SSI slave (72 bytes for 3 reads),
  MOSI=0x00. So **the kernel SPI path works** — a userspace read triggers one
  24-byte transfer, MISO carries the panel's reply. The QEMU slave currently
  returns 0xff for every byte (invalid frame).
- **`/dev/gpiodrv` is NOT the button path.** rbp's `GpioManager`
  (`common_GpioManagerLinux.cpp`, ctor `FUN_0002be28`) does
  `open("/dev/gpiodrv")`, `lseek(fd, gpio_id)`, `read(fd,1)` = level (0/1),
  `ioctl(0x4004f101,{gpio_id,mode})`. rbp opens ~9 discrete lines this way —
  board signals, not the dozens of DJ controls.
- **The `PanelComController` ctor (`FUN_0021a4ac`)** builds the panel: 4 panel
  GPIOs (ids **0xa2–0xa5**), a **24-byte `SubTxData`** TX buffer (the LED/display
  frame sent to the MCU), vtables `DAT_00420700` / `PTR_LAB_00420620`. The peer
  `PanelComPeerLinux("/dev/subucom_spi1.0", ...)` is built in the big device-init
  `FUN_00245098` (which also builds Settings/LedManager/PowerManager/…).
- **The blocker: rbp never reads the panel.** With no manual read, the SSI slave
  sees **0 frames**, and no rbp thread is blocked on the panel (the 57 threads
  sit in futex/nanosleep/poll/skb_recv — none in a char-device read). So the
  panel poll is **gated by a flag/precondition that fails under emulation**
  (board-detect GPIO and/or the StaticInfomation bootstrap), and rbp quietly
  leaves the panel disabled. The read trigger lives in the controller's vtable
  update method (not yet pinned).

### Plan to make buttons work
1. **Pin the gate** — find the controller update/poll method (via `DAT_00420700`
   / `PTR_LAB_00420620`) and the exact precondition that disables the read
   (candidate: one of GPIO 0xa2–0xa5 read as "panel present", or StaticInfomation
   != 0xffff). A QEMU gdbstub breakpoint on the panel read path is the fast way.
2. **QEMU side** — make the gate pass (assert the right GPIO / satisfy the
   StaticInfomation handshake) and extend the `xdj-subucom` SSI slave to return
   valid **24-byte** frames: a StaticInfomation reply at boot, then idle frames,
   then injected button/jog/fader frames.
3. **Frame format** — RE the 24-byte RX frame (sections via
   refCommonData/refFmtData/refOtherData/tMixerData, the XOR/getCrcAdress check,
   and StaticInfomation layout) to encode controls correctly.
4. **Injection** — map host keys (QEMU input) to button bits in the synthesized
   frame; tie each to the physical labels catalogued above.

Note: the SSI-slave SubRxData protocol work done earlier is still needed for
step 3, but step 1 (the read gate) must be solved first or rbp never reads it.

## Quick-win input path: the DebugSub UDP console (allinone_debug::DebugSub)

rbp has a built-in debug injection channel that **bypasses the gated SPI panel
path** entirely:

- A thread `DebugSub` (`allinone_debug::DebugSub`, run loop `FUN_0002b260`) binds
  a **UDP socket on `127.0.0.1:20000`** (port from DebugManager config, default
  20000) and loops on `recvfrom`. It's **live at boot** (confirmed: `netstat -uln`
  shows `127.0.0.1:20000`). Also-listening: UDP `0.0.0.0:50000/50001/50002`.
- Each received **text line** (\r/\n terminated) is wrapped into a
  `common::CommonMessage` (type **0x1234**, tag 0x74583, parser `FUN_0002b0e8`),
  posted to rbp's message bus, and the handler's reply (waited up to 10 s) is sent
  back over UDP. Unhandled/empty → a 1-byte `'1'` ack.
- **Sending from the guest:** busybox `nc` is TCP-only and there's no socat/python,
  BUT the rootfs has `/bin/bash`, which supports `/dev/udp`:
  `bash -c 'exec 3<>/dev/udp/127.0.0.1/20000; printf "%s\n" "$0" >&3; cat <&3 >/tmp/r & p=$!; sleep 11; kill $p' "<cmd>"`.
  Verified end to end: `help` → 1-byte `'1'` ack (channel works; "help" is not a
  recognised command).

**Remaining piece: the command vocabulary.** The text→action mapping is behind the
CommonMessage(0x1234) dispatch (generic C++ message bus), so static tracing is
slow. Next: pin the 0x1234 handler via a QEMU gdbstub breakpoint on `FUN_0002b0e8`
(known-hit when a command arrives) and follow the message to the command parser —
that reveals the command keywords. Then: map host keys → UDP lines (a small guest
TCP↔UDP bridge reachable via QEMU hostfwd, since DebugSub binds 127.0.0.1).

## SPI panel read path — architecture (why rbp doesn't read the panel)

Classes (RTTI): `ui::PanelComController`, `ui::PanelComReciever`,
`ui::PanelComPeerLinux` (SPI peer for `/dev/subucom_spi1.0`), and the nested
`ui::PanelComPeerLinux::MemorandomTimer`.

- The SPI panel is **polled by a periodic timer**, `MemorandomTimer`
  (`timerCallback` = `FUN_0028a7b8`), driven by rbp's JUCE timer thread —
  **not** a blocked read thread. That's why no thread was ever seen blocked on
  the panel, yet 0 SPI frames were clocked.
- `PanelComPeerLinux` ctor `FUN_00282214` builds the peer + `MemorandomTimer`
  but does **not** open the device (lazy). The device path is registered
  (`FUN_002816c8`, `"/dev/subucom_spi1.0"`, buf=100).
- `FUN_0028a7b8` reads the per-control state arrays and dispatches events by the
  same codes the `button` command uses (0x410d loopin, 0x420c decksel, …) →
  the shared dispatcher `FUN_002482d0()->(+8)` / `FUN_00222188`. So the panel's
  **output/dispatch** path is the same one the debug `button` command drives.

**The gate is on the read side.** rbp never clocks the SPI, so either (a) the
`MemorandomTimer` is never started, (b) it fires but the transfer/open of
`/dev/subucom_spi1.0` is skipped/gated, or (c) the open fails and the peer
disables itself. Since `button` (which bypasses the read and dispatches directly)
also appears inert on the idle screen, the whole panel subsystem may be disabled.

**Next (gdbstub):** breakpoint `FUN_0028a7b8` (does the timer fire?) and the
peer's SPI transfer/open (is the device ever opened?) to pin which of a/b/c it is.
