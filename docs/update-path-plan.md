# Plan: full firmware update in the emulator (SUB + MAIN, then boot from NAND)

Goal: `XDJRX.UPD` on a USB stick + update mode (`XDJ_BOOT_UPDATE=1` / File ▸ Enter Firmware Update
Mode) runs Pioneer's own updater to completion — panel MCU (SUB) and main flash (MAIN) — and the
emulator then **boots the updated firmware from NAND**, like the real unit. This makes the emulator a
true custom-firmware test bed (flash a `.UPD`, boot it). Firmware stays untouched; everything is
QEMU/host-side.

## Where it stops today (2026-11-15, user log)
Update mode → USB mount → `check_sig` ("signature ok. version = 2.21") → `decrypt_update` →
`update USB 0` (MAIN/SUB progress screen) → `SpiDownloader::enterUpdateMode` "TRY ENTER WITH CRC16 /
CRC32 … retry over" → `request_function(250): subucom_send_request: -101` → `getUpdateType` fails →
"UPDATE: ERROR UNKNOWN UPDATE TYPE", "MAIN firmware update failed". The init script then has nothing
left to do (as on hardware after a failed update).

Two missing pieces, visible in the (unstripped) `update` binary from the firmware ISO:
1. **SUB** — the panel MCU's flash bootloader: `SubucomRequestManager::{enterUpdateMode(EnCrcType),
   getUpdateType, erase, write, request_program, request_crc, reset}`, `subucom_send_request /
   read_response` over `/dev/subucom_spi1.0`, plus `GpioHandshake::request_wait/request_done`
   (the EUP_REQ / EUP_CONTI / EUP_WRITE lines, service manual p. 23). Payload: `images/PANELCOM.mot`
   split by `mot2bin` into `.bot` (0x10000-0x14000), `.apl` (0xC0000-0xF0000), `.fix` (0xF0000-0x100000).
2. **MAIN** — NAND: `NandDownloader` / `UbootNandDownloader` / `UbifsNandDownloader` write
   `/dev/mtd0..10` (u-boot, kernel A/B, rootfs cramfs, pioneer, data/gui UBI). QEMU has no i.MX6 NAND:
   `mxs-dma-apbh` reset times out and `gpmi-nand` fails to probe. And the emulator boots from an SD
   image, so even a successful NAND write would not be what boots.

## Phases
0. **References.** Pioneer GPL source (`fetch_key.py` URLs; v1.50 → `build/gpl/`, git-ignored):
   `linux-3.0.35` drivers `mtd/nand/gpmi-nand/*`, `dma/mxs-dma.c` (APBH), the BCH ECC layout,
   `char/subucom*` (the SPI driver + its GPIO handshake), `arch/arm/mach-mx6/board-mx6q_sabresd.c`
   (NAND pins/partitions), `u-boot-2009.08` NAND/env code. Plus the updater's `SpiDownloader` /
   `SubucomRequestManager` disassembly for the bootloader command set.
1. **SUB bootloader** (`qemu/xdj/xdj-panel.c`): a bootloader mode in the panel slave — enter (CRC16 or
   CRC32 framing), report update type (ALL), erase / program / CRC / reset; the GPIO handshake lines in
   `xdj-board.c`. Flash contents persisted to a host file (e.g. `XDJ_PANELCOM_IMG`); the reported SUB
   version could later come from it. Milestone: SUB progress bar reaches 100 %.
2. **NAND controller** (`qemu/xdj/xdj-nand.c`): APBH-DMA (command chains), GPMI (bus timing, CS),
   BCH (encode/decode as pass-through + metadata), a NAND chip with the real geometry/ID, backed by a
   host file with OOB. Milestone: `gpmi-nand` probes, `/proc/mtd` lists mtd0..10, `nanddump` /
   `flash_eraseall` / `nandwrite` work.
3. **MAIN update** end-to-end. Milestone: "Firmware update is complete."
4. **Boot from NAND.** Host side reads the kernel from the NAND image (`kernel_bank` A/B from the
   u-boot env partition) instead of `build/qemu/vmlinux.bin`, with `rootfs=nand` in the env — or run
   the real `u-boot.bin.nand` if the boot-ROM path is tractable. The initramfs then does what the
   device does (`nanddump` rootfs from mtd5, pioneer from mtd7, UBI data/gui).
5. **Provisioning + app.** Create the first NAND image by running the updater from the SD boot (the
   real factory path), keep SD boot as the fallback; app: choose boot medium, show the flash state.

## Findings so far
**SUB bootloader transport** (updater `SubucomRequestManager`, kernel `subucom_spi.c`, `gpiodrv.c`
from GPL 1.50):
- `openDevice`: `/dev/subucom_spi1.0`, force 32-bit words (ioctl `0x40027003`=32), **stop the
  driver's periodic timer** (ioctl `0x40017001`=0) — from then on SPI bursts happen only on the
  updater's own `write()` (request) and `read()` (response).
- **GPIO handshake** via `/dev/gpiodrv` (`lseek(gpio)` + 1-byte read/write; offset = Linux GPIO):
  host REQ = **GPIO 18 (GPIO1_IO18)** output, MCU ACK = **GPIO 16 (GPIO1_IO16)** input.
  `request_wait(ms)`: REQ:=0, poll ACK==1 (1 ms steps), REQ:=1. `request_done(ms)`: poll ACK==0,
  REQ:=0. (Service manual p. 23: EUP_REQ / EUP_CONTI.)
- **enterUpdateMode(crc)**: 100-byte request, bytes 0-3 = 0, `[0x13]=0x55`, check value over bytes
  0..0x13 at `[0x14..0x17]` (CRC16 mode: `common_crc16` byte-swapped in a 32-bit word; CRC32 mode:
  `common_crc32`, `rev`), whole buffer word-reversed (`rev` per 32-bit word) → `write(100)` →
  `request_wait(1000)` → `read(16)` → `request_done(10000)`. Response (after per-word `rev`):
  `[0]=0xFE`, `[1]=0x00` = OK; `upd_crc_ccitt16_go8(resp, 12, 0xFFFF)` must equal the LE halfword
  at `[12..13]`.
- `upd_crc_ccitt16_go8`: CRC-CCITT poly 0x1021, MSB-first, init given (0xFFFF), no xorout, **each
  input byte bit-reversed first** (`TblRfctData` = bit-reverse table, verified).

- **request_function(cmd, sub, len, addr, data)**: 0x40C-byte packet, 0xFF-filled; `[0]=cmd [1]=sub
  [2..3]=len LE [4..7]=addr LE [8..]=data`, `ccitt16_go8(pkt, 0x408, 0xFFFF)` LE at `[0x408..9]`.
  `request_wait(1000)` → `write(0x40C)` → `request_done` → `request_wait` → `read(16)` →
  `request_done`. Response: `[0]=cmd [1]=status (0 OK / 1 NG)`, payload from `[4]`, CRC over 12 at
  `[12..13]`. Commands: `F1` init(type), `F2` erase(addr), `F3` program(addr, ≤0x400), `F5` crc(addr,
  u32 n → `[4..5]`, must equal ccitt16_go8 of the file), `F9` reset, `FA` update type (`[4..7]`
  string, `[8]` change flag, `[10]`: `0x10` normal, `0x20` ALL, `0x15` SERVICE, else UNKNOWN).
- Byte order: the updater `rev`s each 32-bit word and the ECSPI shifts words MSB-first, so the wire
  carries the logical bytes in order — the MCU model needs no swapping in bootloader mode.

## Implementation (phase 1)
`qemu/xdj/xdj-panel.c` "SUB bootloader" block: the enter request is recognised at byte 100 of a
normal-mode burst → bootloader mode (normal frame building is bypassed); the handshake state machine
runs off GPIO1_IO18 edges (`xdj_subucom_bl_wire`, wired in `xdj-board.c`) and drives GPIO1_IO16.
Env: `XDJ_PANELCOM_IMG` (1 MiB flash file, saved on reset), `XDJ_UPDATE_TYPE` (hex, default `10`),
`XDJ_BL_LOG=1` (one stderr line per command). Test USB: a FAT32 MBR image with `XDJRX.UPD` at the
root (`hdiutil create -srcfolder … -fs MS-DOS -fsargs "-F 32" -layout MBRSPUD -format UDTO`).

**Verified 2026-11-15** (update 2.21, debug launcher, `XDJ_BOOT_UPDATE=1`, stick on USB1 = hub port
1.2): SUB bar "???? → 0.35" 100 %, `PANELCOM.mot : Success` (~0.2 s), 202 commands, 0 NG; the
flashed `0xC0000-0xEFFFF` equals the `.mot` byte for byte. Sequence: `FA`, `F1`, erase `0x00EFFE`
(4 KiB flag block) + program 4 bytes at `0xE000`, erase `0x0E/0D/0CFFFE` (64 KiB blocks), program
1 KiB pages, `F5` over `0xC0000`, erase the flag block, `F9`. A normal (0x10) update never writes
`.bot`/`.fix`. MAIN then fails at `/dev/mtd4` (no NAND) → "MAIN firmware update failed" /
"UPDATE: ERROR Linux Kernel Bank B." — phase 2.
The "current SUB version" `????` is the u-boot env `rev_subucom` (main `0xb680`; likewise `rev_kernel`, `rev_pioneer`, `release`), unset in the SD env — not the MCU.
**SUB version word** (frame bytes 2–3): PANELCOM is M16C/60 code (fixed vectors `0xFFFDC`, reset →
`0x12C5A` in `.bot`). The APL builds its SPI TX frame at RAM `0x1328`; the first word is four
byte-immediate stores (`b7 28 13` / `c7 35 29 13` / `b7 2a 13` / `b7 2b 13` at `0xC9CEB` = 0x00,
0x35, 0, 0) followed by `72 ff <src> 2c 13` copying the body. `xdj_bl_scan_version()` matches that
shape (unique in 2.21) in the flash → version 0x0035 → test mode "SUB Ver.0.35" (verified); no
flash / no match → 0. Disassembler: binutils 2.42 `--target=m32c-elf --with-system-zlib`,
`make MAKEINFO=true all-binutils`, `objdump -b binary -m m16c` (it shows some valid opcodes as
`*unknown*`, e.g. `e7` = `cmp.b:s #imm,abs16`).

## Implementation (phase 2-3): `qemu/xdj/xdj-nand.c`
APBH-DMA (CCW chains: PIO words → GPMI, CMD_READ/WRITE data phases, CHAIN/DEC_SEM/IRQ, SEMA kick;
SPI 13), GPMI (CTRL0 operations: CLE+ADDRESS_INCREMENT→ALE, raw data, wait-for-ready, ECC pages),
BCH (FLASH0LAYOUT bit-packed `[meta][data0][ecc0][data1]…`, status 0x00/0xff-erased, COMPLETE_IRQ;
SPI 15; parity written as zeros — the kernel never checks it, the boot ROM would), and a non-ONFI
Samsung K9F2G08U0C-style chip (ID `EC DA 10 95 44`, 2048+64, 64 pages/block, 2048 blocks). Backing
file `$XDJ_NAND_IMG` = raw pages with OOB (2112 B each, created erased); unset = no chip.
`XDJ_NAND_LOG=1` logs unhandled commands. The kernel gets `mtdparts=` from the firmware's own
`fw_env.txt` via `qemu_prepare.py` (real u-boot passes it from its env).
Kernel: `mxs-dma-apbh: initialized`, `Samsung NAND 256MiB`, `ecc_strength = 0x4` (t=8),
17 cmdline partitions. Verified in the guest: flash_erase/nandwrite/nanddump round trip,
ubiformat/ubiattach/ubimkvol/UBIFS persist across reattach.

**Verified 2026-11-15 — full update:** update mode + NAND + XDJRX.UPD 2.21 → "USER UPDATE ???? to
2.21": PANELCOM Success, mtd4 uImage (md5 read-back OK), mtd5 rootfs.cramfs, mtd7 pioneer, mtd9/mtd10
gui (ubiformat) all Success → **"Firmware update is complete."**, MAIN and SUB 100 %. (A user
update doesn't write mtd0 boot / kobs-ng.)

## Status
- [x] 0 references  - [x] 1 SUB  - [x] 2 NAND  - [x] 3 MAIN  - [ ] 4 NAND boot  - [ ] 5 provisioning/app
