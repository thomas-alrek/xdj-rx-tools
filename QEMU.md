# Booting the XDJ-RX firmware in QEMU

**Status: the firmware boots fast to its real on-screen UI in QEMU.** The kernel
runs through full device init, hands off to the firmware's own embedded-initramfs
`/init`, which boots the real rootfs from an **SD card** (the firmware's own
SD-boot mode); the app (`rbp`) then renders the genuine **Pioneer DJ / rekordbox
boot splash** in 800×480 landscape, smoothly and without tearing. The firmware
image is loaded byte-for-byte; everything that makes it work lives on the QEMU
side (a few small i.MX6 patches) and in an SD image assembled from the firmware's
own payloads. The app next blocks on the front-panel microcontroller handshake —
emulating that to reach the interactive main screen is the next step (see "Next
steps").

## TL;DR

```bash
# 1. Build the patched QEMU (once; ~15 min). Two i.MX6 patches, firmware untouched.
qemu/build-qemu.sh

# 2. Decrypt the firmware and prepare the kernel boot blobs.
python3 xdjrx_upd.py decrypt XDJRX.UPD -o build/XDJRX_2.21.iso
python3 qemu_prepare.py

# 3. Build the SD rootfs image (needs Docker), then boot.
qemu/build-sd.sh
qemu/watch.sh           # live 800x480 window + serial console (the UI)
./qemu_run.sh           # headless/serial only (login: root, no password)
```

Reference logs under `build/qemu/`: `boot-2.21.log` (device init),
`boot-userspace-2.21.log` (kernel → `/init`, no SD), `boot-shell-2.21.log`
(full SD boot to the shell).

## Target & tools

- QEMU machine: **`-M sabrelite`** (Freescale i.MX6 Quad SABRE Lite, Cortex-A9) —
  matches the SoC; the firmware kernel has `CONFIG_MACH_MX6Q_SABRELITE=y`.
- Kernel: `uImage` → a **zImage** (self-decompressing) wrapping `Linux 3.0.35`.
- QEMU 8+; `qemu-system-arm` with the `loader` device.

## The problem and the fix (why the trampoline exists)

Booting the stock `uImage` with `-M sabrelite -kernel` gives a **silent hang**.
Diagnosis (via `-d in_asm`, the QEMU monitor `info registers`, and symbolizing
the kernel with `vmlinux-to-elf`):

1. The kernel loads, runs `head.S`, enables the MMU, enters `start_kernel`.
2. It hangs at **`dump_machine_table+0x38`** — an infinite loop entered when
   `__lookup_machine_type` fails. The CPU type matched (`__v7_ca9mp_proc_info`),
   but the **machine ID did not**.
3. QEMU's `sabrelite` is a *device-tree* machine and passes board-id **-1**;
   this Freescale 3.0.35 kernel is **ATAG-based** and rejects it. (`earlyprintk`
   is compiled out, so the failure prints nothing — hence "silent".)
4. The kernel's machine table *does* contain id **3769** (`0x0EB9`,
   "Freescale i.MX 6Quad Sabre-Lite Board").

**Fix:** boot the already-decompressed kernel through a tiny trampoline that
sets the registers the kernel expects, with hand-built ATAGs — no firmware edit:

```
r0 = 0
r1 = 3769            ; MACH_TYPE for Sabre-Lite  (the whole fix)
r2 = 0x10000100      ; -> ATAG list (CORE + MEM 1GB@0x10000000 + CMDLINE + NONE)
pc = 0x10008000      ; kernel entry
```

Everything is placed with `-device loader` (see `qemu_run.sh`). Using the
decompressed `vmlinux.bin` (extracted from the zImage's gzip payload) avoids any
dependence on how the decompressor forwards registers.

## Console

- `earlyprintk`/`DEBUG_LL` is **off** in the firmware kernel, so nothing prints
  until a console registers. We read early progress two ways while debugging:
  the QEMU monitor (`info registers`, `pmemsave`) and `vmlinux-to-elf` symbols.
- The board's early console is the **polled MXC console on UART2 (`0x21e8000`)**,
  which QEMU exposes as serial index **1**. The full `imx-uart` driver's TX does
  **not** reach QEMU's chardev (QEMU logs `imx_serial_write: Unimplemented reg
  0xb4` = the UART `UTS` register), so once the driver console takes over, output
  is lost.
- Workaround (no firmware change): cmdline **`console=ttymxc1,115200
  earlyprintk keep_bootcon`** — keep the working polled console alive. In
  `qemu_run.sh` serial index 1 is wired to your terminal (`-serial null -serial
  mon:stdio`).

## The GPU patches (why the firmware now boots to userspace)

With stock QEMU the kernel reaches device init and then **hangs at the Vivante
GPU driver (`galcore`)**, because QEMU models neither the GPU nor its SRC reset.
I traced the two distinct hang points (QEMU monitor `info registers`, a GPU
access trace, and the galcore source) and fixed them in the emulator:

1. **SRC GPU reset never clears** (`patches/01-imx6_src-gpu-reset.patch`).
   `gckOS_ResetGPU()` sets SW_GPU3D_RST (bit 1) / SW_GPU2D_RST (bit 4) in
   `SRC_SCR` and polls until they self-clear. QEMU's `imx6_src` cleared the CPU
   and IPU reset bits but not the GPU ones → infinite spin. The patch makes them
   self-clearing (QEMU models no GPU, so "reset done" immediately is correct).

2. **GPU registers unmapped** (`patches/02-fsl-imx6-gpu-stub.patch`). After the
   SRC reset, galcore's `_ResetGPU()` polls the GPU: it waits for the idle
   register (`0x004`) and for **bits 16 & 17 of the clock-control register
   (`0x000`)** to read back set. QEMU maps nothing there (reads returned 0) → it
   looped forever. The patch adds a minimal register stub at the 3D/2D GPU bases
   that reports idle, echoes the clock-control status bits, reports a GC2000, and
   pulses the GPU IRQ on command/interrupt writes.

With both, galcore's reset completes, its probe then **fails gracefully**
(`galcore: probe of galcore.0 failed with error -22` — there is no real GPU, as
intended) and the kernel boots on. The GPU is not needed for the USB/exFAT work.

Build the patched QEMU with `qemu/build-qemu.sh`.

## Where it ends (full boot — `build/qemu/boot-userspace-2.21.log`)

```
Machine: Freescale i.MX 6Quad Sabre-Lite Board   ·  Memory: 768MB
L310 cache · GPMI NAND · FEC Ethernet · USB + usb-storage · SDHCI/MMC · VPU
Galcore version 4.6.9.6622
galcore: probe of galcore.0 failed with error -22        ← graceful, no real GPU
Anatop Thermal · usbhid · snd-usb-audio · VFP support v0.3
snvs_rtc: setting system clock to 2026-11-04 ...
Freeing init memory: 3844K                               ← kernel done, runs /init
Kernel panic - not syncing: Attempted to kill init!      ← /init exited (no rootfs)
```

The kernel runs the firmware's **embedded-initramfs `/init`** (the kernel is
built with `CONFIG_INITRAMFS_SOURCE`). That `/init` sources `mount_usb.sh` /
`decrypt_update.sh` and tries to mount the NAND UBIFS rootfs; with no NAND
content in QEMU it exits, which the kernel reports as the panic. This is the real
device userland — **including the USB-mount code the exFAT change targets** —
now running under emulation.

Non-fatal emulation gaps (harmless): `mxs-dma` apbh reset timeout, one
`mxc_sdc_fb` probe conflict, `imx-sdma` CH0 timeout (recovers).

## Boot to a shell via SD (`qemu/build-sd.sh`)

Without a rootfs, `/init` exits (above). The firmware's `/init` supports three
rootfs modes (`nand`/`sd`/`nfs`) chosen from the u-boot env. QEMU models no NAND
but **does** model the uSDHC/SD controller, so we use the firmware's own **SD**
mode. `qemu/build-sd.sh` assembles an SD image from the firmware's own payloads:

- **Env** blob at byte `0xC0000` (`rootfs=sd`, `mmcdev=/dev/mmcblk0p1`). The
  SD/NAND DIP switch defaults to SD in QEMU, so `fw_printenv` reads this env.
- **MBR partition 1 = ext4** holding the rootfs: `rootfs.cramfs` (extracted with
  `qemu/cramfs_extract.py`) + `pioneer.tar.gz` (the app). Built with **only ext4
  features kernel 3.0.35 can mount** (no `metadata_csum`/`64bit`/`orphan_file`).
  The root password is cleared so the firmware's getty logs straight in.

Boot (`qemu_run.sh` attaches `build/qemu/sd.img` to `sdhc4` = `mmc0`):

```
### rootfs = sd ###                          ← fw_printenv read the SD env
starting pid … '/etc/rc.d/rcS'               ← real init / rc scripts
#### apl_start script ###  #### Application Start!! ###
BusyBox v1.20.2 built-in shell (ash)   freescale login:  → root → shell
```

Notes / limits:
- The SD path copies the rootfs into a **fixed 48 MB tmpfs** (hard-coded in
  `/init`), so the image omits `gui.tar.gz` (base+app = 40 MB fits). GUI assets
  are delivered separately on the device; see Next steps.
- `/dev/fb0` exists (major 29). Non-fatal: the USB-gadget module and some
  peripheral pokes fail (no hardware).

## Display — framebuffer scanout works

QEMU models no i.MX6 IPU, but the `mxc_sdc_fb` driver still allocates and
configures the framebuffer (**480×800 ARGB8888 at physical `0x18000000`**) and
userspace draws into it. A small **scanout device** added to the i.MX6 machine
(`qemu/patches/02-…`) registers a graphic console that mirrors that memory, so
the display is capturable and showable:

```bash
# activate the LCD framebuffer via the kernel cmdline (in atags), then:
./qemu_run.sh -display cocoa          # live 480×800 window (or gtk/sdl)
# or headless capture via the monitor:
#   (qemu) screendump out.ppm          -> 480×800 image of /dev/fb0
```

Verified end-to-end: writing a test image to `/dev/fb0` in the guest appears
pixel-exact in `screendump`. To activate fb0, the ATAGs cmdline needs
`video=mxcfb0:dev=lcd,800x480M@60,if=RGB24,bpp=32` — **800×480 landscape**, the
physical panel geometry. (`rbp` adapts its UI to whatever fb size it is given, so
the wrong geometry just renders the UI rotated.)

## The IPU vsync-interrupt stub (fast boot, smooth UI)

The real win for the display is in `patches/02`: a small **IPU1 interrupt stub**
that makes the `mxc_sdc_fb` display path behave as if a real 60 Hz panel were
attached.

Without it, QEMU raises none of the IPU interrupts the fb driver waits on, so
every display-enable and every DirectFB page-flip (`rbp` flips with
`DSFLIP_WAITFORSYNC`) falls back to a kernel **timeout** — boot stalled ~80 s and
the splash crawled/teared. The stub models just the IPU Control-Module interrupt
registers (`INT_CTRL`/`INT_STAT`, CM block at IPU1 `+0x200000`) and delivers a
one-shot IRQ ~1/60 s after the driver arms one, exactly like real vsync.

Two details that matter (both learned the hard way, by disassembling the firmware
kernel with `build/qemu/vmlinux.elf`):

- **Register layout** was recovered from the kernel's own `ipu_clear_irq` /
  `ipu_enable_irq`: `INT_CTRL_n` at `0x3C+4(n-1)`, `INT_STAT_n` at `0x200+4(n-1)`,
  write-1-to-clear; irq *k* → register `(k>>5)+1`, bit `k&31`.
- **Only the BG display channel (IDMAC ch 23)** is driven — its EOF (`INT_STAT_1`
  bit 23) and NFACK (`INT_STAT_3` bit 23), which the enable and vsync paths wait
  on. Driving the FG/**alpha** channel (bit 27) instead **panics the kernel**:
  `mxcfb_alpha_irq_handler` runs `complete()` on a completion that isn't
  initialized until later in `mxcfb_probe`. A `IPU_TRACE=1` env var logs the
  `INT_CTRL` writes the driver makes, which is how the exact bits were found.

Result: kernel → userspace in ~8 s, and the splash renders smoothly with no
tearing.

## GUI assets (done)

The app reads its fonts and imagedata from `/root/gui` (`pset/fontdata`,
`pset/imagedata/imagedata.dat`, `pioneer/fontdata`). On the device that's a UBIFS
volume on NAND; QEMU has no NAND, so `qemu/build-sd.sh` delivers `gui.tar.gz` on a
**second ext4 partition** and mounts it at `/root/gui`. The mount is injected into
`apl_start` unconditionally, because the firmware gates its own `mount_ubifs gui`
on `$BOOTMODE`, which comes from the i.MX6 boot-mode straps that QEMU doesn't
emulate. With the assets present, `rbp` renders the splash.

## Next step — the front-panel microcontroller (Subucom)

After the splash, `rbp` blocks handshaking with the panel MCU over
`/dev/subucom_spi1.0` (SPI): it retries `Subucom StaticInfomation is not
completed` forever. The MCU reports the hardware's identity/config and drives the
jog wheels, faders and buttons. Reaching the interactive main screen means
emulating it — a QEMU ECSPI slave that answers enough of the panel protocol
(`ui::PanelComController` / `panel_protocol`) for the handshake to complete.

(After the UI is interactive: mods such as exFAT — the
`12-usb-memory-auto-mount.rules` USB path — can be tested on this same bed.)

## Files

Tracked: `qemu_prepare.py` (kernel boot blobs), `qemu_run.sh` (boot, attaches SD
if present), `qemu/build-qemu.sh` + `qemu/patches/*.patch` (the patched QEMU),
`qemu/build-sd.sh` + `qemu/cramfs_extract.py` (the SD rootfs). Generated under
`build/qemu/` (git-ignored): `uImage`, `vmlinux.bin`, `tramp.bin`, `atags.bin`,
`sd.img`, the `boot-*.log` references; patched QEMU under `build/qemu-src/`.
