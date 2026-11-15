# XDJ-RX emulator — feature status (1.0)

The emulator runs the **unmodified** XDJ-RX firmware on a patched QEMU (`-M sabrelite`, wired as the
XDJ-RX's real board, a Freescale i.MX6Q SABRE-SD). Everything that makes the firmware happy is done
QEMU-side: device models in `qemu/xdj/*.c` plus five small patches to stock QEMU files in
`qemu/patches/` (skill `qemu-imx6-patches`). `rbp`, the kernel and the root filesystem are never
patched in the emulator workflow, so custom firmware can be tested by installing and booting it.

Legend: ✅ works · 🔶 partial · ⬜ not started

| area | status |
|---|---|
| [Boot, display, GPU](#boot-display-gpu) | ✅ |
| [Panel controls and LEDs](#panel-controls-and-leds) | ✅ |
| [Audio](#audio) | ✅ |
| [USB](#usb) | ✅ |
| [Network](#network) | 🔶 |
| [Firmware update, NAND unit, desktop app](#firmware-update-nand-unit-desktop-app) | ✅ |
| [Custom firmware](#custom-firmware) | ✅ |
| [Service modes and auth chip](#service-modes-and-auth-chip) | ✅ |
| [Device features (exFAT, FLAC)](#device-features-exfat-flac) | ⬜ |
| [Known limitations and open items](#known-limitations-and-open-items) | — |

## Boot, display, GPU
✅ Boots to the live rekordbox deck UI, dual-core (`-smp 2`, the real configuration) by default.
- **Display** (`xdj-display.c`): the splash renders at 32 bpp, the deck UI at 16 bpp RGB565; the
  scanout reads the live IPU display-channel stride and decodes whichever depth is active.
- **Vivante GPU** (`xdj-display.c`): the stock `galcore` driver fully initialises (`/dev/galcore`
  exists) and the deck UI renders through it via DirectFB's GAL driver. The stub provides:
  - the GC2000 3D core, a GC320 2D identity (so galcore derives `gcvHARDWARE_2D`) and the GC355
    OpenVG core at `0x02204000` (core index 2) with VG chipFeatures bit 28 set and its own IRQ
    (`V2D_IRQ`, GIC 43);
  - command completion: each submitted command's EVENT id (`LOAD_STATE 0x0E01`) is parsed from the
    command buffer and exactly that event is raised (`xdj_gpu_cmd_event`); AXI-error/MMU bits
    (31/30) of `AQIntrAcknowledge` are never reported;
  - the DVFS settling tick: CCM clock-step writes (CCSR/CACRR/CGPR) raise the GPT OF1 interrupt
    (GIC 87, `imx_gpt_kick()`), which ends the kernel's `set_high_bus_freq` `GICC_HPPIR` poll. Side
    effect: the tick also wakes the idle WFI, so the idle loop re-runs its own bus-freq step; this
    costs host CPU but does not block anything.
- **USB OTG**: the controller's VBUS/session-valid change is modelled (`04-usb-chipidea-hub.patch`),
  so boot doesn't stall at `wait otg vbus change timeout!`.
- **GPIO straps** (`xdj-board.c`): the USB fault flags GPIO4_IO30 / GPIO7_IO12 reset HIGH as on
  hardware (reading 0 deadlocks rbp's panel init); `SRC_SBMR1` boot straps follow `XDJ_BOOT_MEDIA`.
- Knobs: `XDJ_GPU2D_OFF=1` reports a GC2000 in the 2D slot so galcore fails `-22` and DirectFB
  software-renders (regression comparison only); `XDJSMP=1` boots single-core (the init script then
  launches `rbp_solo`, which only the SD test bed provides). Traces: `IMX6_GPU_TRACE`, `IPU_TRACE`,
  `XDJ_FB_TRACE`.

## Panel controls and LEDs
✅ The front panel ("sub-ucom" MCU) is emulated in `xdj-panel.c`: rbp reads every control and
drives every LED and meter.
- **Input** (MISO `SubRxData`): one 100-byte frame on `/dev/subucom_spi1.0` per ECSPI burst
  (`xdj_subucom_frame_start`, hooked from `02-imx6-peripherals.patch`). CRC-16/X-25 (poly 0x8408,
  init/xorout 0xFFFF) over the first 96 bytes with each 32-bit word byte-reversed, LE at `[0x62]`;
  the ECSPI shifts each word MSB-first, so rbp's byte `j` is frame byte `(j&~3)+(3-(j&3))`. Frame
  bytes 2–3 carry the MCU firmware version (see [service modes](service-modes.md)).
- **Control map**: every button, fader, knob, the jog wheels and the browse encoder are
  runtime-verified —
  [subucom-frame-map.md](subucom-frame-map.md); protocol and decoder internals:
  [rbp-and-panel-protocol.md](rbp-and-panel-protocol.md). The POWER key (`0x5d.0`) is active-low and
  inverted by the harness (a hold means "pressed").
- **Output** (MOSI `SubTxData`): 20-byte payload, CRC-16/X-25 at `[0x16]`, deck 2 = deck 1 + 4
  bytes. Published on every change as `T <hex>` up the `xdjctrl` chardev (or `/tmp/subucom_tx` in
  file mode); the panel streams it at `GET /leds`. Lit: PLAY, CUE, LOOP IN/OUT, RELOOP, REV, SLIP,
  SYNC, MASTER, M.T, VINYL, pad modes and pads, CUE1/2, the four COLOR FX, BEAT FX ON, MASTER REC,
  the CH1/CH2/MASTER meters and the jog rings — [subucom-led-map.md](subucom-led-map.md).
- **Source-key LEDs** (rekordbox/MIDI/USB1/USB2) are rbp GPIO outputs 162–165, mirrored by
  `xdj-board.c` onto the LED line as byte `0x14`.
- **MASTER REC** records to USB2, with its blinking LED.
- **Transport**: per-instance QEMU chardevs `id=xdjctrl` (sub-ucom snapshot) and `id=xdjkey`
  (main-ucom key GPIO). The client writes the whole control state as one NUL-terminated record
  (empty = release). Launch with `XDJ_RUNTIME_DIR=<dir>` (sockets `<dir>/ctrl.sock`, `key.sock`).
  Without those chardevs the harness reads `/tmp/subucom_ctrl` / `/tmp/mainkey` (override:
  `XDJ_CTRL_PATH` / `XDJ_KEY_PATH`), which the debug tools (`inj.py`, `sweep_*.py`) use.
- **Auto-level**: once rbp has been polling for a settle delay, the harness merges an audible
  default into the frame (both channels → DECK, faders/MASTER/TRIM up, EQ and COLOR centred), per
  control, so a hold for any offset still wins. `XDJ_AUTOLEVEL=0` disables it,
  `XDJ_AUTOLEVEL_DELAY_MS` sets the settle (default 4000).
- Debug knobs: `SUBUCOM_TRACE=1` (SPI frames), `XDJ_FRAME_LOG=1` (first 12 frames), `XDJ_TX_LOG`,
  `XDJ_TX_PATH`.

## Audio
✅ Real deck audio reaches the host (`xdj-audio.c`): a minimal SDMA engine, RAM-backed SSI1/2/3
and AUDMUX let imx-sdma/ASoC/ALSA run with the playhead in real time; a host sonifier plays SDMA
**ch1 = MasterOut**.
- Audible needs routing = DECK (`rbp[0x33].7` / `rbp[0x43].7`) and gains up (rbp's on-air gate
  `FUN_00065028`); auto-level provides both.
- **Pacing**: rbp's ALSA ring is 2 × 64 frames, so periods must complete evenly. A pacer thread
  completes one period per 1.45 ms, rate-locked to the host through a PCM FIFO (two decks + BEAT
  FX + COLOR FX + tempo: ~2 guest xruns in 30 s). Launch scripts set the host backend's
  `timer-period=1451`.
- Knobs: `XDJAUDIO` (host backend, `none` = silent), `XDJ_AUDIO_LATENCY_MS` (FIFO target, default
  20 ms), `XDJ_AUDIO_OUT_CH` (SDMA channel, default 1), `XDJ_AUDIO_NOSOUND`, `XDJ_AUDIO_PERIOD_HZ`,
  `XDJ_AUDIO_BYTE_RATE`.
- Verify: `XDJ_AUDIO_STATS=1` (pacer stats), `XDJ_AUDIO_DEBUG=1` (per-channel SDMA peaks); xruns
  show as `ALSA: Resolve xrun` on the guest console.

## USB
✅ Both front slots work like hardware: boot with the USB 2.0 hub only, insert the stick after rbp
is polling (`device_add`, or LOAD in the panel) → it mounts at `/media/usb1|usb2/sda1` and rbp
loads the rekordbox library. Recipe and troubleshooting: [usb.md](usb.md).
- `xdj-usbnc.c` wakes the suspended HOST1 on attach; `04-usb-chipidea-hub.patch` provides the
  high-speed hub and ChipIdea port speed, so the slots enumerate at **480 Mbit/s**;
  `05-macos-submillisecond-poll.patch` keeps sub-ms QEMU timers on time on macOS. The firmware caps
  requests at 8 KB, so per-request latency dominates: a 60 MB WAV loads in ~7.4 s.
- GPIO4_IO30 / GPIO7_IO12 (gpiodrv `0x7e` / `0xcc`) are **active-low USB fault flags**, not
  insert-detect. `touch /tmp/usb_insert` / `/tmp/usb2_insert` deliberately injects a fault ("USB
  Error. Remove the device.").
- A stick attached at boot (cold-plug) races rbp's mount rendezvous and shows "USB Error"; insert
  after boot instead. Don't `device_del` to retry by hand (it tears down the drive backend); the
  panel's eject handles this, otherwise reboot.

## Network
🔶 The i.MX6 FEC is modelled and `eth0` works; rekordbox LINK over the LAN is untested.
- Backend: user-mode NAT by default; `XDJNIC=bridged` (or a literal QEMU nic spec) puts the device
  on the LAN (`qemu/watch.sh`; macOS vmnet needs root, `run.sh` elevates only that launch).
- `eth0` gets a DHCP lease on both boots: on a NAND unit rbp starts `udhcpc` itself once it is
  up (console: `udhcpc … Lease of 10.0.2.15 obtained`); the SD test bed (`qemu/build-nand.py`) also
  adds a backgrounded `udhcpc` to apl_start.
- Open: enable rekordbox LINK from rbp's menu and check the library link with a rekordbox PC on the
  LAN (rbp's link handler: `LnkHand`). The target is the rekordbox library link, not Pro DJ Link.

## Firmware update, NAND unit, desktop app
✅ The full firmware update (SUB + MAIN) runs Pioneer's own updater to completion, and a unit boots
from its emulated NAND — [firmware-update-and-nand.md](firmware-update-and-nand.md).
- `xdj-panel.c` emulates the panel MCU's flash bootloader (1 MiB flash, persisted to
  `XDJ_PANELCOM_IMG`; update type from `XDJ_UPDATE_TYPE`, hex: `10` normal, `20` ALL, `15`
  SERVICE; `XDJ_BL_LOG=1` traces it). `xdj-nand.c` emulates APBH-DMA/GPMI/BCH and a 256 MiB NAND
  (`XDJ_NAND_IMG`, `XDJ_NAND_LOG`).
- **Provision + boot** with the `xdj` CLI: `xdj nand init` (blank NAND with the firmware's default
  env) → an ALL update from USB → `xdj nand boot` (u-boot's `bootcmd_nand`: reads `kernel_bank`,
  builds the boot blobs) → launch with `XDJ_BOOT_MEDIA=nand XDJ_SD=none XDJ_SMP=2`.
- **Desktop app** ([standalone-app.md](standalone-app.md)): a dropped firmware provisions a blank
  NAND unit with its ALL update (stick inserted automatically), then boots from NAND. "Installed" =
  `release=` in the NAND env. The app starts powered off; the panel's POWER switch / File menu
  powers QEMU on and off (the [standby handshake](service-modes.md#power-key-standby-handshake)
  when rbp is up). USB images loaded while off are inserted after boot. File menu: *Power On in Test
  Mode*, *Install Firmware Update…*, *Enter Firmware Update Mode*. The window shows the deck only,
  aspect-locked. Prep, NAND handling and the panel server are Rust in `xdj-host` (no Python).
- **MCP server** (`POST :8765/mcp`, [standalone-app.md §7a](standalone-app.md#7a-mcp-server-automation)):
  agents and scripts drive the running app — power, `load_firmware`, every control by name, USB,
  PNG screenshots, LEDs, console.
- **UTILITY → VERSION No.** shows the installed release, e.g. `Ver 2.21` —
  [version-display-and-override.md](version-display-and-override.md).

## Custom firmware
✅ `xdj fw build` (base image + overlay + `--release`) produces a `.UPD` that Pioneer's updater
installs on the emulated unit, which then boots it (e.g. 2.21 → 2.30, test mode shows SYSTEM
Ver.2.30) — [custom-firmware.md](custom-firmware.md) (skill `custom-firmware`). Not yet covered:
kernel/initramfs rebuilding, u-boot and panel-MCU images (carried over untouched), downgrades.

## Service modes and auth chip
✅ Test mode (all 8 sub-modes), factory reset key combo, firmware update mode and the POWER standby
handshake — [service-modes.md](service-modes.md).
- **Auth chip** (`xdj-mfi.c`): I2C4 (`0x021F8000`, `/dev/i2c-3`) with the Apple MFi coprocessor's
  identity registers at slave 0x10, so there is no `E-7206 AUTH CHIP ERROR`.
- **Update mode**: `XDJ_BOOT_UPDATE=1` raises GPIO3_IO30 (`xdj-board.c`) → the initramfs' D001
  "Connect USB storage device" screen.

## Device features (exFAT, FLAC)
⬜ These change the firmware and are built as [custom firmware](custom-firmware.md).
- **exFAT**: the 3.0.35 kernel has vfat/msdos/ntfs but **no exFAT and no FUSE**, so it needs an
  out-of-tree kernel module (e.g. exfat-nofuse) built for this kernel plus a mount rule.
- **FLAC**: rbp decodes MP3 (libmpg123), AAC, M4A, AIFF and WAV in two decoder slots
  (`PLAYER_DECODER_0/1`); codec libraries are `dlopen`ed and their symbols resolved by name. Adding
  FLAC = a FLAC codec library registered in the player's format→codec factory (in
  `playengine_Player`, not yet located).

## Known limitations and open items
- **Test mode flake**: about 1 boot in 8 comes up in the normal UI although both keys are in every
  frame from the first — rbp-side timing ([service-modes.md](service-modes.md#test-mode)).
- **NAND unit root shell**: the serial getty needs the stock root password (not recovered); use the
  SD test bed (same `rbp`/kernel) for shell-level debugging.
- **u-boot is not executed**: boot blobs are built host-side (`xdj prepare`, `xdj nand boot`); the
  boot ROM / u-boot path would need real BCH parity.
- **Single-core** needs `rbp_solo`, which a stock install lacks (SD test bed only).
- **Rear LINE/PHONO switch** (GPIO 119/120) and external audio inputs are not emulated.
- **LED map**: a few status bits are unnamed and the jog ring's non-position values are undecoded
  ([subucom-led-map.md](subucom-led-map.md#open-items)).
- **rekordbox LINK** over the network is untested (see [Network](#network)).
- Regression coverage of all of the above: [testing.md](testing.md).

## RE notes
- Kernel: link base **0x80008000** (PAGE_OFFSET 0x80000000); wrap `vmlinux.bin` as an ELF at that
  base for objdump. Ghidra times out importing the 13 MB kernel — slice the region of interest
  into its own ELF, or use `objdump -d` + the QEMU gdbstub (`-s -S`). gdb hardware breakpoints work.
- rbp: ARM32 ELF with C++ RTTI; REA/Ghidra handles it well — skill `rbp-binary-internals`.
