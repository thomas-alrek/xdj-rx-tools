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

### gdbstub result: the panel poll timer is never started (confirmed)

Pinned with a QEMU gdbstub breakpoint on the `MemorandomTimer::timerCallback`
(**`0x282484`** — note `FUN_0028a7b8` is only the dispatch helper it tail-calls):
with rbp **fully booted** (UI painted), the callback **never fires** in 60 s and
0 SPI frames are clocked. So of the three candidates it is **(a): `startTimer`
on the panel `MemorandomTimer` is never called.** rbp constructs the panel
(peer + timer at peer+0x2c, receiver, controller) but never starts polling.

Next: find the `startTimer` call site for the panel timer and the precondition
that skips it in the emulator (candidates: a board-detect GPIO read as "no panel",
or the `StaticInfomation` bootstrap). Once that precondition is satisfied (or the
timer forced to start), the poll reads `/dev/subucom_spi1.0` (24-byte frames) and
the `xdj-subucom` SSI slave can answer with StaticInfomation + button frames — and
the same path lights up the `button` debug command too.

### ROOT CAUSE (definitive): rbp never starts the panel poll thread

Corrected mechanism + confirmation via gdbstub:

- The real panel poll is **not** the `MemorandomTimer` — it's an **epoll-based
  run-loop thread**, `PanelComPeerLinux::run()` = **`FUN_00281d24`**. That loop
  `epoll_ctl`s each channel fd (`/dev/subucom_spi1.0`), then **reads the SPI only
  when `epoll_wait` reports `POLLIN`** (via `FUN_00281c78`); a timeout branch does
  not read.
- With a **hardware breakpoint on `FUN_00281d24` set before boot** (`-S` + `Z1`),
  rbp **fully boots (reaches Application Start) but never once enters the run
  loop** in 160 s. So the panel **poll thread is never spawned** — rbp constructs
  the whole panel stack (peer + receiver + controller, `setReceiver` done) but
  never calls `startThread`. (The peer TX `FUN_00281650` and the loop body
  `0x281e64` also never fire — consistent.)
- The panel `start`/`startThread` is **event-driven and not in the UI-init path**
  (`FUN_00245098` only constructs); the triggering event never occurs under
  emulation. This is why rbp never opens/reads `/dev/subucom_spi1.0`, and why the
  `button` debug command's dispatch has no live panel behind it.

**Fix is multi-part** (for the responsive SPI path):
1. **Start the poll thread** — find + satisfy the start trigger, or force-start it
   (a small rbp patch / injected hook, bakeable into the image).
2. **Make the device report `POLLIN`** — the epoll loop only reads on data-ready;
   model the subucom data-ready signal in QEMU (or make the driver always-readable)
   so `epoll_wait` wakes. (A plain blocking `read()` already clocks the SSI slave —
   proven by the `dd` test — so only the readiness/notify path is missing.)
3. **Answer with valid 24-byte frames** from the `xdj-subucom` SSI slave
   (StaticInfomation handshake + button/jog/fader frames).

### Live-process confirmation + exact symbols (2026-11-05)

Attached a gdbstub to a **running** rbp (QEMU monitor `gdbserver tcp::1234`, no
reboot) and read `/proc/<rbp>/…` and live memory. This upgrades the above from
"inferred" to "measured":

- **The panel is fully built, the thread just never starts.** Live rbp has **57
  threads** (`MessageLoop`, `DebugSub`, `Ui_CycleTask`, `Ui_EventTask`, `gui_task`,
  many `Tot_*`/`DB*`…) but **no `UiMain`** — that is the panel peer's thread name,
  set in its ctor. The process has **no `/dev/subucom_spi1.0` fd** (only 9
  `/dev/gpiodrv`) and **0 epoll fds**. So `run()` (`FUN_00281d24`) truly never ran.
- **Class / symbols.** The peer is **`PanelComPeerLinux`**, a JUCE `Thread` named
  **"UiMain"**, singleton **`DAT_0124087c`**; ctor **`FUN_00282214`** (the
  `__cxa_throw` string `"PanelComPeerLinux::PanelComPeerLinux()"` names it, and it
  passes `"UiMain"` to the Thread base). `run()` is vtable[5] (`0x423658`+0x14) =
  `FUN_00281d24`.
- **Build path, all confirmed reached in live memory.** `main()` =
  **`FUN_0000f7cc`** → UiObjectManager accessor **`FUN_0020dc6c`** (singleton
  `DAT_0123e16c`) → **`FUN_00245098`** (panel init). Live checks: init guard
  `DAT_0123e1fe`=3, entry gate byte `[UiObjMgr+0x3c]`=0, and the "bring-up subucom"
  flag `[0x0123e460]&1`=1 are **all satisfied** (none of these is the gate). Init
  stores the peer at `UiObjMgr+0x40` and registers **1 channel** (addChannel
  `FUN_002816c8`) holding `/dev/subucom_spi1.0` — live `peer+0x94` (channel count)
  = **1**, channel ptr at `peer+0x98`. The controller ctor `FUN_0021a4ac` only
  *creates* GpioManagers 0xa2-0xa5 with **no callback and no read**, so the panel
  is **not** GPIO-gated at construction.
- **`startThread` identified.** JUCE `Thread::startThread` = **`FUN_002f3a04`**
  (15 callers; `…←createThread wrappers←pthread_create@plt 0xea48`, GOT `0x461c40`).
  The three init-path starts are `MessageLoop` (in `main()`), the event dispatcher
  singleton `DAT_0123e580` (`FUN_00249654`), and the **"PDJ Certified"**
  Pro-DJ-Link thread (`FUN_002868d0`). **None start `UiMain`.** The UiMain start is
  a later UI-ready event that does not fire under emulation; the exact (indirect)
  call site is still unpinned.
- **Addresses for a bake-able fix** (rbp is non-PIE, base 0x8000, so these are
  absolute VAs): startThread = `0x2f3a04`, peer singleton = `*(DAT_0124087c)`,
  single-arg call `startThread(peer)`. A binary patch / `LD_PRELOAD` hook that
  calls `startThread(*(int*)0x0124087c)` once the peer is non-null is the planned
  step-1 fix (then steps 2-3 above: QEMU POLLIN + 24-byte frames).

> Note: force-starting live via the gdbstub is possible but fiddly — the emulated
> system sits in the kernel idle loop (pc `0x803dae8c`) almost always, so you must
> trigger a breakpoint inside rbp (not random interrupts) to get an injection
> context. The bake-able patch avoids this entirely.

---

## SOLVED (2026-11-05): it was a boot deadlock, fixed QEMU-side

The sections above (panel poll thread "never started") were the intermediate
picture. The real mechanism, found by walking the **live stuck main thread's
stack** and confirmed by a runtime experiment, is a **circular deadlock in panel
init** — and the fix is a QEMU GPIO change, with **no firmware/rbp patch**.

### Mechanism

1. The panel poll thread is started by **`FUN_002818bc`** (PanelComPeerLinux open
   + start: `open64("/dev/subucom_spi1.0")`, SPI-config ioctls, then `startThread`
   the epoll loop `FUN_00281d24`). Its only caller is **`FUN_002481ec`**
   (`UiObjectManager::start`), which `main` runs **only after** panel init
   `FUN_00245098` returns success.
2. `FUN_00245098` **never returns.** While building managers it constructs two
   **`UsbStorageManager`s** (`FUN_0024e834`, decks 1 & 2). Each makes a
   **`GpioManager`** (`FUN_0002be28`) on a **USB-detect GPIO** — deck1 =
   **GPIO4_IO30** (gpiodrv id 126 / 0x7e), deck2 = **GPIO7_IO12** (id 204 / 0xcc) —
   with a callback. The GpioManager ctor **reads the line and fires the callback**
   with the value.
3. The callback `FUN_00250020(this,val)` **sends to the panel only when the line
   reads 0 (LOW)** (`cmp r1,#0; popne` — returns harmlessly when it reads 1/HIGH).
   The send is `peer->send()` (`FUN_00281554`), which spin-sleeps
   `while(*(peer+0x88)==0)` — waiting for the poll thread's run loop to set
   `peer+0x88=1` after it opens the device + builds its epoll (`FUN_00281d24`).
4. So init talks to the panel → waits for the poll thread → but the poll thread is
   only started *after* init finishes. **Deadlock.** (Live proof: `main` the only
   thread in `nanosleep`, subucom fd never opened, no `UiMain` thread, built-flag
   `UiObjMgr+0x3c`=0.)

QEMU's `imx_gpio_reset` resets every input (`psr`) to **0**, so both USB-detect
lines read "USB present" → the callback sends → deadlock. On real hardware they
idle **HIGH** (external pull-up, "no USB at boot"), so the callback doesn't send
and init completes normally.

### Proof

On the deadlocked process, writing `peer+0x88 = 1`
(`peer = *(uint*)0x0124087c`) via `/proc/PID/mem` instantly unstuck it:
`/dev/subucom_spi1.0` opened, the **`UiMain` poll thread started**, epoll came up,
thread count 57 → 79. This confirmed avoiding the premature send is all that's
needed.

### Fix — `qemu/patches/04-imx6-usb-detect-gpio.patch`

Pure QEMU, no firmware change (so a **vanilla** `.UPD` still boots the panel, and
future firmware patches can be tested against stock behaviour):

- `hw/gpio/imx_gpio.c` + `imx_gpio.h`: new `psr-reset` property; `imx_gpio_reset`
  now does `s->psr = s->psr_reset` (default 0 — unchanged for every other GPIO).
- `hw/arm/fsl-imx6.c`: sets `psr-reset` on **GPIO4 (bit 30)** and **GPIO7 (bit 12)**
  so the two USB-detect lines reset HIGH ("no USB").

**Verified on a fresh stock boot:** subucom fd open, `UiMain` thread running, epoll
up, UI painted (16bpp), and `SUBUCOM_TRACE=1` shows the poll thread actively
clocking the SSI slave (~300k MOSI bytes). When a USB image is cold-plugged later,
the same lines should be driven LOW to signal "present".

### Remaining for working buttons (SPI command protocol + handshake)

The `xdj-subucom` SSI slave (patch 02) still returns constant `0xff` on MISO, so no
`StaticInfomation` handshake and no button/jog/fader frames come back. The reader
`FUN_00281c78` reads the frame and only dispatches to the receiver on a **changed**
frame (memcmp vs. the previous buffer). To make a control register:
- model the TX→RX command protocol (the MOSI stream `02 a8 ..` are rbp's commands/
  `SubTxData`; the panel answers on MISO),
- answer the **StaticInfomation** handshake (version etc.) so the panel is "ready",
- return frames the receiver decodes: the receiver is **`PanelComReciever`**
  (`FUN_00285384`); its +0x30 sub-object `vtable[2]` parses the frame into
  `{code, player, flags}` events that **`FUN_0028cea4`** routes (codes per the
  `button` table above — menu = 0x20a, play = 0x4101, …).
Note: the debug-console `button` command does **not** reach the UI even with the
panel live — only the real SPI frame path does.

---

## SPI command protocol — RE progress & plan (buttons + handshake)

With the panel alive (poll thread running), this is the remaining work to make
controls actually register. Entry points mapped so far:

**Wire shape.** The Subucom is a **continuously-clocked streaming** SPI device, not
a request/response one. `SUBUCOM_TRACE=1` shows ~12M+ byte transfers, the vast
majority **MOSI = 0x00** (rbp clocking to read the MISO stream), with occasional
command bursts beginning `02 a8 ..`. The kernel `subucom_spi` driver (built-in; no
source in tree) does the streaming + frames the MISO bytes into SubRxData packets;
rbp's `read(/dev/subucom_spi1.0, buf, 100)` returns one framed packet.

**rbp read/decode path (all confirmed):**
- `FUN_00281d24` run loop → on POLLIN → `FUN_00281c78`: `read()`s the channel,
  `memcmp`s against the previous buffer, and **only on a CHANGED frame** calls the
  receiver `onData` = `peer[0x98]->vtable[2]`.
- `onData` thunk **`0x285158`** (channel-id guard) → real parser **`FUN_00284d44`**
  `(receiver, buf, N)`. The parser does C++ event-queue management and reads the
  frame header at `buf[2]/buf[3]`; it builds **event structs** and routes each via
  the event router **`FUN_0028cea4`**.
- Event struct layout (from `FUN_0028cea4`): **code @ +8 (u16)**, **player @ +10
  (u8)**, **flags @ +0xb (u8; low nibble 0 = press)**. Codes = the `button <type>`
  table above (menu = `0x20a`, play = `0x4101`, quant = `0x4488`, …).
- The per-field sub-decoders (buttons/jogs/faders/deck-split: `FUN_0028830c`,
  `FUN_002899e8`, `FUN_00289f14`, `FUN_002886f4`, …) live at `0x284xxx-0x289xxx`,
  a range **Ghidra under-analyzed** (REA maps those VAs as file offsets) — use
  `objdump` to read them.

**StaticInfomation handshake.** rbp is NOT currently stuck on it (no "StaticInfomation
is not completed. retry..." after the fix), so it likely does not block button input,
only version/MAC display. Confirm separately.

**Slave today.** `xdj_subucom_transfer` (patch 02) returns constant `0xff`, so the
MISO stream never framing-syncs and never changes → zero events.

**Two viable approaches to finish:**
1. **Kernel-driver route (gets the framing for free):** objdump the built-in
   `subucom_spi` driver out of `vmlinux.bin` (wrap at 0x80008000) to learn the MISO
   framing (sync/length) + what a SubRxData packet looks like, then make the slave
   stream valid packets and inject a changed one for a press.
2. **Empirical route:** add a frame-inject hook to `xdj_subucom` (return bytes from a
   control file for one read burst), boot, breakpoint `FUN_0028cea4` via the gdbstub,
   and vary the injected frame until the router reports code 0x20a (menu) — mapping
   raw bytes → events directly, no assembly slog.

Recommended: (1) to nail the driver framing, then (2) to validate a menu press on
screen. NOTE the debug-console `button` path does not reach the UI — only real SPI
frames do.

### Kernel `subucom_spi` driver — transport map (from vmlinux.bin)

Disassembled out of the kernel (wrap a slice as an ARM ELF at VA = 0x80008000 +
file-offset; `/tmp/wrapelf.py` in session history). char-dev **major 153**, 256
minors, registered by the module_init at VA **0x800135a4** ("subucom SPI user
interface"). `file_operations` @ **0x807a54f4**:

| op | addr |
|---|---|
| read | 0x803eefd8 |
| write | 0x803ee8e0 |
| poll | 0x803edd28 |
| unlocked_ioctl | 0x803eea2c |
| open | 0x803ee860 |
| release | 0x803edd74 |

**read()** has two paths on a mode byte `priv[0x44]`: `==1` copies a pre-buffered
frame from `priv[0x98]`; otherwise it allocates `len*2`, builds a `spi_transfer`
(36B) + `spi_message`, calls the driver's SPI helper **0x803eddf8** (→ `spi_sync`
0x80616da0), then `copy_to_user(buf, priv[0xdc], len)`. **Either way the frame rbp
reads = the MISO of one `len`-byte (100) SPI burst.** So there is no stream-framing
to reverse: the QEMU `xdj_subucom` slave just needs to present a 100-byte SubRxData
frame per read burst (reset its byte index on CS/burst start).

**Still to do for buttons:** the SubRxData *content* layout (which bytes/bits =
which control, press/release, player) is defined by rbp's parser `FUN_00284d44` +
sub-decoders (the Ghidra-under-analyzed 0x284xxx-0x289xxx range) feeding the event
router `FUN_0028cea4` (code@+8/player@+10/flags@+0xb). Fastest way to map it now
that transport is understood: add a frame-inject hook to `xdj_subucom`, breakpoint
`FUN_0028cea4` via the gdbstub, and vary the injected frame until it reports menu
(0x20a). Then baseline-idle frame + a one-shot changed frame = a press on screen.

## Handshake + AUTH CHIP — RE findings (2026-11-05)

Live proof first: booting with a visible window, the deck UI renders and **DECK1
shows a red `E-7206 AUTH CHIP ERROR`** (DECK2 `Not Loaded.`) transiently during
boot, then settles to `Not Loaded.` (screenshot: `docs/img/panel-auth-chip-error.png`).
This confirms rbp opens subucom, the `UiMain` poll thread clocks the slave, and rbp
evaluates the panel auth on the SPI data — failing on our stub slave.

### The StaticInfomation handshake
- Class **`ui::StaticInfomation`** (RTTI mangled name `N2ui16StaticInfomationE` @
  `0x41e200`). Singleton object `DAT_0123e05c` (vtable `0x41e1e0`); its **version
  halfword is `DAT_0123e054`**, and a 0x40-byte info buffer at `DAT_0123e064`.
- **Gate:** `DAT_0123e054 == 0xffff` ⇒ rbp logs `"Subucom StaticInfomation is not
  completed. retry..."` (`0x420dd4`) and does not proceed. The panel must report a
  non-`0xffff` version word. Referenced by the **" VERSION INFORMATION"** menu-screen
  builder `FUN_00242538` (SYSTEM/SUB/APL/KERNEL Ver, LANGUAGE, MAC_ADDR).
- **Version *strings* come from guest files**, not SPI: `FUN_0025a3d4` reads
  **`/tmp/smdj.rev`** into `DAT_0123e064` (printable bytes only; fallback string
  `"Can't get version."`), and `FUN_00242538` reads **`/tmp/smdj2.rev`**. On real hw
  the subucom driver/boot writes these; under emulation they may be absent (cosmetic
  — the real gate is the `0xffff` version word from the panel).
- The **real writer** of the version word + per-frame status is the big subucom RX
  processor **`FUN_002cb8ec`** (non-contiguous, ~55 KB span; writes `DAT_0123e054`
  at `0x2d8ddc`). This is the next-phase target for the exact static-info frame
  layout and status-byte offsets.

### AUTH CHIP (E-7206) is panel-reported status, NOT rbp-side crypto
Decisive for emulation (symmetric-extractable vs asymmetric-unforgeable):
- The panel reports **per-channel hardware status as bytes** that rbp only
  *displays*: `FUN_00242538` sets `"[CH1]JOG uCom ERROR!!"` from `param+0x3e` and
  `"[CH2]JOG uCom ERROR!!"` from `param+0x3f`. The `E-7206 AUTH CHIP ERROR` is the
  same pattern — **per-deck**, shown in the deck status field.
- A full string scan finds **no panel-related crypto**: the only `auth` strings in
  rbp are Wi-Fi (`auth_algs`, WPS) and iPod accessory auth — nothing tied to the
  subucom/panel. There is no SHA/HMAC/signature-verify on the panel path.
- **Conclusion:** the auth chip is verified **on the panel MCU**, which reports a
  pass/fail *status* to rbp over subucom. rbp does **not** run a cryptographic
  challenge-response and holds **no secret key**. ⇒ The auth is **emulatable with no
  key**: the slave just needs to present a valid StaticInfomation (version word ≠
  `0xffff`) and report the **auth-OK status** in the periodic frame. This is the good
  outcome — no unforgeable private key in the physical panel blocks us.

### Next phase (to clear E-7206 and accept buttons)
1. RE **`FUN_002cb8ec`** (the subucom RX): extract (a) the StaticInfomation frame
   bytes that set `DAT_0123e054` ≠ `0xffff`, (b) the per-deck **auth-OK status**
   byte/bit, (c) the **jog uCom OK** bytes, and (d) the button-event frame header
   that routes a frame to the global decoder (the earlier "bare frame not routed"
   gate). Work from the version-word write at `0x2d8ddc` outward to the command
   dispatch.
2. Create `/tmp/smdj.rev` + `/tmp/smdj2.rev` in the guest (check `build-nand.sh` /
   boot) so the version strings are non-empty (cosmetic but clean).
3. Extend the QEMU `xdj-subucom` slave to stream: valid static-info + auth-OK +
   idle frame, then one-shot the control bit for a press (scaffolding in
   `docs/wip/subucom-inject-scaffolding.txt`).

## SubRxData frame parser `FUN_00284d44` — decoded via objdump (2026-11-05)

Ghidra left `0x284xxx` undefined; `objdump -d --start-address` on the rbp ELF
disassembles it cleanly. This is the parser rbp runs on each 100-byte MISO frame.
**All offsets below are in rbp's read-buffer coordinates.** (CORRECTION
2026-11-06: the "kernel drops 3 MISO bytes / `rbp_buf[j]==slave_frame[j+3]`"
claim here is **WRONG** — the real transform is a per-32-bit-word byte reversal
with no linear shift. See "FRAME TRANSPORT SOLVED & VERIFIED END-TO-END" at the
end of this doc.)

### The frame-routing gate (this was the "bare frame not routed" blocker)
```
284dd4  ldrb r2,[frame,#3]            ; frame[3]
284dd8  ldrb r3,[frame,#2]            ; frame[2]
284ddc  orr  hdr, r3, r2<<8           ; hdr = (frame[2]<<8)|frame[3]  (big-endian u16)
284de4  cmp  hdr,#0xffff ; beq SKIP   ; header 0xffff  -> frame dropped
284df0  cmp  hdr,#0      ; beq SKIP   ; header 0x0000  -> frame dropped
284df8  bl   <decode>                 ; else: frame is processed
```
A frame whose **header at `[2..3]` is `0x0000` or `0xffff` is silently dropped** —
which is exactly our all-zero / all-`0xff` stub frames. **To make any frame decode,
the slave must present a header at `[2..3]` that is neither 0 nor 0xffff.** The
parser also requires the frame pointer to be **4-byte aligned** and length ≥ an
expected size, or it `__cxa_throw`s.

### Frame structure (from the parser + decoder `FUN_00282630`)
- `[2..3]`  u16 big-endian **frame header / type id** (the routing gate; stored to
  the receiver at `+0x94`).
- `[0x62]`  u16 compared against a value computed over the previous frame
  (`FUN_0028259c` → `0x3f7f4`, a byte-swapped hash). (CORRECTION 2026-11-06:
  this "change-detector, not a checksum gate" reading is **WRONG** — `[0x62]` is
  a hard CRC-16/X-25 gate; on mismatch the frame is re-baselined and **no events
  fire**. The slave MUST satisfy it. See the final "FRAME TRANSPORT SOLVED"
  section.)
- `this+0x88` u16 = previous-frame marker (`0xffff` on the very first frame → the
  first-frame init path).

### Control-bit offsets decoded by `FUN_00282630`
```
[0x10] bit2   -> event arg A        [0x20] bit2   -> event arg B
[0x12] bit3   -> gate               [0x12] & 0x88 == 0x88 -> allocate a 388-byte event
[0x13] bit2   -> event              [0x13] bit3   -> event
```
These are per-control press/flag bits (more decoders — `FUN_002841e4`,
`FUN_00286074`, `FUN_0028613c` — cover the rest of the surface; full map TBD).

### What the slave must now emit (updated recipe)
1. A valid **header `[2..3]`** (non-zero, non-0xffff) so the frame routes — slave
   bytes `[5..6]`. Exact per-type header values still to be enumerated (the real
   panel's frame-type ids).
2. The control bit for the target button (menu = rbp `buf[0x1b]` bit0 = slave
   `[0x1e]`, from earlier RE) set once, in an otherwise-steady frame.
3. For E-7206: the per-deck **auth-OK status** byte (still to be located; it rides
   the same frame, panel-reported — see the handshake section).
Open items: enumerate the real **header type-ids**, the **auth-OK byte/bit**, and the
`[0x10]/[0x12]/[0x13]/[0x20]…` → physical-control map.

## Empirical test of the header fix — INCONCLUSIVE (2026-11-05)

Applied the header fix to the `xdj-subucom` slave (every frame carries a non-zero
header at slave `[5..6]` = rbp `[2..3]` = `0x0100`, plus the menu bit at `[0x1e]`),
rebuilt QEMU, and tested on a live boot. **Result: not verified working — do not
assume buttons decode yet.**

- **Visual, gdb-free (valid):** with a *proven-stable* baseline (two identical frames
  2 s apart), the first menu inject coincided with DECK1's `E-7206 AUTH CHIP ERROR`
  clearing to `Not Loaded.`. But that error **auto-clears on its own** during boot
  (seen on an earlier instance with no inject), so this is **confounded**, not proof.
  A second menu inject on the already-settled `Not Loaded.` screen produced **no
  visible change**. So on a settled UI, the menu inject has no observable effect.
- **gdb confirmation (failed/unreliable):** attempted hardware breakpoints on the
  parser (`0x284d44`), the global-button decoder (`0x2832b8`/`0x283394`) and even a
  kernel positive-control (`0x803eefd8`) via the QEMU gdbstub. **None triggered**, and
  the stub's run-control wedged the VM (resumable only via the monitor `cont`). So the
  gdbstub breakpoints are non-functional in this setup — there is currently **no valid
  in-process confirmation** either way.

### Likely remaining issues (reliable, static next steps)
1. **`frame[0x62]` may be a required field, not just a change-detector.** In the parser
   first-frame path (`0x284e2c`), `FUN_0028259c` computes a value and the decoder
   `FUN_00282630` runs only if it **equals `frame[0x62]`** (`284e7c cmp; 284e80 bne`).
   Our stub leaves `frame[0x62]=0`; if the computed value isn't 0, decode is skipped.
   RE `FUN_0028259c` (→ `0x3f7f4`) to learn what `frame[0x62]` must hold.
2. **Real header type-id.** `0x0100` is a guess; the parser→decoder dispatch
   (`FUN_00282604` vs `FUN_002841e4` vs `FUN_00282630`, and how `0x2832b8` is reached)
   may select the decoder by header/path. Trace it to get the true button-frame id.
3. **Confirm the poll thread still reads after settle** (SUBUCOM_TRACE) — the kernel
   positive-control not firing *might* mean reads pause once idle (or just the broken
   breakpoints). Relaunch with `SUBUCOM_TRACE=1` to check frames still flow.

The slave header change is RE-backed but unverified; it lives in the (gitignored)
build tree, not in `qemu/patches`, pending a working runtime probe.

## THE REAL GATE: a CRC-16 on every frame (fully RE'd, 2026-11-05)

The header gate was a red herring for *decoding*. Re-reading the parser
`FUN_00284d44`, **decoding only happens when a per-frame CRC matches** — otherwise
the frame is stored but nothing decodes (the header path calls a `bx lr` no-op).

- **Algorithm:** `FUN_0028259c` → `0x3f7f4` is a table-driven **CRC-16/X-25**
  (poly `0x1021`, reflected **`0x8408`** — confirmed from the table-gen constant at
  `0x3f9e8`; init `0xFFFF`; refin/refout; xorout `0xFFFF`).
- **Coverage:** the length method `0x2859b8` returns `frame_ptr + 96`, so the CRC is
  over the **first 96 bytes** (rbp `[0..0x5f]`), with **each 32-bit word byte-swapped
  first** (`FUN_0028259c` `rev`s each word before hashing).
- **Field:** compared to the little-endian halfword at rbp `[0x62]` (`ldrh [r1,#98]`).
- **Match path** (`0x284f28`) runs the section decoders on frame offsets
  `+4/+0x14/+0x24/+0x34/+0x44` (`FUN_002841e4`, `FUN_00282800`, `FUN_00282a98`) vs the
  previous frame at `this[0x90]` — this is where buttons (incl. the global-button
  byte `[0x1b]`) decode. **No header check on this path.**

So our stub's `frame[0x62]=0` never matched → zero decoding, ever. **Implemented the
CRC in the `xdj-subucom` slave** (build tree): build the frame in rbp coords, CRC the
byte-swapped first 96 bytes, write it to `[0x62]`, present on MISO. (CORRECTION
2026-11-06: "shift +3 onto MISO" was wrong — present each 32-bit word
byte-reversed, no shift. See the final "FRAME TRANSPORT SOLVED" section.)

### Confirmed by SUBUCOM_TRACE
- **The poll thread reads continuously after settle: ~140 frames/sec** (56,200 MOSI
  bytes in 4 s). Frames flow; the parser runs ~140×/sec.
- Therefore the **QEMU gdbstub breakpoints are non-functional here** — a breakpoint on
  a function hit 140×/sec never triggered (tooling dead, not code idle). No in-process
  probe is available; verification must be via the framebuffer.
- rbp's **MOSI** is a structured write stream: 4-byte words `02 aa <reg> <val>`
  (e.g. `02 aa 1a b0`, `02 aa 1b 08`) — rbp writing panel registers (LEDs/display).

### Verification gap + next steps
Could not yet *visually* confirm decode: the **MENU button has no visible effect on an
empty "Not Loaded" deck**, so a menu inject on a settled UI shows no change either way.
Needs one of:
1. A control with a **guaranteed visible effect** (toggles an on-screen indicator) to
   inject instead of MENU — map the event codes (`0x204`, `0x209`, `0x20a`, `0x4488`)
   to actions via the event router `FUN_0028cea4`.
2. The **auth-status byte**: set it to "fail" and confirm E-7206 *persists* (vs the
   known auto-clear) — definitive proof frames are processed, and the handshake goal.
3. **Risk to verify:** the ECSPI 32-bit burst byte-order. The parser's per-word `rev`
   may be compensating for the i.MX ECSPI delivering MISO bytes big-endian within each
   word; if so the slave's byte placement needs a matching swap. Unverifiable without a
   working decode signal — resolve once #1/#2 gives one.

## Handshake question answered + SubRxFormat frame class (2026-11-05, live memory)

Using the guest shell (root, `/dev/console` = rbp stdout) and reading rbp's memory
via `/proc/<pid>/mem` (`dd bs=4 skip=$((addr/4)) count=N | hexdump -e ...`), two
things are now settled:

### The StaticInfomation handshake does NOT gate buttons
Read the StaticInfomation struct at `0x0123e050` in the live `rbp_solo` (pid 2323):
**all zeros**, including the `__cxa_guard` byte `0x0123e050`. So the object was
**never initialized** — it is a *lazy* cache built only when the VERSION-INFO screen
or the debug console is opened (both read `/tmp/smdj.rev`). It is not on the panel
read/decode path. So a missing StaticInfomation is **not** why buttons don't work.
(`/tmp/smdj.rev` = `--.--`, `/tmp/smdj2.rev` = `--.-- [4206:4195:]` — placeholder
system/sub firmware revision strings rbp writes; `smdj` = the board code.)

### The MISO frame format is class `ui::SubRxFormat`
RTTI + assertion strings (`0x427328`+) name its accessors, which map 1:1 to the
parser's section decoders:
- `getCrcAdress()`  → the CRC field (`[0x62]`; coverage = first 96 bytes). **Confirms
  the CRC model again**: the format's size method returns 100, the coverage method
  returns start+96.
- `refFmtData()`    → frame `+0x04`
- `refCommonData()` → frame `+0x14`  (global buttons live here: `[0x1a]`,`[0x1b]`)
- `tMixerData()`    → frame `+0x24` / `+0x34`  (per-deck, decoder `FUN_00282800`, r3=1/2)
- `refOtherData()`  → frame `+0x44`  (decoder `FUN_00282a98`)

### MOSI (rbp→panel) protocol
Constant 100-byte frame, three `02 aa <reg> <val>` sub-commands per burst at offsets
0/32/36 (block sizes 32/4/64). No request/response handshake — rbp streams it from
early boot. Its tail holds rbp **pointers** (`0x0035c9f0`, `0x0035ca08`), so the TX
format differs from the RX (no CRC at `[0x62]`); can't validate the RX CRC against it.

### Where this leaves buttons
CRC implemented and re-confirmed; frames flow at ~140/s; handshake ruled out. Buttons
still show no *visible* effect, but MENU/global buttons may simply have no visible
effect on an empty deck. Definitive next tests (both now feasible with `/proc/mem`):
1. **Auth-status byte** — RE the per-deck decoder `FUN_00282800` (frame `+0x24`) to
   find the E-7206 auth bit; set it "fail" and confirm E-7206 *persists* (vs the known
   auto-clear) = proof frames are processed.
2. **Receiver frame counter** — locate the live `PanelComReciever` object and watch
   its `+0x8c` counter / `+0x88` marker increment = proof frames are accepted.

## DECODE CONFIRMED — the CRC frame is processed end-to-end (2026-11-05)

Definitive result via a controlled test + `/proc` process check:
1. Boot the CRC-valid slave to a **stable** UI (rbp_solo pid alive, two identical
   framebuffers).
2. `touch /tmp/subucom_menu` → the slave writes non-zero values into the **deck-1
   analog section** (rbp_buf `+0x24`, decoded by `FUN_00282800`).
3. **rbp_solo is dead immediately afterward** (`ps | grep -c rbp_solo` → 0), while it
   was alive before.

A rejected frame (bad CRC) is ignored and cannot affect rbp, so the only way the
injection can kill rbp is if the **frame was accepted and decoded**. Reproduced twice
(analog-in-idle crashed during boot; analog-gated crashed right after the trigger),
while menu-only / all-global-button runs reach a stable UI. **Therefore the CRC gate
works and the panel input path runs end-to-end**: slave frame → kernel → parser
`FUN_00284d44` (CRC matches) → section decoder → control handler.

- The crash is **downstream of decode**, in the control-change **render** (the earlier
  kernel fault dump shows `r6=0x320`=800, `r5=0x1e0`=480, the framebuffer dims → a
  blit). This is the project's known **GPU-2D render** flakiness (feature A/D), not a
  panel-protocol problem. (It may also be sensitive to out-of-range injected ADC
  values; real hardware stays in range.)
- So **the panel protocol (transport + CRC-16/X-25 + SubRxFormat sections) is solved
  and working at the decode level.** Clean *visible* control feedback is gated by the
  separate GPU-2D render path.

### Analog control map (deck section, `FUN_00282800`, value = sec[off]|sec[off+1]<<8, /1023)
`+0x24` deck1 / `+0x34` deck2, six 16-bit ADC values at section offsets 0/2/4/6/8/0xa
→ event codes `0x5008/0x5007/0x500a/0x5009/0x5011/0x508b`; plus a button at section
`[0xf]` bit7 → code `0x5112`. One of the analog codes is the TEMPO fader.

## FULL control map — SubRxData frame byte/bit → event code (2026-11-05)

Extracted by disassembling the whole decoder cluster (`0x282600-0x284d44`) and
parsing each `load offset → emit code` block. Each decoder is called from the
CRC-match path on a fixed section base; a control fires when its byte/bit (or 16-bit
analog value) **changes** vs the previous frame. Offsets below are **absolute rbp_buf
offsets** unless noted. Anchor (empirically confirmed): MENU = `[0x1b]` bit0 = code
`0x20a`.

### Global buttons — `[0x15]`..`[0x1b]` (decoder `0x2832b8`, inside `FUN_00282a98`)
```
[0x1b] bit0=0x20a(MENU) bit1=0x204 bit2=0x209 bit3=0x208
[0x1a] bit1=0x409 bit2=0x401 bit5=0x4488
[0x19] bit0=0x4206/0x4494 bit4=0x4205 bit5=0x420d bit6=0x420d bit7=0x420b
[0x15] bit0=0x5014 bit4=0x5095 bit5=0x5096 bit6=0x5097 bit7=0x5098
[0x16] bit1=0x4493                           [0x17] bit1=0x5014 bit2=0x4487 bit3=0x4486
```
(each press/release emits with state = bit? pressed:released)

### Per-deck performance buttons — `FUN_002841e4`, deck1 `+0x04`, deck2 `+0x14`
Button bits live in the section's last 4 bytes (`sec[0x0c..0x0f]` = `[0x10..0x13]`
deck1 / `[0x20..0x23]` deck2); a 16-bit value at `sec[0x02]` (code `0x411d`). The
per-deck code set (deck2 is the parallel set via the r3=2 arg):
```
sec[0x0c] 0x01=0x430e 0x10=0x411a 0x20=0x4119 0x40=0x4118 0x80=0x421c
sec[0x0d] 0x01=0x4116 0x02=0x4115 0x04=0x4114 0x08=0x4113 0x10=0x411b 0x20=0x411c 0x40=0x421b 0x80=0x4110
sec[0x0e] 0x01=0x4103 0x02=0x4111 0x04=0x4109 0x08=0x4108 0x10=0x4112 0x20=0x410b 0x40=0x4104
sec[0x0f] 0x01=0x410f 0x02=0x410d 0x04=0x4121 0x08=0x4120 0x10=0x431e 0x20=0x431f 0x40=0x4101 0x80=0x4102/0x4117
```
(0x41xx = deck transport/hot-cue/loop/pad buttons.)

### Per-deck analog — `FUN_00282800`, deck1 `+0x24`, deck2 `+0x34`
Six 16-bit ADC values (`sec[off]|sec[off+1]<<8`, LE, 0..1023, normalized /1023) +
one button. One of these is the **TEMPO fader**:
```
sec[0x00]=0x5008  sec[0x02]=0x5007  sec[0x04]=0x500a
sec[0x06]=0x5009  sec[0x08]=0x5011  sec[0x0a]=0x508b
sec[0x0f] bit7 = 0x5112 (button)
```

### Mixer / "Other" section — `FUN_00282a98`, `+0x44` (= `[0x44]`..`[0x52]`)
Analog (/1023) + buttons:
```
sec[0x00]=0x805  sec[0x02]=0x804  sec[0x04]=0x440c  sec[0x06]=0x4405
sec[0x08]=0x448b sec[0x0a]=0x600f sec[0x0c]=0x4403  sec[0x0e]=0x4402
```
(channel faders / crossfader / EQ / headphone — 0x44xx mixer, 0x08xx faders.)

### First-frame-only decoder — `FUN_00282630`
`[0x13]` bit3=0x0001, `[0x20]` bit7+bit3 (`&0x88`)=0x0002/0x0000 (init/handshake flags).

### Notes
- Codes are Pioneer internal control IDs; physical-label mapping is via the event
  router `FUN_0028cea4` (follow-up). Ranges: `0x20x` global, `0x40x/0x41x/0x42x/0x43x`
  deck buttons, `0x44x` mixer buttons, `0x50x/0x51x` analog, `0x60x` rotary/encoder.
- To model a control in the slave: set its byte/bit (or LE 16-bit value) in the frame
  built in rbp_buf coords, recompute the CRC, present it; toggle once to fire press,
  clear to fire release. Analog: write the raw 0..1023 value LE at the section offset.

---

## FRAME TRANSPORT SOLVED & VERIFIED END-TO-END (2026-11-06)

This section is **authoritative** and corrects two load-bearing errors in the
dated sections above (flagged inline there): there is **no "+3 kernel byte
drop"**, and `[0x62]` **is** a hard CRC gate (not merely a change-detector).
Verified against a live rbp with gdb (hardware breakpoints + guest-memory reads).

### The two-layer delivery path (both fully RE'd)

1. **SPI poll + change-detection** — `FUN_00281c78` (the `PanelComPeerLinux`
   poll, called ~140–220×/s). It `read()`s the 100-byte frame into a **ping-pong
   buffer** (`peer[0x18]`/`peer[0x1c]`, selected by `peer[0x14]&1`), then
   `memcmp`s the two buffers and **dispatches `onReceive(evtType, frame, 100)`
   only when the new frame DIFFERS from the previous one.** The forwarder
   `0x285160` runs the real handler `0x284d44` only for `evtType==0`.
   - Consequence for emulation: a **constant** frame is never dispatched. Our
     emulated idle frame is constant, so rbp only "sees" a control at the
     idle→pressed and pressed→idle **transitions** (which is exactly what a
     real press/release is). A held-constant value produces one down + one up.

2. **SubRxData parser + CRC gate** — `0x284d44`. On each dispatched frame it
   computes CRC-16/X-25 over the first 96 bytes (via `FUN_0028259c`) and
   **branches to the section decoders only if it equals the stored halfword at
   `rbp_buf[0x62]`** (`284dd0 cmp; 284f28 decode`). On mismatch the frame is
   treated as "first frame" (`this+0x88==0xffff` path) which just re-baselines
   and emits **no** events. So a wrong CRC = silent, total loss of control
   input — which is exactly the bug that made "controls not land".

### The byte-order transform (this was THE bug)

The ECSPI shifts each 32-bit word **MSB-first**, so the wire byte at position
`p` lands in rbp's logical read buffer `B` at index `(p&~3)+(3-(p&3))` — a
**per-32-bit-word byte reversal, with NO linear shift**. (This is the very swap
rbp's CRC routine reverses before hashing, and why the whole protocol is
word-reversed.) Calibration proved it to the byte: a marker at `s->frame[0x50]`
landed at `B[0x53]`/`B[0x52]` (symmetric word-pair), and an asymmetric marker
confirmed the within-word reversal exactly.

The old QEMU slave used `memcpy(s->frame + 3, rbp, 100)` — a **bogus +3 linear
shift** — which pushed the CRC (at `rbp[0x62]`) to wire index 0x65 → rbp's
`B[0x62]` read `0` → CRC never matched → every frame rejected → zero control
decode, while the frame still "changed" enough to look alive. (The earlier
"kernel drops 3 MISO bytes" note was a misreading of the word swap.)

**Fix (QEMU `xdj-subucom` slave, patch 02):** build the logical frame `rbp[]`
in rbp_buf coordinates (controls at `0x04/0x14/0x24/0x34/0x44`, CRC at `0x62`),
then transmit it with each 32-bit word byte-reversed:

```c
for (i = 0; i < 100; i++)
    s->frame[i] = rbp[(i & ~3) + (3 - (i & 3))];
```

rbp re-reverses on receive, recovering `rbp[]` exactly; its CRC over the first
96 bytes then equals the value we stored at `[0x62]`.

### Verified (live rbp, gdb)

- CRC **matches**: at the compare site rbp's computed CRC == stored (`0xcbae`
  idle, tracks the data when a control is set); the handler now runs the
  **NORMAL decode path** (`0x284dd0`→`0x284f28`), not the first-frame path, i.e.
  `this+0x88` is initialised and frames are being diffed.
- Control events **dispatch**: injecting the global-button byte `[0x1b]` makes
  the Common-section decoder `0x2841e4` detect the change and call the button
  dispatcher `0x28411c` (`LR=0x2842b8`) — the full chain
  `SPI read → memcmp → onReceive(0) → CRC ok → section diff → control event`
  is live.

### Still open (follow-up, not a transport problem)

- **Visible UI reaction.** With no track loaded, pressing the global-button
  bytes `0x14..0x23` produced no framebuffer change (confirmed by sweep) — most
  buttons don't redraw the deck screen in that state. The event *is* delivered
  (proven above). Finding controls that visibly change the screen (BROWSE, the
  rotary, LOAD, …) and re-verifying the **byte/bit→code map** (the map in the
  sections above was largely derived while decode was broken) is the next step.
- **Idle frame liveness.** Because dispatch is change-gated and our idle frame
  is constant, consider giving the idle frame a real MCU-like heartbeat (e.g. a
  cycling counter in a non-control byte) so rbp's baseline initialises early and
  state stays "live" the way continuous hardware frames keep it.

### Reproduce / test (headless + gdb)

Scratchpad helpers (session): `launch_v2.sh` (boots `-smp 1` with `-gdb
tcp::1234`, monitor `/tmp/d_mon4.sock`, serial `/tmp/d_con4.sock`), `catch.py
<addr…>` (set hw bp, report hits — clears stale bps first), `crcverify.py`
(reads computed vs stored CRC at the compare sites), `rampmap.py`
(`SUBUCOM_RAMP=1` + direct buffer read → raw MISO→rbp_buf map), `con4.py`
(guest shell), `shot4.py` (screendump). Inject a control with
`echo "b 1b 1" > /tmp/subucom_ctrl` (button) or `echo "a 24 abcd" >` (analog).
Diagnostics (env): `SUBUCOM_RAMP`, `SUBUCOM_BADCRC`, `SUBUCOM_TRACE`,
`SUBUCOM_BURST`.

### Control-map re-derivation — progress (2026-11-06, same session)

Confirming the old byte/bit→code map is unreliable and mapping the real dispatch:
- `frame[0x1a..0x1b]` (old map: "global buttons MENU/…") is a **16-bit ANALOG**:
  its Common-section handler `FUN_0028411c` computes a float and emits code
  **`0x410a`** — not a button. So the old "[0x1b] bit0 = MENU 0x20a" is wrong.
- Events are dispatched through the **receiver's observer vtable**: the section
  decoders call `(*receiver[0x3c])->vtable[8](receiver[0x3c], code, …)`. The
  `FUN_0028cea4` that older notes called "the event router" is **NOT** on this
  button path — a breakpoint there never fired for any injected frame byte.
- Empirical negative result: with the deck idle ("Not Loaded.", USB mounted), a
  full sweep of frame bytes `0x04..0x5f` (both as buttons `b <off> ff` and as
  analogs `a <off> 03ff/00ff`) produced **no framebuffer change**. Decode +
  dispatch are confirmed live, but no single control visibly changes the idle
  deck screen — likely most need a loaded track / a different screen context, or
  the real button bytes/codes differ from the old map.

**Next (control map):** trace the observer vtable[8] target from
`receiver[0x3c]` to the code→action table, enumerate which codes are buttons vs
analogs per section, and identify navigation controls (BROWSE / MENU / rotary /
LOAD) by code. A cycling-counter idle heartbeat would also let the baseline
initialise without an injected transition. BROWSE specifically could not be
pinned this session — it is the first target once the code→action map is RE'd.

---

## ✅ UI VISIBLY REACTS TO CONTROLS — SEEN LIVE (2026-11-09)

**This section is authoritative and overturns the "Still open / no fb change"
negatives above (lines in "FRAME TRANSPORT SOLVED" §"Still open" and
§"Control-map re-derivation").** With patch-02 (byte-order fix) in the running
build and USB mounted, injected panel controls **visibly change the deck UI**,
and it was reproduced **live in a Cocoa window**. rbp stays healthy throughout
(`pidof rbp_solo` = 2405, 79 threads, before and after).

### Why the earlier sweep reported "no change" (measurement artifact)

The earlier negative ("sweep 0x04..0x5f → no fb change") was not real. Two causes:
1. Those sweeps compared screendumps **by eye**; the real reactions are often
   small text/colour toggles (e.g. `TEMPO ±10`→`±16`, `500 ms`→`10 ms`) that are
   easy to miss. **md5-diffing** each screendump catches them instantly.
2. The reactions are **sticky toggles**. Pressing the same control twice returns
   to the baseline, so a sweep that diffs *consecutive* shots sees "no change"
   after the first toggle sticks. (Diff against a **fixed** baseline, or capture
   **pre/post per injection**, to avoid this.)

### What was observed (byte-level sweep, `b <off> ff`, all 8 bits, fixed baseline)

A sweep of `b <off> ff` for `off = 0x04..0x5f` (the harness auto-runs a full
press+release via `s->press=30`), md5-diffed against the idle baseline, drove the
deck UI through **four distinct sticky states**. The genuine reacting transitions
(where the hash changed from the *previous* byte) are:

| transition byte | section | visible change |
|---|---|---|
| `0x12` | deck1 Fmt/Common (`+0x04`, button bytes `[0x10..0x13]`) | **DECK 1 TEMPO range ±10 → ±16** |
| `0x22` | deck2 Fmt/Common (`+0x14`, button bytes `[0x20..0x23]`) | **DECK 2 TEMPO range ±10 → ±16** |
| `0x56` | Mixer/Other (`+0x44`) | **BEAT EFFECTS 500 ms → 10 ms** |
| `0x5b` | Mixer/Other (`+0x44`) | **ROLL selected (orange) + TAP BPM (green)** |

**Accuracy note:** because the toggles are sticky and the sweep diffed against a
*fixed* baseline, bytes *between* the transitions (0x13–0x21, 0x23–0x55, …) read
as "changed" only because the screen was still holding the prior toggle — they are
**not** independently confirmed reactors. The four bytes above are the confirmed
reacting controls at all-bits granularity; isolating the exact bit/control of
every byte needs the per-bit **pre/post-diff** pass (below).

### Render path is NOT the bottleneck (and never was)

`IMX6_GPU_TRACE=1` over a full boot shows GPU command traffic **frozen after init**
— exactly **one** `GPUSTUB[0320] wr off=0x658` (a 2D command kick) in the whole
run. So this build renders the deck UI and all control reactions via **software
DirectFB straight to the framebuffer at `0x18000000`**; the GC320 2D stub is not
in the live redraw path. The "render-stub swallows the redraw" theory is therefore
dead: redraws reach the framebuffer directly, which is why the md5 diffs fire.

### Seen live

`scratchpad/launch_live.sh` boots `-display cocoa` (visible window) with the USB
image, monitor `/tmp/d_mon5.sock`, gdb `:1235`. Injecting `0x12 → 0x22 → 0x56 →
0x5b` stepped the on-screen window through all four states; each capture off the
live window matched the headless sweep hash exactly. The harness reads host
`/tmp/subucom_ctrl` (`b <off> <bitmask>` / `a <off> <value>`, hex).

### Method for the full control map (in progress)

- **Byte pre/post sweep:** for each byte, screendump *before*, inject `b <off> ff`,
  screendump *after*, diff pre vs post — finds every byte with a net-visible effect
  (immune to accumulated sticky state).
- **Per-bit pre/post sweep** of each reacting byte → isolate the individual
  button/LED bit.
- **Analog sweep** of the section ADC offsets (`a <off> <val>` at 0x24–0x33 deck1,
  0x34–0x43 deck2, 0x44–0x53 mixer, and the deck-perf 16-bit at `sec[0x02]`) with a
  few values (0x0000 / 0x0200 / 0x03ff) → faders / jog / knobs.
- Correlate each hit to (a) the visible UI element, (b) the static decoder code
  (the byte/bit→code tables above), and (c) the **XDJ-RX physical control**.
