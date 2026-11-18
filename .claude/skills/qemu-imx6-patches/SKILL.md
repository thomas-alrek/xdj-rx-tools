---
name: qemu-imx6-patches
description: Understand and extend the XDJ-RX QEMU changes - the device models in qemu/xdj/ (GPU/IPU stubs and scanout, audio SDMA/SSI with the paced clock, the front-panel sub-ucom harness and LED back-channel, MFi auth chip, USBNC, GPIO straps) plus the small patches to stock QEMU files (i.MX6 peripherals, SABRE-SD board, ChipIdea USB, macOS timers). Use when modifying qemu-side emulation, adding a control-injection hook, debugging a boot/USB/audio gap, or building the patched qemu-system-arm.
---

# XDJ-RX QEMU: device models + patches

All device behavior lives **qemu-side**; the firmware (kernel + `rbp`) is loaded byte-for-byte
and **never patched**. Base: **QEMU 11.1.0** (checksum-pinned), machine **`-M sabrelite`**
(i.MX6Q, Cortex-A9), booted as the real board (**SABRE-SD, MACH_TYPE 3980,
`ATAG_REVISION=0x500`**) via a loader trampoline + hand-built ATAGs (`xdj prepare`, crate
`crates/xdj-fw`).

**macOS timer granularity (read before chasing latency):** without `05-macos-submillisecond-poll`
any QEMU main-loop timer below 1 ms fires ~1 ms late on macOS (no `ppoll`). That's why the audio
pacer runs on its own thread and why EHCI USB transfers crawled. Millisecond-quantized device
timing on a Mac → suspect this first.

## Layout
```
qemu/xdj/            real source files -- EDIT THESE; synced to <qemu>/hw/arm/xdj/ by build-qemu.sh
  xdj.h              shared interface (hooks, cross-module symbols)
  xdj-board.c        GPIO reset levels (key/fault pull-ups, update strap), bank captures,
                     source-key LED outputs, xdj_imx6_realize() dispatcher
  xdj-display.c      Vivante GPU 3D/2D/VG stubs, DVFS GPT kick, TEMPSENSE stub, IPU1 EOF
                     interrupt on the panel clock (CVT 800x480: 16.81 ms, panel_epoch),
                     IPU CPMEM capture, raster readout (panel_img filled in 16-row bands
                     at their readout times; XDJ_RASTER=0 off, XDJ_RASTER_BAND), tear meter
                     (`xdj-tears`, `xdj-tear-row`; XDJ_RASTER_TRACE=1 per-refresh band map),
                     frame-rate meter (`xdj-fps`: 120 Hz host-clock sampler of framebuffer
                     changes); the meters run only while read
  xdj-audio.c        SDMA engine, SSI1-3/AUDMUX register files, pacer thread + PCM FIFO
  xdj-panel.c        front-panel MCU ("sub-ucom") SPI slave: inject harness (holds, one-shots,
                     ctrl/key chardevs -- bound at realize, NOT lazily: boot-time holds must
                     be read before rbp's first frame -- or /tmp files; XDJ_FRAME_LOG=1), auto-level,
                     POWER key 0x5d.0 inverted (active-low: no hold = idle), LED back-channel, MAIN-UCOM
                     keys (/tmp/mainkey), USB fault-flag files, sub-ucom version word; the MCU's flash
                     bootloader for firmware updates (enter/erase/program/crc/reset over SPI +
                     GPIO1_IO18 REQ / GPIO1_IO16 ACK; docs/firmware-update-and-nand.md)
  xdj-mfi.c          I2C4 controller + MFi auth-coprocessor identity (fixes E-7206)
  xdj-usbnc.c        USBNC OWIR wakeup registers
  xdj-nand.c         APBH-DMA + GPMI + BCH + 256 MiB NAND chip ($XDJ_NAND_IMG; reads incl. u-boot's
                     cache reads 31h/3Fh); docs/firmware-update-and-nand.md
  xdj-bootrom.c      i.MX6 boot ROM for NAND boot (XDJ_BOOT_ROM=1: FCB -> u-boot plugin/IVT/DCD ->
                     u-boot) + the MMDC (DDR calibration completes); docs/firmware-update-and-nand.md §4
  meson.build
qemu/patches/        edits to stock QEMU files (diffs against pristine 11.1.0)
```
| patch | files | what |
|---|---|---|
| `01-imx6-soc-xdj-hooks` | `hw/arm/fsl-imx6.c`, `hw/arm/meson.build` | `#include "xdj/xdj.h"`; `xdj_imx6_gpio_pre/post_realize(s, i)` around each GPIO bank's realize; one `xdj_imx6_realize(s, dev, gic)` after the PCIe PHY stub; `subdir('xdj')` |
| `02-imx6-peripherals` | `imx6_src.c`, `imx_gpt.c/.h`, `imx6_ccm.c`, `imx_gpio.c/.h`, `imx_spi.c` | SRC GPU reset bits self-clear; SBMR1 boot straps from `xdj_src_sbmr1()` (weak; `XDJ_BOOT_MEDIA=nand`); `imx_gpt_kick()` + CCM clock-step → `xdj_dvfs_gpt_kick()`; GPIO `psr-reset` property; ECSPI burst start → `xdj_subucom_frame_start()` (weak) |
| `03-sabresd-board` | `hw/arm/sabrelite.c` | SD rootfs `xdjsd` on uSDHC3, console ttymxc0, `xdj-subucom` SSI slave on ECSPI2 |
| `04-usb-chipidea-hub` | `dev-hub.c`, `chipidea.c`, `hcd-ehci*.c/.h` | USB-2.0 hub descriptor + `PORT_STAT_HIGH_SPEED`; OTG VBUS-valid; ChipIdea (`tdi`): PORTSC[27:26] PSPD, USBSTS[7] SRI reads 1, microframe async re-poll while busy |
| `05-macos-submillisecond-poll` | `util/qemu-timer.c` | whole-ms `poll` + `nanosleep` the remainder (no `ppoll` on macOS) |

## Build
- `qemu/build-qemu.sh`: download + verify the tarball, extract `build/qemu-src/qemu-11.1.0`
  (`XDJ_QEMU_SRC` overrides the parent dir), apply `qemu/patches/*.patch` (recorded in
  `.xdj-patches`; a tree stamped with a different patch set is **re-extracted** automatically),
  **sync `qemu/xdj/*` → `hw/arm/xdj/`** (changed files only), configure
  (`--target-list=arm-softmmu --disable-sdl --disable-libssh …`; libssh is never used and would
  pull OpenSSL into the bundle), `ninja qemu-system-arm`.
- **Day-to-day:** edit `qemu/xdj/*.c` in the repo, then re-run `qemu/build-qemu.sh` (it syncs and
  runs ninja; ~30 s incremental). Editing `hw/arm/xdj/` in the build tree directly will be
  **overwritten** on the next sync.

## Changing a stock QEMU file (a patch)
Edit the file in the build tree, then **regenerate that patch from pristine** — never hand-edit
hunks (`/usr/bin/patch` here is Apple 2.0 and strict: a wrong `@@` count is a hard error that breaks
every later patch). Each patch owns a fixed set of files (table above), so a patch is simply
`diff -u <pristine>/<file> <tree>/<file>` over its files, with `a/`/`b/` labels, after the header
prose. Pristine: `tar xf build/qemu-src/qemu-11.1.0.tar.xz -C /tmp/p qemu-11.1.0/<dirs>`.
New file group → new patch (keep one file in one patch).
**Verify before committing:** fresh tarball + all patches + `qemu/xdj` sync == the live tree
(`diff -rq` over `hw include util`), and a from-scratch build:
`XDJ_QEMU_SRC=/tmp/fresh qemu/build-qemu.sh` (copy the tarball there first).

## Extending it (adding a control hook)
- A **sub-ucom** control (fader/button/knob/rotary) needs no code — inject it through the harness
  (`/tmp/subucom_ctrl` or the `xdjctrl` chardev; map-subucom-* skills).
- A **GPIO** control: reset level in `xdj_imx6_gpio_pre_realize` (`psr-reset`), runtime drive via
  `g_xdj_gpio[bank]` + `qemu_set_irq` (see the `/tmp/mainkey` hook in `xdj-panel.c`), rbp
  *outputs* via `qdev_connect_gpio_out` in `xdj_imx6_gpio_post_realize` (as the source-key LEDs).
  **A pin whose GDIR is output can't be driven as an input** — check `GDIR` first.
- A new SoC block: add a module `qemu/xdj/xdj-<name>.c`, list it in `qemu/xdj/meson.build`, call
  its realize from `xdj_imx6_realize()`, map MMIO with `*_overlap` priority ≥ 1 if it sits over a
  catch-all unimplemented region.

## Gotchas
- Don't drive IPU **alpha/FG** channel (bit 27) — panics the kernel; only BG ch23.
- Keep the **firmware untouched** — if a fix seems to need an rbp/kernel edit, find the qemu-side
  cause first (custom firmware is tested by booting it).
- Two emulators at once contend for the host and can stall a boot — don't benchmark side by side.
- **Boot ROM**: QEMU's sabrelite ignores unmapped accesses (reads 0), so missing hardware shows
  up as code waiting forever for a status bit, not as an abort. The u-boot plugin's DDR
  calibration was the case: the MMDC model in `xdj-bootrom.c` completes it. A device u-boot uses
  but the kernel doesn't (e.g. NAND cache reads) needs the same care.

## References
`qemu/build-qemu.sh`, `qemu/watch.sh`, `qemu/build-nand.sh`, `crates/xdj-fw` (`xdj prepare`),
`docs/emulator-status.md`, `docs/firmware-update-and-nand.md`, `docs/service-modes.md`,
`docs/subucom-led-map.md`, `docs/usb.md`. Skills: [run-xdj-emulator], [rbp-binary-internals], [map-gpio-control],
[map-subucom-button].
