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

## Open follow-ups
- Decompile the concrete `SubRxFormat` subclass → exact RX frame byte layout
  (section offsets, sizes, which bytes/bits = which button/jog/fader) and the XOR.
- Decompile `PanelComPeerLinux` SPI transfer + the `StaticInfomation` handshake.
- Map `playengine_Player` decoder selection (for FLAC).
