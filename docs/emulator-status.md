# XDJ-RX emulator — feature status & findings

Status of the standalone-emulator effort (QEMU `-M sabrelite` patched to the
XDJ-RX's real SABRE-SD board). The goal: a packaged emulator that takes only the
**unmodified** `.UPD` firmware, used to develop device features (exFAT, FLAC, …).
Prefer QEMU-side changes over firmware edits; exFAT/FLAC are device features that
will modify the firmware (the emulator is the dev/test vehicle).

Legend: ✅ done · 🔶 partial/diagnosed · ⬜ investigated, not started · ❌ blocked

## ✅ Boot to the live UI (unmodified firmware)
Splash (32bpp) + main UI (16bpp RGB565) render; dynamic bpp scanout reads the IPU
display stride. Launch: `qemu/watch.sh`. Uses `nand.img` (the GAL
DirectFB gfxdriver removed so DirectFB software-renders). Committed.

## ✅ A. galcore / Vivante GPU — resolved by shipping nand.img
The built-in galcore (Vivante) driver probes at boot and fails `-22` (no real GPU),
which is harmless: `nand.img` has the one DirectFB GAL gfxdriver
(`libdirectfb_gal.so`) removed, so the UI software-renders without `/dev/galcore`
(the GPU is only used for an unreached `g2d_blit`). The baseline GPU register stub
(patch 02) just makes that `-22` clean rather than a hang.

**Why not a byte-for-byte rootfs?** Making galcore fully initialise is blocked by a
feature QEMU does not model. Deep RE (the `-22` was root-caused and cleared; boot then
hangs) showed the real blocker is **not galcore** but the **i.MX6 bus-frequency DVFS**
it triggers once the GPU clocks are enabled: `set_high_bus_freq` does an ARM-clock /
DDR frequency change with CPU IRQs masked, waits for a settling timer tick, and — even
once that tick is delivered — **retry-loops in the clk framework because QEMU models no
MMDC** (DDR controller, phys 0x021b0000), so the DDR frequency transition never
completes/verifies. Closing it would mean modelling the i.MX6 MMDC + DDR DVFS, a
substantial undertaking, so the experiment was removed (it lives in git history:
commits for the 2D-core identity + `imx_gpt_kick` + CCM CACRR trap) and we ship the
gfxdriver-removed image. The GPU does no work in the idle UI, so nothing is lost
visually. RE tooling used: QEMU `-gdb` + a raw-RSP python client, kernel VA→phys
page-table walk, and Ghidra (REA) on a sliced region ELF (the full 13 MB kernel times
out Ghidra's import).

## 🔶 B. Ethernet → rekordbox LAN link
FEC is modeled and `eth0` now **comes up on the LAN**. The firmware's
`/etc/rc.d/init.d/network` only configures eth0 when `rc.conf` sets
`SYSCFG_IFACE0=y`, and the apl_start boot path never runs it — so build-nand.sh
injects `ifconfig lo up; udhcpc -b -i eth0 … &` into apl_start (DHCP client,
backgrounded so it can't block the UI). `watch.sh` attaches a QEMU nic to the FEC.
The **default is user-mode NAT** so `./run.sh` opens the UI window as you with no
password (a sudo'd Cocoa window won't attach to the macOS session — that was what
hid the UI). Bridged LAN is opt-in with **`XDJNIC=bridged`** so the device is a
normal host on your LAN (user goal: a **rekordbox PC on the same LAN pulls/serves
library over the network**); on macOS vmnet needs root, so `run.sh` builds
unprivileged and elevates only that launch. **Verified:** eth0 gets a DHCP lease
(user-mode) and the UI boots to the live deck screen. Remaining: actually enabling the rekordbox
link is a **menu action in rbp** (needs panel buttons — feature C), and rbp's link
handler is `LnkHand` / the `(rekordbox)` code. The device is NOT a player-to-player
Pro-DJ-Link master here; the target is the rekordbox app library link.

## 🔶 D. 2× USB passthrough (= the original exFAT goal)
USB **is** modeled (ChipIdea, 4 controllers → `usb-bus.0..3`; firmware brings up
hosts `fsl-ehci.0` @0x02184000=`usb-bus.0` and `fsl-ehci.1` @0x02184200=`usb-bus.1`).
A FAT32 `usb-storage` attaches on the QEMU side (`info usb`: *Device 0.2, Port 1,
480 Mb/s, QEMU USB MSD*) and the **EHCI PORTSC correctly reflects it** — reading
`0x02184184` gives `0x00001005` = CCS=1 (connected) + PED=1 (enabled) + PP=1
(powered). So this is **NOT a QEMU model bug**; the controller sees the device and
enables the port, but the kernel then doesn't emit "new USB device" / read the
descriptor. Two leads:
1. **Real topology has a hub**: the firmware logs `register_usbhub_USB2512B` +
   `attach_usbhub` at late boot — a Microchip **USB2512B** 2-port hub sits between
   the i.MX6 host and the two front ports (USB1/USB2), likely configured over I2C
   (unmodeled). The firmware's USB flow may expect that hub; a drive belongs on a
   USB2512B downstream port, not the EHCI root. Options: model/attach a `usb-hub`,
   or (if the hub config is I2C) model the USB2512B on I2C.
2. Debug why enumeration doesn't proceed though the root port is enabled: **both
   cold- and clean hot-plug** (`device_add` after boot) leave PORTSC connected but
   produce **no** "new USB device" and **no** error — so khubd isn't running the
   descriptor read. Suspect the ChipIdea EHCI port-change interrupt / async-schedule
   isn't driving khubd (the controller starts and the root hub works, so it's a
   port-change-notify or control-transfer issue specific to this EHCI). Ruled out:
   the USB IRQ wiring is correct (OTG→GIC 43→irq 75, HOST1→GIC 40→irq 72, both
   matching the kernel's fsl-ehci.{0,1}), so it is not an interrupt-mapping bug.
**exFAT itself:** the 3.0.35 kernel has vfat/msdos/**ntfs** but **no exFAT and no
FUSE** — so the old "fuse-exfat" plan won't work; exFAT needs an out-of-tree kernel
module (e.g. exfat-nofuse) built against this kernel + a mount rule. (Test images:
`scratchpad/usbbuild/usbfat.img`.)

## ⬜ E. Audio
Not started. Need to model the i.MX6 audio path (SSI/codec) + `-audiodev`.

## ⬜ F. FLAC playback
rbp decodes **MP3 (libmpg123), AAC, M4A, AIFF, WAV — no FLAC** (two decoder slots
`PLAYER_DECODER_0/1`). The codecs are **dlopen'd** (e.g. `libmpg123.so.0` with
`mpg123_*` resolved by name, not a static PLT symbol), so the player has a
format→codec-lib factory. Adding FLAC = ship a FLAC codec lib + register it in that
factory's format map (follow-up: locate the dlopen/format-map factory in
`playengine_Player`). (Device feature; modifies firmware.)

## 🔶 C / G. Controls → app + rbp structure
Documented in `docs/rbp-and-panel-protocol.md`: control surface (buttons/jogs/
faders) comes via the Subucom panel MCU over SPI (`/dev/subucom_spi1.0`) →
`PanelComPeerLinux` → `PanelComReciever` → `panel_protocol::SubRxData`
(Common/Fmt/Other/Mixer + XOR). Next: decompile the concrete `SubRxFormat` subclass
for the exact RX frame byte layout (enables button injection in the emulator), and
the `StaticInfomation` boot handshake (blocks full stock boot after galcore).

## RE tooling notes
- Kernel: link base **0x80008000** (PAGE_OFFSET 0x80000000); wrap `vmlinux.bin` as
  an ELF at that base for objdump/Ghidra. Ghidra-via-REA times out on the 13MB
  kernel — use `objdump -d` + QEMU `-s -S` gdbstub instead.
- rbp: ARM ELF, C++ RTTI survives; REA/Ghidra works well (literal pools resolve).
