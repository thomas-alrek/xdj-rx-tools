# XDJ-RX service modes: test mode, factory reset, firmware update

Sources: the XDJ-RX **service manual** (manualslib 1307531, 210 pp.; *not* in the repo,
Pioneer-owned. Page numbers below are the manual's printed numbers) cross-checked by static RE of
`rbp` / the initramfs and verified in the emulator (2026-11-15).

## Summary

| mode | real device (hold, then power on) | how the firmware decides | emulator |
|---|---|---|---|
| **Test mode** (8 sub-modes) | DECK 1 **MEMORY** + DECK 1 **TEMPO RANGE** | rbp, first sub-ucom frame: `[0x12]&0x08` + `[0x13]&0x04` | ✅ hold `B 12 08` + `B 13 04` from boot |
| **Factory reset** | DECK 1 **DELETE** + DECK 1 **TEMPO RANGE** | rbp, first frame: `[0x12]&0x08` + `[0x13]&0x08` | not tried (resets settings) |
| test variant 2 | TEMPO RANGE + **RELOOP/EXIT** | rbp, first frame: `[0x12]&0x88` → `TestMode(…, 2)` | not tried; not in the manual |
| **Firmware update** | DECK 1 **USB STOP** + DECK 1 **RELOOP/EXIT** | sub-ucom MCU raises **GPIO3_IO30** (gpio 94); initramfs `/init` reads it | ✅ `XDJ_BOOT_UPDATE=1` (`xdj-board.c` (was patch 19)) |

## Test mode (manual §6.1, pp. 36–40)
Eight sub-modes; **turn the rotary selector** (browse encoder) to change, **press** it to wrap
8↔1. If a JOG uCom fails, Mode 1 shows `[CHn]JOG uCom ERROR!!` and nothing works until BEAT FX
ON/OFF is pressed.

| mode | content | use for us |
|---|---|---|
| 1 | VERSION INFORMATION: SYSTEM / SUB / APL / KERNEL, language, MAC, JOG uCom status | shows the version of the firmware in the emulated MCU flash (`XDJ_PANELCOM_IMG`; `0.00` if none; `XDJ_SUBUCOM_VER` overrides) |
| 2 / 3 | all LEDs + LCDs off / on | LED-bit oracle (everything set) |
| 4 | LCD patterns (push = colour/black/white, BACK = back) | — |
| 5 | **KEY/LED TEST**: each key lights its LED | **LED-map oracle**: the manual's key→LED table names LEDs we haven't mapped (USB1/2 STOP, MASTER REC, BROWSE, TAGLIST, INFO, MENU, TAP, LOAD1/2) |
| 6 | **SW TEST**: DECK/EXT, PHONO/LINE, XF CURVE, MIC; CH1/CH2/master meters follow the faders/crossfader | switch oracle |
| 7 | every pot/fader value as `000–3FF` hex (TRIM, EQ, COLOR, FADER, TEMPO, TOUCH/RELEASE, MIC EQ, HP, XF, LEVEL/DEPTH, BOOTH, MASTER) | **analog-map oracle**: reads back exactly what our frame sends |
| 8 | SELECTOR / TIME rotary counters `00–FF` | encoder oracle |

**From the app**: File > *Power On in Test Mode* (`POST /power on test`): the panel holds
`B 12 08` + `B 13 04`, cold-boots (power-cycling first if on), and releases them once rbp is fully
up (`ALSA: Resolve xrun`). Two races used to make it boot normally about half the time; both fixed
(2026-11-15): the harness bound its `xdjctrl` chardev lazily *on the first SPI frame*, so a snapshot
sent at QEMU start was read only after that frame was built — now bound at realize
(`xdj_subucom_realize`); and the keys were released on rbp's first LED frame, which can precede its
first parsed input frame. `XDJ_FRAME_LOG=1` traces the first 12 frames (`XDJ-FRAME n t= snap=
[12]= [13]= [33]=` on QEMU's stderr): frames 0–2 come from the kernel driver at probe, rbp's first
poll ~20 s later.

**How rbp enters it** (static + live, rbp sha `4f62aa71…`):
- `Base::setBinaryData()` (`0x284d48`) parses each sub-ucom frame. When there is **no previous
  frame** (index `0xffff`, i.e. the first frame after start) it calls the **boot-key handler
  `0x282630`** with the frame:
  - `[0x12]&0x08` (TEMPO RANGE) and `[0x13]&0x04` (MEMORY) → `new ui::TestMode(…, 0)` (ctor
    `FUN_0023b5e8`), pushed to the UI manager `*0x123e244`;
  - TEMPO RANGE + `[0x13]&0x08` (DELETE) → factory-reset path (`*0x123e1f8` vtable+8, then a
    message);
  - `[0x12]&0x88` (TEMPO RANGE + RELOOP) → `TestMode(…, 2)`;
  - it also stores frame bytes 2–3 (the **sub-ucom version word**) into `+0x94`.
- `ui::TestMode` sub-views: `VersionTestMode`, `LedLcdTestMode`, `LcdPatternTestMode`,
  `KeyTestMode`, `SwitchTestMode`, `VolumeTestMode`, `SelectorTestMode`, `JogTestMode`.
- SW TEST reads switchState `+0xa0/+0xb0`: `0` → LINE, `≠0` → PHONO (`0x2409d0`); GPIO 119/120
  level is stored as `(level != 0)` (`0x23bd1c`) ⇒ rear switch HIGH = PHONO.
- SUB version is printed `"%x.%02x"` of (hi, lo) — `0x0035` → `0.35`.

**Emulator recipe** (file mode, debug launcher):
```bash
printf 'B 13 04\nB 12 08\n' > /tmp/subucom_ctrl     # hold MEMORY + TEMPO RANGE from power-on
./tools/xdj-panel/debug/relaunch.sh                 # boots straight to Mode 1 VERSION INFORMATION
rm /tmp/subucom_ctrl                                # release; then turn/push the browse encoder
```
Caveats seen: 1 of 5 test-mode boots came up in the normal UI (cause not found); 1 crashed the
`TestModeView` thread (SIGSEGV inside a shared library) and left a garbled screen — reboot.

## Factory reset (manual p. 41)
DELETE + TEMPO RANGE at power-on resets AUTO CUE, Vinyl mode, TIME MODE, QUANTIZE, TEMPO RANGE,
MASTER TEMPO and the pad mode (not the language). In the emulator the settings live on the
`snapshot=on` SD image in the debug launcher, so a reset there is discarded on exit; the **app**
image is persistent.

## Firmware update (manual §8.2 p. 60, §5.1–5.2 pp. 20–22)
- Device: copy `XDJRX.UPD` to the root of a USB stick, hold USB STOP + RELOOP/EXIT while powering
  on, insert the stick → update runs → "Firmware update is complete."
- Hardware: the sub-ucom ("EUP UCOM", IC3601) sees the keys and raises **EUP_WRITE / update
  request** (pins 19–26 on p. 23: `EUP_CONTI`, `EUP_REQ`, `EUP_RDY`, `EUP_WRITE`). On the i.MX6
  side that is **GPIO3_IO30**.
- Firmware (initramfs `/init`, inside the kernel image): `regio T,D,020A4000,40000000` reads
  GPIO3 `DR` bit 30 → `update_key=1`. u-boot env `boot_type=update` forces update too. It then
  shows image **D001** "Connect USB storage device into top USB1 port.", loops in
  `mount_nandupdate` until a stick with the UPD mounts, runs `check_sig`, then `decrypt_update`
  → the ISO's `update` program. `update` reflashes NAND MTD partitions (`/dev/mtd0..10`, UBI
  volumes) and the sub-ucom (`images/PANELCOM.mot` over `/dev/subucom_spi1.0`:
  `enterUpdateMode`, `getUpdateType`), with MAIN/SUB progress bars (manual p. 22).
- Old rootfs path (`/root/pioneer/versionup/version_up.sh`) is the same logic, commented out,
  plus an SD path: a `nandupdate/` dir on the SD card runs `./update SD 0` then powers off.
- **Emulator:** `XDJ_BOOT_UPDATE=1 [XDJ_BL_LOG=1] XDJUSB=<fat32 img with XDJRX.UPD>
  ./tools/xdj-panel/debug/relaunch.sh` → D001 screen ✅; then `device_add usb-storage,bus=usb-bus.1,
  port=1.2,drive=usbstick,id=xdjusb1,removable=on` (USB1). `update` writes NAND MTDs (the SD-booted
  emulator has none yet) and talks the sub-ucom's update
  protocol — **now emulated** (`xdj-panel.c` SUB bootloader; the SUB bar reaches 100 %, see
  `docs/update-path-plan.md`). MAIN still fails until the NAND model exists. Use the debug
  launcher (`snapshot=on`), never the app's persistent image, and never a real stick.

## Other manual facts worth keeping
- Startup sequence (p. 20): sub-ucom resets first (MASTER REC LED flashes red), decides
  update-vs-normal, then powers the i.MX6; normal startup then "start of communication between
  EUP UCOM and i.MX6".
- The MASTER REC LED and the jog LEDs are driven by the sub-ucom itself (pin table p. 23:
  `STBY_LED`, `JOG*_LED_*`); direct keys on the MCU include QUANTIZE, PLAY/CUE, pads, jog touch.
- **Auth chip** (E-7206): an Apple MFi authentication coprocessor on I2C4 (`/dev/i2c-3`, slave
  0x10). rbp's self-check (`FUN_00349294`) reads reg `0x00` (must be `0x05`) and `0x01` (must be
  `0x01`); register accessors at `0x349294…0x34a640`, open/`I2C_SLAVE` at `0x34ae5c`, reset via
  `/dev/gpiodrv` at `0x34b02c`. `xdj-mfi.c` (was patch 18) adds I2C4 + an identity-only responder.
