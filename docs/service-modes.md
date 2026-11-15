# XDJ-RX service modes: test mode, factory reset, firmware update, standby

Sources: the XDJ-RX **service manual** (manualslib 1307531, 210 pp.; Pioneer-owned and *not* in the
repo — page numbers are the manual's printed numbers), cross-checked by static RE of `rbp` and the
initramfs and verified in the emulator.

## Summary

| mode | real device (hold, then power on) | how the firmware decides | emulator |
|---|---|---|---|
| **Test mode** (8 sub-modes) | DECK 1 **MEMORY** + DECK 1 **TEMPO RANGE** | rbp, first sub-ucom frame: `[0x12]&0x08` + `[0x13]&0x04` | ✅ hold `B 12 08` + `B 13 04` from boot |
| **Factory reset** | DECK 1 **DELETE** + DECK 1 **TEMPO RANGE** | rbp, first frame: `[0x12]&0x08` + `[0x13]&0x08` | not exercised (resets settings) |
| test variant 2 | TEMPO RANGE + **RELOOP/EXIT** | rbp, first frame: `[0x12]&0x88` → `TestMode(…, 2)` | not exercised; not in the manual |
| **Firmware update** | DECK 1 **USB STOP** + DECK 1 **RELOOP/EXIT** | sub-ucom MCU raises **GPIO3_IO30** (gpio 94); initramfs `/init` reads it | ✅ `XDJ_BOOT_UPDATE=1` (`xdj-board.c`) |

## Test mode
Manual §6.1, pp. 36–40. Eight sub-modes; **turn** the rotary selector (browse encoder) to change,
**press** it to wrap 8↔1. If a JOG uCom fails, Mode 1 shows `[CHn]JOG uCom ERROR!!` and nothing
works until BEAT FX ON/OFF is pressed.

| mode | content | use in the emulator |
|---|---|---|
| 1 | VERSION INFORMATION: SYSTEM / SUB / APL / KERNEL, language, MAC, JOG uCom status | SUB = the firmware in the emulated MCU flash (`XDJ_PANELCOM_IMG`; `0.00` if none; `XDJ_SUBUCOM_VER=<hex>` overrides) |
| 2 / 3 | all LEDs + LCDs off / on | LED-bit oracle (everything set) |
| 4 | LCD patterns (push = colour/black/white, BACK = back) | — |
| 5 | **KEY/LED TEST**: each key lights its LED | LED-map oracle ([subucom-led-map.md](subucom-led-map.md)) |
| 6 | **SW TEST**: DECK/EXT, PHONO/LINE, XF CURVE, MIC; CH1/CH2/master meters follow the faders/crossfader | switch oracle |
| 7 | every pot/fader value as `000–3FF` hex (TRIM, EQ, COLOR, FADER, TEMPO, TOUCH/RELEASE, MIC EQ, HP, XF, LEVEL/DEPTH, BOOTH, MASTER) | analog-map oracle: reads back exactly what the frame sends |
| 8 | SELECTOR / TIME rotary counters `00–FF` | encoder oracle |

### Entering it
- **App**: File > *Power On in Test Mode* (`POST /power on test`). The panel holds `B 12 08` +
  `B 13 04`, cold-boots (power-cycling first if on) and releases the keys once rbp is certainly past
  its first-frame check (the console's first `ALSA: Resolve xrun`, + 2 s; fallback 3 min).
- **Dev** (file mode, debug launcher):
  ```bash
  printf 'B 13 04\nB 12 08\n' > /tmp/subucom_ctrl     # hold MEMORY + TEMPO RANGE from power-on
  ./tools/xdj-panel/debug/relaunch.sh                 # boots straight to Mode 1 VERSION INFORMATION
  rm /tmp/subucom_ctrl                                # release; then turn/push the browse encoder
  ```
- The harness binds its `xdjctrl` chardev at realize (`xdj_subucom_realize`), so a snapshot sent at
  QEMU start is in the very first frame. `XDJ_FRAME_LOG=1` traces the first 12 frames on QEMU's
  stderr (`XDJ-FRAME n t= snap= [12]= [13]= [33]=`): frames 0–2 come from the kernel driver at probe,
  rbp's first poll follows ~20 s later.

### Known issues (open)
- **~1 boot in 8 comes up in the normal UI** although the frame trace shows both keys in every frame
  from the first (including rbp's first poll), so it is rbp-side timing, not the frame. Seen at
  random and on the first boot after a firmware update. `tools/test/regress.py` retries test mode
  once and reports it.
- Once, the `TestModeView` thread crashed (SIGSEGV inside a shared library), leaving a garbled
  screen; a reboot recovers.

### How rbp enters it
Static + live, rbp sha `4f62aa71…`:
- `Base::setBinaryData()` (`0x284d48`) parses each sub-ucom frame. When there is **no previous frame**
  (index `0xffff`, i.e. the first frame after start) it calls the **boot-key handler `0x282630`**:
  - `[0x12]&0x08` (TEMPO RANGE) and `[0x13]&0x04` (MEMORY) → `new ui::TestMode(…, 0)` (ctor
    `FUN_0023b5e8`), pushed to the UI manager `*0x123e244`;
  - TEMPO RANGE + `[0x13]&0x08` (DELETE) → factory-reset path (`*0x123e1f8` vtable+8, then a
    message);
  - `[0x12]&0x88` (TEMPO RANGE + RELOOP) → `TestMode(…, 2)`;
  - it also stores frame bytes 2–3 (the **sub-ucom version word**) at `+0x94`.
- `ui::TestMode` sub-views: `VersionTestMode`, `LedLcdTestMode`, `LcdPatternTestMode`,
  `KeyTestMode`, `SwitchTestMode`, `VolumeTestMode`, `SelectorTestMode`, `JogTestMode`.
- SW TEST reads switchState `+0xa0/+0xb0`: `0` → LINE, `≠0` → PHONO (`0x2409d0`); the GPIO 119/120
  level is stored as `(level != 0)` (`0x23bd1c`) ⇒ rear switch HIGH = PHONO. (Not emulated.)
- SUB version is printed `"%x.%02x"` of (hi, lo): `0x0035` → `0.35`. rbp rejects only `0xffff`.

## Factory reset
Manual p. 41. DELETE + TEMPO RANGE at power-on resets AUTO CUE, Vinyl mode, TIME MODE, QUANTIZE,
TEMPO RANGE, MASTER TEMPO and the pad mode (not the language). Settings live on the unit's storage:
the app's NAND unit is persistent, while the debug launcher's SD image is `snapshot=on`, so a reset
there is discarded on exit.

## Firmware update mode
Manual §8.2 p. 60, §5.1–5.2 pp. 20–22. Full emulator details:
[firmware-update-and-nand.md](firmware-update-and-nand.md).
- **Device**: copy `XDJRX.UPD` to the root of a USB stick, hold USB STOP + RELOOP/EXIT while
  powering on, insert the stick → the update runs → "Firmware update is complete."
- **Hardware**: the sub-ucom ("EUP UCOM", IC3601) sees the keys and raises the update request (pins
  19–26 on p. 23: `EUP_CONTI`, `EUP_REQ`, `EUP_RDY`, `EUP_WRITE`); on the i.MX6 side that is
  **GPIO3_IO30**.
- **Firmware** (initramfs `/init`, inside the kernel image): `regio T,D,020A4000,40000000` reads GPIO3
  `DR` bit 30 → `update_key=1`; u-boot env `boot_type=update` forces update too. It shows image
  **D001** "Connect USB storage device into top USB1 port.", loops in `mount_nandupdate` until a
  stick with the UPD mounts, runs `check_sig`, then `decrypt_update` → the ISO's `update` program.
  `update` reflashes the NAND MTD partitions (`/dev/mtd0..10`, UBI volumes) and the sub-ucom
  (`images/PANELCOM.mot` over `/dev/subucom_spi1.0`: `enterUpdateMode`, `getUpdateType`), with
  MAIN/SUB progress bars (manual p. 22).
- The rootfs also carries the same logic, commented out, in `/root/pioneer/versionup/version_up.sh`,
  plus an SD path: a `nandupdate/` dir on the SD card runs `./update SD 0`, then powers off.
- **Emulator**: `XDJ_BOOT_UPDATE=1` raises GPIO3_IO30 (`xdj-board.c`) → D001. The panel MCU's
  bootloader (`xdj-panel.c`) and the NAND (`xdj-nand.c`) are emulated, so both SUB and MAIN bars
  complete.
  - App: File > *Install Firmware Update…* (puts the `.UPD` on a stick and cold-boots into update
    mode), or File > *Enter Firmware Update Mode* + a USB LOAD on USB1.
  - Dev: `XDJ_BOOT_UPDATE=1 XDJ_BOOT_MEDIA=nand XDJ_SD=none XDJ_NAND_IMG=<copy of a unit>
    XDJ_PANELCOM_IMG=<…> XDJUSB=<FAT image with XDJRX.UPD> ./tools/xdj-panel/debug/relaunch.sh`, then
    on the monitor `device_add usb-storage,bus=usb-bus.1,port=1.2,drive=usbstick,id=xdjusb1,removable=on`
    (USB1). `XDJ_BL_LOG=1` traces the MCU bootloader. The NAND and MCU-flash images are written in
    place: work on copies, and never use a real stick.

## POWER key: standby handshake
Static + live. POWER is not a power cut: the key reaches rbp through the panel MCU as frame bit
**`0x5d.0`** (code **`0x8001`**, **active-low**: decoder `0x283678`, bit clear = pressed), and rbp
runs a deliberate standby sequence:
- `ui::PowerManager` (vtable `0x427cc8`, implements `uif::IPowerManager`: `reqStandbyOn`,
  `notifyPreparedToStandby`, `notifyAutoStandbyTime`) notifies the `IStandbyReqReciever`s and waits
  for them to report "prepared" (`PowerManager::WaitTimer` bounds the wait). The same path serves
  **auto standby** (`IAutoStandbyPermitter`, the AUTO STANDBY setting).
- Then `ui::PanelComController` (vtable slot 2, `0x21a350`) sets its standby flag (`+0x50`) and every
  TX frame carries payload byte **`0x11 = 0x53` ('S')** with the LEDs cleared — "i.MX6 ready, cut
  me". The panel MCU (which owns the power rails and the STBY/MASTER REC LED) then powers the i.MX6
  down. A second press does nothing.
- What "prepared" covers: rbp has **no `sync()` call**; `umount`/`umount2` exist only in the USB
  storage path (`0x296628`, `0x298170`). The root fs is read-only cramfs and the writable data/gui
  are UBIFS (journaled), so the handshake mainly lets rbp finish what it is doing (e.g. stop a USB
  recording, settle settings). Which receivers do what is not traced.

**In the emulator**: the harness inverts `0x5d.0` (`xdj-panel.c`: no hold → 1 = idle, `B 5d 01` → 0
= pressed). The panel server plays the MCU's part: POWER off while rbp runs a normal boot presses
the key and waits up to 10 s; whenever rbp's TX carries `0x11 = 'S'` (a POWER press *or* rbp's own
AUTO STANDBY) it cuts power (`standby_cut` in `app/host/src/panel.rs`, `_standby_cut` in
`tools/xdj-panel/server.py`). While booting, in update mode, or with no answer, POWER off is a plain
power cut. rbp answers in ~0.2 s when idle. Regression: `regress.py` stage `standby`.

## Auth chip
`E-7206 AUTH CHIP ERROR` = the Apple MFi authentication coprocessor on **I2C4** (`0x021F8000`,
`/dev/i2c-3`, slave 0x10) is missing. rbp's self-check (`FUN_00349294`) reads reg `0x00` (must be
`0x05`) and `0x01` (must be `0x01`); register accessors at `0x349294…0x34a640`, open/`I2C_SLAVE` at
`0x34ae5c`, reset via `/dev/gpiodrv` at `0x34b02c`. `xdj-mfi.c` adds I2C4 and an identity-only
responder.

## Other manual facts
- Startup sequence (p. 20): the sub-ucom resets first (MASTER REC LED flashes red), decides
  update-vs-normal, then powers the i.MX6; a normal startup then begins "communication between EUP
  UCOM and i.MX6".
- The MASTER REC LED and the jog LEDs are driven by the sub-ucom itself (pin table p. 23:
  `STBY_LED`, `JOG*_LED_*`); keys wired directly to the MCU include QUANTIZE, PLAY/CUE, the pads and
  jog touch.
