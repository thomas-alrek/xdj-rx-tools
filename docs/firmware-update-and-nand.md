# Firmware update and NAND

The emulator runs Pioneer's own firmware updater to completion — panel MCU (SUB) and main flash
(MAIN) — and then **boots the installed firmware from the emulated NAND**, like the real unit. A
blank unit is provisioned the way the factory does it (an ALL update from USB), and any `.UPD`,
official or custom ([custom-firmware.md](custom-firmware.md)), installs through the same path.
Firmware stays untouched; everything is QEMU- and host-side:

- **SUB** — the panel MCU's flash bootloader, in `qemu/xdj/xdj-panel.c` (the GPIO handshake is
  wired in `qemu/xdj/xdj-board.c`).
- **MAIN** — APBH-DMA + GPMI + BCH + a 256 MiB NAND chip, in `qemu/xdj/xdj-nand.c`.
- **Boot** — the unit's own chain: an emulated i.MX6 boot ROM loads u-boot from mtd0, u-boot
  boots the kernel (`qemu/xdj/xdj-bootrom.c`, §4). A blank unit's provisioning boot, and
  `XDJ_DIRECT_BOOT=1`, host-load the kernel instead (`xdj nand boot`, `crates/xdj-fw/src/nand.rs`).

The desktop app does all of this automatically ([standalone-app.md](standalone-app.md) §4a); the
manual recipes are in §5.

## 1. The update flow on the unit

Update mode (`XDJ_BOOT_UPDATE=1`, or the app's *Enter Firmware Update Mode* / *Install Firmware
Update*) → the update initramfs prints `### USB update mount. ###` and loops mounting
`/dev/sd[a-e]{1,}` until `XDJRX.UPD` shows up (the stick goes on **USB1**, [usb.md](usb.md)) →
`check_sig` ("signature ok. version = 2.21") → `decrypt_update` → `./update USB <language>`
(MAIN/SUB progress screen) → `### USB update success. (...) ###` or
`### USB update error. (...) error_code = N ###`. What each step checks is in
[custom-firmware.md](custom-firmware.md) §3.

`XDJ_BOOT_UPDATE=1` boots with GPIO3_IO30 (gpio 94, the EUP update request) HIGH
(`xdj-board.c`); unset, it is LOW = normal boot.

## 2. SUB: the panel MCU bootloader (`xdj-panel.c`)

The updater (`SubucomRequestManager::{enterUpdateMode, getUpdateType, erase, write,
request_program, request_crc, reset}`, `SpiDownloader`) talks to the MCU over the same
`/dev/subucom_spi1.0` as the normal panel frames. Payload: `images/PANELCOM.mot`, split by
`mot2bin` into `.bot` (0x10000–0x14000), `.apl` (0xC0000–0xF0000), `.fix` (0xF0000–0x100000).

### Transport
- `openDevice`: forces 32-bit words (ioctl `0x40027003` = 32) and **stops the driver's periodic
  timer** (ioctl `0x40017001` = 0) — from then on SPI bursts happen only on the updater's own
  `write()` (request) and `read()` (response).
- **GPIO handshake** via `/dev/gpiodrv` (`lseek(gpio)` + 1-byte read/write; offset = Linux GPIO):
  host REQ = **GPIO 18 (GPIO1_IO18)** output, MCU ACK = **GPIO 16 (GPIO1_IO16)** input
  (EUP_REQ / EUP_CONTI, service manual p. 23). `request_wait(ms)`: REQ:=0, poll ACK==1 (1 ms
  steps), REQ:=1. `request_done(ms)`: poll ACK==0, REQ:=0.
- **enterUpdateMode(crc)**: 100-byte request, bytes 0–3 = 0, `[0x13]=0x55`, check value over
  bytes 0..0x13 at `[0x14..0x17]` (CRC16 mode: `common_crc16` byte-swapped in a 32-bit word;
  CRC32 mode: `common_crc32`, `rev`), whole buffer word-reversed (`rev` per 32-bit word) →
  `write(100)` → `request_wait(1000)` → `read(16)` → `request_done(10000)`. Response (after the
  per-word `rev`): `[0]=0xFE`, `[1]=0x00` = OK; `upd_crc_ccitt16_go8(resp, 12, 0xFFFF)` must equal
  the LE halfword at `[12..13]`.
- `upd_crc_ccitt16_go8`: CRC-CCITT poly 0x1021, MSB-first, given init (0xFFFF), no xorout,
  **each input byte bit-reversed first** (`TblRfctData` = a bit-reverse table).
- **request_function(cmd, sub, len, addr, data)**: 0x40C-byte packet, 0xFF-filled;
  `[0]=cmd [1]=sub [2..3]=len LE [4..7]=addr LE [8..]=data`, `ccitt16_go8(pkt, 0x408, 0xFFFF)` LE
  at `[0x408..9]`. `request_wait(1000)` → `write(0x40C)` → `request_done` → `request_wait` →
  `read(16)` → `request_done`. Response: `[0]=cmd [1]=status (0 OK / 1 NG)`, payload from `[4]`,
  CRC over 12 bytes at `[12..13]`.
- Commands: `F1` init(type), `F2` erase(addr), `F3` program(addr, ≤ 0x400), `F5` crc(addr, u32 n
  → `[4..5]`, must equal `ccitt16_go8` of the file), `F9` reset, `FA` update type (`[4..7]`
  string, `[8]` change flag, `[10]`: `0x10` normal (USER), `0x20` ALL, `0x15` SERVICE, else
  UNKNOWN).
- Byte order: the updater `rev`s each 32-bit word and the ECSPI shifts words MSB-first, so the
  wire carries the logical bytes in order — the MCU model needs no swapping in bootloader mode.

### The model
The enter request is recognised at byte 100 of a normal-mode burst → bootloader mode (normal
frame building is bypassed). The handshake state machine runs off GPIO1_IO18 edges
(`xdj_subucom_bl_wire`, wired in `xdj-board.c`) and drives GPIO1_IO16. The MCU flash is a 1 MiB
host file (`XDJ_PANELCOM_IMG`), saved on reset.

A normal (USER) update's command sequence: `FA`, `F1`, erase `0x00EFFE` (4 KiB flag block) +
program 4 bytes at `0xE000`, erase `0x0E/0D/0CFFFE` (64 KiB blocks), program 1 KiB pages, `F5`
over `0xC0000`, erase the flag block, `F9` — ~200 commands, ~0.2 s, after which `0xC0000–0xEFFFF`
equals the `.mot` byte for byte. A USER update never writes `.bot`/`.fix`.

### SUB version
Frame bytes 2–3 (the sub-ucom version `rbp` and test mode show) come from the flash: PANELCOM is
M16C/60 code (fixed vectors at `0xFFFDC`, reset → `0x12C5A` in `.bot`). The APL builds its SPI TX
frame at RAM `0x1328`; the first word is four byte-immediate stores (`b7 28 13` / `c7 35 29 13` /
`b7 2a 13` / `b7 2b 13` at `0xC9CEB` = 0x00, 0x35, 0, 0) followed by `72 ff <src> 2c 13` copying
the body. `xdj_bl_scan_version()` matches that shape (unique in 2.21) → version `0x0035` → test
mode "SUB Ver.0.35". No flash or no match → 0 (`rbp` only rejects `0xffff`);
`XDJ_SUBUCOM_VER=<hex>` overrides. Disassembler: binutils 2.42 `--target=m32c-elf
--with-system-zlib`, `make MAKEINFO=true all-binutils`, `objdump -b binary -m m16c` (it shows
some valid opcodes as `*unknown*`, e.g. `e7` = `cmp.b:s #imm,abs16`).

The "current SUB version" on the update screen (`????` on a blank unit) is the u-boot env
`rev_subucom` (`rbp` main `0xb680`; likewise `rev_kernel`, `rev_pioneer`, `release`), not the MCU.

## 3. MAIN: NAND (`xdj-nand.c`)

The updater's `NandDownloader` / `UbootNandDownloader` / `UbifsNandDownloader` write
`/dev/mtd0..10` (u-boot, kernel A/B, rootfs cramfs, pioneer, data/gui UBI).

- **APBH-DMA**: CCW chains (PIO words → GPMI, CMD_READ/WRITE data phases, CHAIN/DEC_SEM/IRQ,
  SEMA kick; SPI 13).
- **GPMI**: CTRL0 operations — CLE + ADDRESS_INCREMENT → ALE, raw data, wait-for-ready, ECC pages.
- **BCH**: FLASH0LAYOUT bit-packed `[meta][data0][ecc0][data1]…`, status 0x00 / 0xff-erased,
  COMPLETE_IRQ; SPI 15. Parity is written as zeros — the kernel and u-boot never check it, and
  the emulated boot ROM doesn't either (§4).
- **Chip**: a non-ONFI Samsung K9F2G08U0C-style part, ID `EC DA 10 95 44`, 2048+64-byte pages,
  64 pages/block, 2048 blocks (256 MiB).
- **Backing file** `$XDJ_NAND_IMG`: raw pages with OOB (2112 bytes each, page-major), created
  erased (all 0xff); unset = no chip. Raw page layout (BCH t=8, GF13, 4 chunks):
  `[10-byte metadata][512 data0][13 ecc0]…[512 data3][13 ecc3][2 spare]`, with the block-mark
  swap logical byte 1999 ↔ metadata[0].
- `XDJ_NAND_LOG=1` logs unhandled commands.

The kernel gets `mtdparts=` on its command line: from the firmware's own `images/fw_env.txt`
when host-loading the update kernel (`xdj prepare`), or from the NAND env's bootargs on a NAND
boot — real u-boot passes it from its env likewise.

In the guest: `mxs-dma-apbh: initialized`, `Samsung NAND 256MiB`, `ecc_strength = 0x4` (t=8), 17
command-line partitions. `flash_erase` / `nandwrite` / `nanddump` round-trip, and
`ubiformat` / `ubiattach` / `ubimkvol` / UBIFS persist across reattach.

### What an update writes
- **USER update** (MCU type `0x10`): PANELCOM → the *other* kernel bank (mtd3 = A / mtd4 = B,
  md5 read-back) → mtd5 rootfs.cramfs → mtd7 pioneer → mtd9/mtd10 gui (ubiformat) →
  "Firmware update is complete." It doesn't write mtd0 (u-boot / kobs-ng), doesn't create the
  `data` UBI volume and sets `ubifs_formatted=true`.
- **ALL update** (type `0x20`, `XDJ_UPDATE_TYPE=20`): additionally mtd0 `u-boot.bin.nand` and
  mtd6 + mtd8 `data.tar.gz`. Its last step, `finEnv`, writes the NAND env: `kernel_bank`,
  `release`, `rev_*`, `update_status=success`, `ubifs_formatted=true`. The installer's real end is
  `umount /mnt/usb` after the D007 "complete" screen, not the last partition.

A blank unit must be provisioned with an **ALL** update: after a USER update on blank NAND there
is no `data` volume and apl_start's first-boot init is skipped (`ubifs_formatted=true`), so `rbp`
hangs on the splash.

## 4. Booting from NAND

- **Boot strap**: `XDJ_BOOT_MEDIA=nand` sets `SRC_SBMR1` BOOT_CFG1 = 0x80 (a weak hook in
  `qemu/patches/02-imx6-peripherals.patch` → `xdj_src_sbmr1()` in `xdj-board.c`). `fw_printenv`
  and `bootmod` read it via `/dev/mem` (≥ 8 = NAND), so the env is read from mtd2.
- **Boot ROM → u-boot → kernel** (`XDJ_BOOT_ROM=1`, what the app uses for every installed unit;
  `qemu/xdj/xdj-bootrom.c`). CPU0 starts at the reset vector in the ROM region, as on the chip:
  1. **FCB**: page 0 of blocks 0–3; valid = fingerprint `FCB ` + checksum (`~` byte sum of FCB
     bytes 4–511). kobs-ng writes it at raw byte 12 of the page. It gives the page geometry, the
     BCH layout (10 metadata bytes, 4 × 512-byte blocks, ECC type 4 = t8), the bad-block-marker
     byte (1999) and `Firmware1/2_startingPage` (512 / 1024; the DBBT is in blocks 4–7).
  2. **Firmware pages** are de-interleaved with that layout; data byte 1999 comes back from
     metadata[0] (the marker swap). Both copies decode byte-identical to the firmware's
     `u-boot.bin.nand`. Firmware2 is the fallback; an image read runs past
     `PagesInFirmware` up to where the other copy starts (the plugin asks for 1 KiB more).
  3. **IVT at image offset 0x400**: u-boot 2009.08's is a **plugin** (16 KiB to OCRAM
     `0x907000`). The ROM calls `plugin(&start, &bytes, &ivt_offset)`; the plugin sets up the
     DDR (with calibration) and calls the ROM's `pu_irom_hwcnfg_setup` (ROM API table at
     `0xC0`, version word at `0x48`), then answers 0x37AF8 bytes → `0x17800000`, IVT at `+0x42C`.
     The ROM loads that and jumps to u-boot (entry `0x178009A0`). A DCD, when an IVT has one,
     is applied (write commands).
  4. **The MMDC** (`0x021B0000`/`0x021B4000`) is a register file in which every calibration
     completes without errors: `MDSCR.CON_ACK` follows `CON_REQ`, and the trigger bits the
     calibration waits on read back clear. u-boot prints the calibration result
     ("Write Leveling success / calibration success").
  5. **u-boot** finds the NAND (`Boot Device: NAND`), reads its env from mtd2, runs
     `bootcmd_nand`: `nand read` of `kernel_bank`'s 5 MiB with **cache reads** (31h/3Fh, which
     the NAND model implements for it), checks the uImage CRC and boots it with its own ATAGs:
     revision `0x63510` (`& 0xF00 = 0x500`) and the env's bootargs, including its bare
     `video=mxcfb0` (the LVDS panel comes up at 800×480 with it). About 1 s from reset to
     "Starting kernel".
  The ROM program is a few ARM instructions in the ROM region; the loading is done in C behind a
  small control register (`0x0001F000`). Not modelled: HAB/signature checks (the unit's images
  aren't signed), the ROM's USB serial-download fallback, BCH parity checks.
- **Host-side boot** (provisioning a blank unit, which has no u-boot yet; `XDJ_DIRECT_BOOT=1`;
  a unit without an FCB): `xdj nand boot <img> <dir>` does `bootcmd_nand` on the host: reads
  `kernel_bank` from the env, reads that bank's uImage (`bootargs_nand_A/B`: `nand read
  ${loadaddr} <off> <len>`), and builds the bootargs from `bootargs_base` + `bootargs_nand`, with
  u-boot's bare `video=mxcfb0` replaced by the emulator's LVDS mode line → `uImage`,
  `vmlinux.bin`, `atags.bin` (revision `0x500`), `tramp.bin` in `<dir>`. Either way a firmware
  update done inside the emulator boots its new kernel on the next power-on.
- **Userland**: the initramfs takes `rootfs=nand`: into a 60 MiB tmpfs it `nanddump`s a fixed
  20 MiB of mtd5 (`rootfs.cramfs`, copied out with `cpdir`, then deleted) and a fixed 3 MiB of
  mtd7 (`pioneer.tar.gz`, unpacked into `/root/pioneer`), then `switch_root`s into it. apl_start
  sees `BOOTMODE=NAND` and mounts UBI data/gui, `rbp` starts → home screen. Those fixed lengths
  (and u-boot's 5 MiB kernel read) are smaller than the partitions: a custom firmware must fit
  them ([custom-firmware.md §2b](custom-firmware.md); `xdj fw check`).
- **Needs ≥ 2 CPUs** (`XDJ_SMP=2`): single-core makes apl_start run `rbp_solo`, which a stock
  install doesn't have.
- **No SD card** (`XDJ_SD=none`): a NAND unit has none.
- **Root shell**: the serial getty asks for the stock root password on a NAND unit, and that
  hash (`/etc/shadow`, DES crypt, salt `bE`) is not known. For shell-level debugging use the SD
  test bed (same `rbp` and kernel; its root password is cleared). Only the home-screen UI needs
  NAND. To get a shell on NAND specifically, blank root in the installed rootfs (custom firmware).

## 5. Manual recipes (dev)

Tools: the `xdj` CLI (`crates/xdj-fw`, `cargo build --release` → `crates/xdj-fw/target/release/xdj`)
and the debug launcher `tools/xdj-panel/debug/relaunch.sh` (passes its environment through to
QEMU; `XDJ_BOOT_DIR` picks the boot blobs, `XDJ_SD=none` drops the SD card, `XDJ_SMP` sets the
CPU count, `XDJUSB` the stick backend).

```sh
X=crates/xdj-fw/target/release/xdj
$X nand init  nand.img --upd XDJRX.UPD        # erased 256 MiB NAND, factory env (fw_env.txt) in mtd2
                                              # (or --iso XDJRX.iso / --env fw_env.txt)
$X prepare    XDJRX.UPD --out build/qemu      # the update's kernel + boot blobs, host-loaded
$X fw stick   XDJRX.UPD stick.img             # FAT16 stick with XDJRX.UPD at the root
```

**Provision (ALL update):**
```sh
XDJ_BOOT_UPDATE=1 XDJ_UPDATE_TYPE=20 XDJ_BOOT_MEDIA=nand XDJ_SD=none \
XDJ_NAND_IMG=$PWD/nand.img XDJ_PANELCOM_IMG=$PWD/panelcom.bin XDJUSB=stick.img \
  tools/xdj-panel/debug/relaunch.sh
# when the console shows "### USB update mount. ###", insert on USB1 (HMP monitor):
#   device_add usb-storage,bus=usb-bus.1,port=1.2,drive=usbstick,id=xdjusb1,removable=on
```

**Check and boot:**
```sh
$X nand info nand.img                         # rootfs, kernel_bank, release, ..., installed
$X nand env  nand.img                         # the full u-boot env
$X nand boot nand.img build/nand-boot         # host-side bootcmd_nand -> blobs (no XDJ_BOOT_ROM)
XDJ_BOOT_DIR=build/nand-boot XDJ_BOOT_MEDIA=nand XDJ_SD=none XDJ_SMP=2 \
XDJ_NAND_IMG=$PWD/nand.img XDJ_PANELCOM_IMG=$PWD/panelcom.bin \
  tools/xdj-panel/debug/relaunch.sh
```

A USER update of an installed unit is the same as provisioning without `XDJ_UPDATE_TYPE`
(default `10`), with the kernel from `xdj nand boot`.

## 6. Environment variables (QEMU side)

| var | meaning |
|---|---|
| `XDJ_BOOT_UPDATE=1` | boot in firmware update mode (GPIO3_IO30 HIGH) |
| `XDJ_UPDATE_TYPE` | update type the MCU reports, hex: `10` USER (default), `20` ALL, `15` SERVICE |
| `XDJ_PANELCOM_IMG` | panel MCU flash file (1 MiB; source of the SUB version) |
| `XDJ_SUBUCOM_VER` | override the SUB version word (hex) |
| `XDJ_BL_LOG=1` | one stderr line per bootloader command |
| `XDJ_NAND_IMG` | NAND backing file; unset = no NAND chip |
| `XDJ_NAND_LOG=1` | log unhandled NAND commands |
| `XDJ_BOOT_MEDIA=nand` | SBMR1 boot strap = NAND |
| `XDJ_BOOT_ROM=1` | CPU0 starts in the emulated boot ROM, which boots u-boot from the NAND (no kernel loaders) |

Host-side (the app): `XDJ_DIRECT_BOOT=1` boots an installed unit's kernel host-side instead of
through the boot ROM.

Launcher-side: `XDJ_BOOT_DIR`, `XDJ_SD=none`, `XDJ_SMP` (`relaunch.sh`).

## 7. Verification

Firmware 2.21, `XDJ_BOOT_UPDATE=1`, stick on USB1:

| check | result |
|---|---|
| SUB only (USER update) | SUB bar "???? → 0.35" 100 %, `PANELCOM.mot : Success`, ~200 commands, 0 NG; flash `0xC0000–0xEFFFF` equals the `.mot` |
| SUB version | test mode shows "SUB Ver.0.35" from the flashed MCU |
| USER update on NAND | "USER UPDATE ???? to 2.21": PANELCOM, mtd4 uImage (md5 read-back), mtd5, mtd7, mtd9/mtd10 all Success → "Firmware update is complete.", MAIN and SUB 100 % |
| ALL update (provisioning) | all partitions incl. mtd0 and data written; env `kernel_bank=B`, `release=2.21`, `rev_*`, `update_status=success`, `ubifs_formatted=true` |
| NAND boot | `rootfs=nand`, UBI data/gui mounted, `rbp` → home screen (2 CPUs) |
| custom firmware | a rebuilt image installs as a USER update and boots ([custom-firmware.md](custom-firmware.md) §5) |

The regression suite covers provisioning, NAND boot and *Install Firmware Update*
([testing.md](testing.md)).
