# rbp and the front-panel (sub-ucom) protocol

Technical reference for `rbp`, the XDJ-RX main application, and for the SPI protocol between rbp
and the front-panel microcontroller ("sub-ucom"). It covers the rbp structure and anchor
addresses, the panel stack inside rbp, the wire format in both directions, and how the emulator's
panel model (`qemu/xdj/xdj-panel.c`) produces frames and takes injected control input.

Other docs own some of the details:

| Topic | Doc |
|---|---|
| Frame byte/bit → control (authoritative control map) | [subucom-frame-map.md](subucom-frame-map.md) |
| rbp → panel LEDs, meters, jog ring (`SubTxData`) | [subucom-led-map.md](subucom-led-map.md) |
| Test mode, factory reset, POWER/standby handshake, update mode, auth chip | [service-modes.md](service-modes.md) |
| Panel-MCU flash bootloader, NAND, ALL updates | [firmware-update-and-nand.md](firmware-update-and-nand.md) |
| rbp's UDP debug console (`button`/`knob`/`rotary` vocabulary) | [debug-console-protocol.md](debug-console-protocol.md) |
| Version strings (`/tmp/smdj.rev`, `/tmp/smdj2.rev`) | [version-display-and-override.md](version-display-and-override.md) |
| USB slots and mounting | [usb.md](usb.md) |
| Feature status | [emulator-status.md](emulator-status.md) |

## 1. The binary

- Path on the unit: `/root/pioneer/rbp`. Analyzed copy: `build/extract/rbp` (sha256
  `4f62aa71ef8c7246536e87950939cc081b1850b22502a3780851c20c35b18dd7`).
- ARM32 EABI5 soft-float ELF, dynamically linked (`/lib/ld-linux.so.3`), C++ (GCC/libstdc++),
  about 4.6 MB, built 2017-10-18.
- **Non-PIE**, load base `0x8000`: `vaddr = file_offset + 0x8000`. Every address in this doc is
  an absolute VA. Globals live at fixed VAs (`0x0107xxxx`, `0x0123xxxx`).
- Stripped of symbols, but C++ RTTI type names and `__func__`/assert strings survive. That is how
  the class names below were recovered.
- Analyze with REA/Ghidra; xrefs resolve well because rbp uses PC-relative literal pools.
  **Ghidra under-analyzes `0x282000-0x28b000`** (the panel decoders): it leaves many functions
  undefined and resolves some VAs as file offsets. Disassemble that range with
  `objdump -d --triple=armv7 --start-address=0x… build/extract/rbp`. The
  `rbp-binary-internals` skill has the full workflow, the subsystem map beyond the panel, and the
  gdbstub rules.

### Source layout

Recovered from embedded `../../../Source/...` paths and RTTI names:

| Area | Source root | Role |
|---|---|---|
| Nexus / DB | `Source/Allinone/Nexus/DB/cache/cue/` | track database and cue/analysis cache: `disc_*`, `msc_anlz_*_usb` (USB media analysis), `ex_fsys` |
| Nexus / GUI | `Source/Allinone/Nexus/GUI/suntec/glib/` | the "suntec glib" GUI toolkit on DirectFB (`DS_HW_Glib3_DFB.c` = the DirectFB HW plugin) |
| UI | `Source/Allinone/UI/` | screens, TestMode, DirectFbIf; the `ui::` namespace (242 classes), including the whole panel stack |
| Engine | `Source/Engine/PlayEngine/` | deck engine: `playengine_Player`, `playengine_LoopQuantize`, DSP `TimeStretch` |
| Common | `Source/Common/` | `native/linux` (`GpioManagerLinux`), `reader/FileReader` (page-buffered and remote input streams) |

The UI is drawn by software DirectFB straight into the framebuffer. The Vivante GC320 2D core is
not on the redraw path.

### Other subsystems (pointers)

- **Audio decode:** MP3 (`libmpg123.so.0`, dlopen'd), AAC, M4A, AIFF, WAV. No FLAC. Two decoder
  slots, `PLAYER_DECODER_0/1`. Recording writes `/PIONEER REC/REC%03d.WAV`.
- **USB media:** two slots. The mount code logs `mount success` / `mount falied` and
  `PlNo=%d,Dev=%d,Lett=0x%x(%c),Mount=%d,PcLett=0x%x(%c)`. Media is analysed into the cue/DB
  cache (`msc_anlz_*_usb.c`). exFAT would be a kernel capability (the 3.0.35 kernel has none).
- **GPIO:** `common::GpioManager` on `/dev/gpiodrv` (ctor `FUN_0002be28`): `lseek(fd, id)`,
  `read(fd, 1)` = level, `ioctl(0x4004f101, {id, mode})`. rbp uses it for a handful of discrete
  board lines, not for the DJ controls. See the `map-gpio-control` skill.
- **Mixer/audio graph, switch state, settings:** see the `rbp-binary-internals` skill.

New device features (FLAC, exFAT, …) are built as custom firmware ([custom-firmware.md](custom-firmware.md)).
The emulator itself never patches rbp.

## 2. The panel stack inside rbp

Every physical control (buttons, jog wheels, tempo faders, crossfader, EQ/trim/FX knobs, the
browse encoder) is read by the panel MCU and sent to rbp over SPI. The XDJ-RX has no touchscreen.
A few top-panel keys and the rear LINE/PHONO switch are i.MX6 GPIOs instead (section 6).

```
panel MCU ─SPI (ECSPI2, bus 1 CS 0)─► kernel subucom_spi ─► /dev/subucom_spi1.0
  ─► PanelComPeerLinux  ("UiMain" thread: epoll loop, read, change check)
  ─► PanelComReciever   (onReceive → panel_protocol parser, CRC gate)
  ─► section decoders   (diff vs previous frame → (code, state, deck) events)
  ─► dispatchers        (centre/mixer FUN_0026ae7c, analog FUN_002adee8; observed at 0x2a47c0)
```

### Classes (`ui::` namespace)

| Class | Role |
|---|---|
| `PanelComPeerLinux` (`IPanelComPeer`/`PanelComPeer`) | Transport. A JUCE `Thread` named **`UiMain`**. Opens the device, runs the epoll loop, does the reads and writes. Nested `MemorandomTimer` (callback `0x282484`); the timer is not the read path. |
| `PanelComController` | Owns the `SubTxData` TX buffer and the panel tasks. |
| `PanelComReciever` (`IPanelComReciever`) | Receives frames from the peer and runs the parser. |
| `panel_protocol::Base`, `::TxData`, `::SubTxData`, `::SubRxData` | Frame objects. RX is formatted by `ui::SubRxFormat` (accessors `refFmtData`, `refCommonData`, `tMixerData`, `refOtherData`, `getCrcAdress`, `xor`; RTTI from `0x427328`). |
| `PcControlKeyData`, `PcControlJogData`, `PcControlTempoSliderData`, `PcControlDeckSelectData`, `PcControlPadData`, `PcControlBfxRotaryData`, `PcControlLongPressData`, `PcControlLedData`, `PcControlKeyServer`, `KeyManager`, `KeyInput`, `XFaderInnards`, `TempoSlider`, `JogSpeedGuesser` | Decoded control state and its consumers. |
| `KeyTestMode`, `SwitchTestMode`, `JogTestMode`, … | Test-mode views that show raw control state. Useful oracles when mapping ([service-modes.md](service-modes.md)). |

### Anchor addresses

| What | Address |
|---|---|
| `main()` | `FUN_0000f7cc` |
| UiObjectManager accessor / singleton | `FUN_0020dc6c` / `DAT_0123e16c` |
| Device/panel init (builds Settings, LedManager, PowerManager, the panel peer, …) | `FUN_00245098` |
| `UiObjectManager::start` (runs after init returns) | `FUN_002481ec` |
| Peer open + start (`open64("/dev/subucom_spi1.0")`, SPI ioctls, `startThread`) | `FUN_002818bc` |
| `PanelComPeerLinux` ctor / singleton | `FUN_00282214` / `DAT_0124087c` (also stored at UiObjMgr `+0x40`) |
| `addChannel` (registers `/dev/subucom_spi1.0`, buffer 100) | `FUN_002816c8` |
| Peer `run()` = epoll loop (vtable `0x423658`, slot 5) | `FUN_00281d24` |
| Channel read + change check | `FUN_00281c78` |
| Peer send (TX) | `FUN_00281554`, low-level `FUN_00281650` |
| JUCE `Thread::startThread` | `FUN_002f3a04` |
| `PanelComController` ctor (vtable `DAT_00420700`) | `FUN_0021a4ac` |
| `PanelComReciever` vtable / RTTI | `0x4272cc` (secondary `0x4272e0`) / `N2ui16PanelComRecieverE` @ `0x4272f0` |
| Receiver `onReceive` forwarder / thunk with channel-id guard | `0x285160` / `0x285158` |
| Frame parser `panel_protocol::Base::setBinaryData` | `0x284d44` (prologue `push` at `0x284d48`) |
| CRC over the frame (word-reverse then table CRC) | `FUN_0028259c` → `0x3f7f4` (table generator constant at `0x3f9e8`) |
| CRC coverage method (returns start + 96) | `0x2859b8` |
| First-frame boot-key handler | `0x282630` |
| Per-channel mixer analog decoder (`+0x24`, `+0x34`) | `0x282800` |
| Centre-section decoder (`+0x44`; button block at `0x2832b8`, POWER bit at `0x283678`) | `0x282a98` (code range `~0x282c00-0x283880`) |
| Deck decoder (`+0x04`, `+0x14`) | `0x2841e4` (code range `~0x2845xx-0x284d40`) |
| Jog decoder | `FUN_00283dc4` |
| Centre/mixer code dispatcher | `FUN_0026ae7c` |
| Continuous-analog handler | `FUN_002adee8` |
| Panel event dispatcher, `(obj, code, state, deck, …)` | `0x2a47c0` |

The deck-button code handler downstream of the deck decoder is not pinned. It is **not**
`FUN_00222188`: that is a deck-engine state object, and breakpoints on it do not fire for deck
buttons. `FUN_0028cea4` (which routes `{code @+8, player @+10, flags @+0xb}` event structs) is
not on the panel-button path either.

### Startup and the poll thread

1. `main` → `FUN_00245098` constructs the panel stack: peer + receiver + controller, one channel
   (`peer+0x94` = channel count, `peer+0x98` = channel pointer). The controller ctor creates
   GpioManagers `0xa2-0xa5` (no callback, no read) and registers four periodic panel tasks
   (`FUN_0002be28(obj, id, 1, 100, 0, 0x1e)`). Construction opens nothing.
2. Only after `FUN_00245098` returns does `FUN_002481ec` call `FUN_002818bc`. That opens the
   device and starts the `UiMain` thread.
3. `run()` (`FUN_00281d24`) adds each channel fd to an epoll set, sets **`peer+0x88 = 1`**
   ("ready"), and reads a channel only when `epoll_wait` reports `POLLIN`. A timeout reads
   nothing.

**Fault-flag requirement.** During step 1, each of the two `UsbStorageManager`s
(`FUN_0024e834`) creates a GpioManager on a front-USB **fault flag**: USB1 = GPIO4_IO30
(gpiodrv id `0x7e`), USB2 = GPIO7_IO12 (id `0xcc`). The GpioManager ctor reads the line and calls
the listener `FUN_00250020(this, level)`. When the level is **0** (fault asserted), the listener
sends to the panel through `peer->send()` (`FUN_00281554`). That call spins until `peer+0x88`
becomes 1. The flag is only set by the poll thread, which does not exist yet, so init would hang
forever. On hardware these lines idle HIGH (no fault). The emulator reproduces that with a reset
level: `psr-reset` (added to `imx_gpio` by `qemu/patches/02-imx6-peripherals.patch`) set HIGH for
both pins in `qemu/xdj/xdj-board.c`. When the panel is up, rbp runs about 79 threads, including
`UiMain`, with `/dev/subucom_spi1.0` and an epoll fd open.

## 3. Kernel `subucom_spi` driver

The driver is built into the kernel. Its GPL source exists, and its layout was also read out of
`vmlinux` (VA = `0x80008000` + file offset):

- Char device **major 153**, 256 minors, module_init at `0x800135a4` ("sub-ucom SPI user
  interface"). `file_operations` at `0x807a54f4`:

  | op | addr |
  |---|---|
  | read | `0x803eefd8` |
  | write | `0x803ee8e0` |
  | poll | `0x803edd28` |
  | unlocked_ioctl | `0x803eea2c` |
  | open | `0x803ee860` |
  | release | `0x803edd74` |

- `read()` has two paths, chosen by a mode byte `priv[0x44]`. Mode 1 copies a pre-buffered frame
  from `priv[0x98]`. Any other mode builds an `spi_transfer` + `spi_message`, calls the SPI helper
  `0x803eddf8` (→ `spi_sync` `0x80616da0`), and does `copy_to_user(buf, priv[0xdc], len)`. Either
  way, **the frame rbp reads is the MISO of one 100-byte SPI burst.** There is no stream framing
  or sync pattern to decode.
- The driver runs its own periodic transfers (the firmware updater stops this timer before it
  reflashes the MCU). The first few frames on a boot come from the driver at probe time, and
  rbp's first poll follows about 20 s later. Once up, rbp reads about 140–220 frames/s.

## 4. MISO: the `SubRxData` control frame

One 100-byte frame per burst. The RX has **one** channel; every control is in this frame (the
RX3 has a second one).

### Wire byte order

The i.MX ECSPI shifts each 32-bit word MSB-first. The wire byte at position `p` therefore lands
in rbp's read buffer at `(p & ~3) + (3 - (p & 3))`. This is a **per-32-bit-word byte reversal
with no linear shift**. All offsets in this doc and in the frame/LED maps are **logical** offsets
in rbp's buffer. A sender must transmit each word reversed:

```c
for (i = 0; i < 100; i++)
    wire[i] = logical[(i & ~3) + (3 - (i & 3))];
```

### Layout (logical offsets)

| Offset | Contents |
|---|---|
| `0x00-0x01` | unused by the decoders |
| `0x02-0x03` | panel-MCU firmware **version word**, LE (`hi.lo` shown as `"%x.%02x"` in test mode; `0x0035` → `0.35`). Stored by the first-frame handler at receiver `+0x94`. |
| `0x04-0x13` | deck 1: tempo fader pair, vinyl-speed knob, jog period/counter/flags, buttons `0x10-0x13` |
| `0x14-0x23` | deck 2 (deck 1 + `0x10`) |
| `0x24-0x33` | mixer channel 1: EQ HI, TRIM, EQ LOW, EQ MID, channel fader, COLOR; `0x33.7` INPUT SELECT (1 = DECK) |
| `0x34-0x43` | mixer channel 2 (channel 1 + `0x10`); `0x43.7` INPUT SELECT |
| `0x44-0x5f` | centre: mic EQ, booth, master, FX depth, crossfader, headphones, browse/TIME counters, FX/CH select, nav/source buttons, POWER |
| `0x60-0x61` | unused |
| `0x62-0x63` | **CRC-16**, LE |

The per-control byte/bit assignments, codes and encodings are in
[subucom-frame-map.md](subucom-frame-map.md). `SubRxFormat`'s accessors line up with these
sections: `refFmtData` +0x04, `refCommonData` +0x14, `tMixerData` +0x24/+0x34, `refOtherData`
+0x44. The names are Pioneer's; the physical meaning per offset comes from the frame map.

### CRC

**CRC-16/X-25**: poly `0x1021` (reflected `0x8408`), init `0xFFFF`, refin/refout, xorout
`0xFFFF`. It covers the **first 96 bytes**, each 32-bit word byte-reversed before hashing (so it
is computed over the wire order). The result is stored LE at `0x62`. The `SubTxData` frame uses
the same CRC over 20 bytes ([subucom-led-map.md](subucom-led-map.md)).

### What rbp does with a frame

1. **Change gate** (`FUN_00281c78`): the peer reads into a ping-pong buffer pair
   (`peer[0x18]`/`peer[0x1c]`, selected by `peer[0x14] & 1`) and `memcmp`s new against previous.
   **Only a frame that differs from the previous one is passed on** (`onReceive(0, frame, 100)`).
   A constant frame is delivered once and then ignored. That is why a held control produces
   exactly one press and one release.
2. **Sanity checks** (`0x284d44`): the frame must be 4-byte aligned and long enough, or the
   parser throws.
3. **First frame:** when the receiver's previous-frame marker (`this+0x88`) is `0xffff`, the
   frame becomes the baseline. The boot-key handler `0x282630` runs on it: test mode, factory
   reset, and storing the version word ([service-modes.md](service-modes.md)). **Boot-key
   combinations are checked on the first frame only.**
4. **CRC gate:** the parser computes the CRC and compares it with the halfword at `0x62`
   (`0x284dc8-0x284dd0`).
   - Match → `0x284f28`: the section decoders run on `+0x04/+0x14/+0x24/+0x34/+0x44`, diffing
     against the previous frame (`this+0x90`).
   - Mismatch → no control events. If the header `[2..3]` is neither `0x0000` nor `0xffff`, the
     parser calls `0x282604` (a header path that does not decode controls). **A bad CRC means
     silent, total loss of control input.**
5. **Decode and dispatch:** each decoder is an unrolled `ldrb [cur,#off]; ldrb [prev,#off]; eor;
   tst #mask` pattern. Every changed bit or field emits an event `(code, state, deck)`, where
   `state` is 0 = press, 2 = release, 3 = relative, 4 = absolute value. Button codes follow
   rbp's debug-console vocabulary (`0x20x` global/nav, `0x40x-0x43x` deck, `0x44xx` mixer and
   beat FX, `0x50xx` analog, `0x60xx` switches, `0x80xx` POWER and USB STOP). The full table is
   in [debug-console-protocol.md](debug-console-protocol.md).

### Encodings

- **Buttons:** one bit each, 1 = pressed. **Exception: POWER (`0x5d.0`, code `0x8001`) is
  active-low**: it idles at 1 and reads 0 while pressed. A press starts rbp's standby handshake
  ([service-modes.md](service-modes.md)).
- **Analog pots/faders:** 16-bit LE, `0..1023`, normalised ÷1023. The tempo fader is a
  complementary pair (`0x04` and `0x0a`) and both halves must be set together, or rbp reads the
  pot as pegged.
- **Multi-position switches** (FX select, CH select, crossfader curve, MIC) are small bit fields
  inside a byte. Values are in the frame map.
- **Endless encoders** (browse `0x54`, beat-FX TIME `0x56`, jog position `0x0e`) are 16-bit
  counters that rbp diffs. A counter must keep its value in every subsequent frame: dropping it
  back to 0 reads as a huge jump.
- **Jog:** pulse period at `0x0c` (speed = 416.67/period; 0 or `0xffff` = stopped), position
  counter at `0x0e`, flags at `0x10` (.0 touch, .1 reverse).

## 5. MOSI: the `SubTxData` LED frame

The same burst carries rbp → MCU data on MOSI: every panel LED, the level meters and the
jog-ring position. The MCU only lights what it is told; rbp makes LEDs blink by toggling the bits
itself.

- Logical payload `0x00-0x13` (20 bytes), CRC-16/X-25 over the word-reversed payload at `0x16`
  (LE), `0x14-0x15` zero. The rest of the 100-byte burst is stale buffer memory (it contains rbp
  pointers) and has no meaning. Deck 2 = deck 1 + 4 bytes.
- The 24-byte `SubTxData` object inside `PanelComController` (`memset(this+0x6c, 0, 0x18)`,
  asserted 4-byte aligned: `"SubTxData() not 4bytes alignment 0x%x"`) is this payload plus its
  CRC.
- rbp sends it from early boot. It is constant at idle and changes about 20×/s while a deck
  plays.
- The four source-key LEDs (rekordbox/MIDI/USB1/USB2) are **not** in this frame. They are rbp
  GPIO outputs 162–165.

Bit map and transport details: [subucom-led-map.md](subucom-led-map.md).

## 6. Other panel-related inputs

- **MAIN-UCOM direct keys** are i.MX6 GPIOs read through `/dev/gpiodrv` (key handler
  `FUN_00274a38`), not frame bits. There are eight: Linux GPIO 119, 120 (rear LINE/PHONO switch,
  HIGH = PHONO) and 146, 149, 158–161 (top-panel source/select keys). They idle HIGH and read LOW
  when pressed.
- **GPIO 117/118, 134/135 and 162–165 are rbp outputs**, not inputs.
- **Firmware-update request:** GPIO3_IO30, raised by the MCU when USB STOP + RELOOP/EXIT are held
  at power-on ([service-modes.md](service-modes.md)).

## 7. `StaticInfomation` and version information

- `ui::StaticInfomation` (RTTI `N2ui16StaticInfomationE` @ `0x41e200`): singleton
  `DAT_0123e05c` (vtable `0x41e1e0`), guard byte `0x0123e050`, version halfword `DAT_0123e054`,
  0x40-byte info buffer `DAT_0123e064`.
- It is a **lazy cache**, built only when the VERSION INFORMATION screen or the debug console
  needs it. It is **not** on the control path. When `DAT_0123e054 == 0xffff`, rbp logs
  `"Subucom StaticInfomation is not completed. retry..."` (string `0x420dd4`). The version
  halfword is written at `0x2d8ddc`, inside `FUN_002cb8ec`.
- Version strings come from files, not SPI. `FUN_0025a3d4` reads `/tmp/smdj.rev` into
  `DAT_0123e064` (fallback `"Can't get version."`). The VERSION INFORMATION builder
  `FUN_00242538` (SYSTEM/SUB/APL/KERNEL Ver, LANGUAGE, MAC_ADDR) reads `/tmp/smdj2.rev`. Both
  files are written by `apl_start` ([version-display-and-override.md](version-display-and-override.md)).
- The same screen shows `"[CH1]JOG uCom ERROR!!"` / `"[CH2]JOG uCom ERROR!!"` from status bytes
  `param+0x3e` / `+0x3f`.
- The `E-7206 AUTH CHIP ERROR` has nothing to do with the panel. It is the Apple MFi coprocessor
  on I2C4, emulated by `qemu/xdj/xdj-mfi.c` ([service-modes.md](service-modes.md)).

## 8. The emulated panel MCU (`qemu/xdj/xdj-panel.c`)

`xdj-subucom` is an SSI slave on ECSPI2 (bus 1, CS 0). It has three jobs:

- **Normal mode:** it presents a `SubRxData` frame on MISO and captures `SubTxData` from MOSI.
- **Bootloader mode:** it acts as the MCU's flash bootloader for firmware updates. It enters this
  mode when a 100-byte request has `[0x13] = 0x55` and every other byte zero. The REQ/ACK
  handshake runs on GPIO1_IO18 / GPIO1_IO16. Flash persists to `$XDJ_PANELCOM_IMG`. Protocol:
  [firmware-update-and-nand.md](firmware-update-and-nand.md).
- **Side channels** read once per burst: MAIN-UCOM keys and USB fault flags.

### Burst hook

`qemu/patches/02-imx6-peripherals.patch` makes `imx_spi_flush_txfifo` call
`xdj_subucom_frame_start()` (a weak symbol) at the **start of every ECSPI burst**. That hook
builds the frame so it is byte-aligned with the burst. **Without it the slave sends all-zero
frames and no control reaches rbp.** At the same point the hook finishes the previous burst's
MOSI capture.

### Frame build, per burst

1. Zero a 100-byte logical frame.
2. `[2..3]` = the version word of the panel firmware in the emulated flash. It is scanned from
   `$XDJ_PANELCOM_IMG`, or 0 when nothing is installed. `XDJ_SUBUCOM_VER=<hex>` overrides it.
   rbp only rejects `0xffff`.
3. Apply every **hold** (up to 64): `A` = write a LE 16-bit value, `B` = OR a bitmask.
4. Merge the **auto-level** set (below) for any offset the operator is not driving.
5. Apply the current **one-shot** on top, if one is active.
6. Flip `0x5d.0` so that "POWER held" in the injection grammar becomes the active-low wire
   value.
7. Compute the CRC over the word-reversed first 96 bytes and store it at `0x62`.
8. Word-reverse the frame onto MISO.

**Auto-level.** About 4 s after rbp's first poll, the harness holds an audible default for every
control the operator is not driving: both channels routed to DECK (`0x33.7`, `0x43.7`), channel
faders, TRIM and MASTER up, EQs, COLOR and crossfader centred. A loaded deck is then audible
without manual setup. `XDJ_AUTOLEVEL=0` disables it; `XDJ_AUTOLEVEL_DELAY_MS=N` changes the
settle delay.

**MOSI capture.** At the end of each burst the slave un-reverses the MOSI bytes and checks the
`SubTxData` CRC. When the payload has changed, it publishes one line `T <40 hex> <srcled>`. The
21st byte mirrors GPIO outputs 162–165. In socket mode the line goes up the `xdjctrl` chardev; in
file mode it atomically replaces `$XDJ_TX_PATH` (default `/tmp/subucom_tx`).

**USB fault flags.** `touch /tmp/usb_insert` (USB1) or `/tmp/usb2_insert` (USB2) drives the
fault line LOW, and rbp shows "USB Error. Remove the device." Removing the file clears it. A
normal USB insert must leave these lines alone; use `device_add` ([usb.md](usb.md)).

**MAIN-UCOM keys.** A GPIO number on the `xdjkey` chardev, or in `$XDJ_KEY_PATH` (default
`/tmp/mainkey`), holds that key LOW. An empty record or no file releases it. Reset levels for
these pins (HIGH) are set in `xdj-board.c`.

## 9. Injecting controls

### Channels

| Mode | Used by | Input | LED output |
|---|---|---|---|
| Socket | the desktop app (`xdj-host`); `qemu/watch.sh` / `tools/xdj-panel/debug/relaunch.sh` with `XDJ_RUNTIME_DIR` set (`server.py` then talks to the sockets) | chardev `id=xdjctrl`; each NUL-terminated record is the **whole** desired state | `T …` lines written back on the same socket |
| File | the same launchers without `XDJ_RUNTIME_DIR`; `server.py`, `inj.py` and other scripts write the file | `$XDJ_CTRL_PATH` (default `/tmp/subucom_ctrl`), read every burst | `$XDJ_TX_PATH` (default `/tmp/subucom_tx`) |

The slave uses socket mode when a chardev with that id exists; otherwise it falls back to the
file.

When a client disconnects, socket mode keeps the last snapshot, so holds survive a harness
restart. Both chardevs are bound when the device is realized, so a snapshot sent at QEMU start is
in place before rbp's first poll. Boot-key combinations (test mode) depend on that.

### Grammar

One spec per line. Offsets and values are hex, in logical frame coordinates.

| Line | Meaning |
|---|---|
| `B <off> <mask>` | hold bits `mask` set while the line is present |
| `A <off> <val16>` | hold a LE 16-bit analog value while the line is present |
| `b <off> <mask>` | one-shot button: pressed for 22 frames, then released for 8 |
| `a <off> <val16>` | one-shot analog value, same timing |

Only the first one-shot line in a snapshot is used.

- **File mode:** a one-shot fires when the slave sees it. If the file holds only the one-shot, the
  file is deleted. If the file also holds `B`/`A` lines, it stays, and **the one-shot re-fires
  every 30 frames**. Rewrite the holds alone right after firing.
  `tools/xdj-panel/debug/inj.py pulse 'b 5d 10'` does exactly that, keeping the analog-gain base
  holds; `inj.py mainkey <gpio>` pulses a MAIN-UCOM key.
- **Socket mode:** a one-shot fires once per fresh record.

### Practical rules

- rbp only reacts to **changes**. A hold produces one press when added and one release when
  removed. Many controls (TEMPO RANGE, FX select, …) are **sticky toggles** on screen, so
  pressing twice returns to the starting state.
- Keep encoder and jog counters in every frame once moved. The web panel's `_compose_ctrl`
  (`server.py`) and the app's panel code do this.
- Set both halves of the tempo fader.
- Hold boot-key combinations *before* boot (test mode = `B 13 04` + `B 12 08`). rbp checks only
  the first frame.

### Diagnostics (environment variables)

| Variable | Effect |
|---|---|
| `SUBUCOM_TRACE=1` | log every MOSI byte (`SUBUCOM [n] mosi=xx`) |
| `SUBUCOM_BURST=1` | log each burst length |
| `SUBUCOM_BADCRC=1` | corrupt the CRC, so rbp decodes nothing (negative control) |
| `SUBUCOM_RAMP=1` | send a `0,1,2,…` ramp instead of the frame (reveals the wire → buffer mapping) |
| `XDJ_FRAME_LOG=1` | trace the first 12 frames (`XDJ-FRAME n t= snap= [12]= [13]= [33]=`) |
| `XDJ_TX_LOG=1` | log every changed MOSI burst (`SUBUCOM-TX <len> <hex>`) |
| `XDJ_BL_LOG=1` | log bootloader activity and the version word |

## 10. Mapping a control (method)

The `map-subucom-{button,fader,knob,rotary}` and `map-gpio-control` skills give the full
procedures. The core techniques:

- **Statically:** read the decoder for the section with `objdump` and list each
  `ldrb #off … tst #mask → code` block. Turn `decoder_off` into a frame offset with the section
  base (`0x04` deck, `0x44` centre).
- **At runtime:** break on the dispatcher `0x2a47c0` (`r1` = code, `r2` = state, `r3` = deck)
  while injecting one bit or field. Name the code with the debug-console vocabulary. Keep halts
  sparse and step off each breakpoint (`rbp-binary-internals` skill).
- **Visually:** take a screendump before and after **each** injection and diff the hashes. Do
  not compare against a fixed baseline, because sticky toggles accumulate. Many reactions are
  small text changes (`TEMPO ±10` → `±16`, `500 ms` → `10 ms`), so compare hashes, not
  impressions. For analog fields, sweep a few values (`0x000`, `0x200`, `0x3ff`).
- **Oracles:** test mode's Switch/Key/Jog/Volume pages show raw panel state on the LCD
  ([service-modes.md](service-modes.md)). For LEDs, diff the published `SubTxData` with
  `tools/xdj-panel/debug/ledprobe.py`.
