# XDJ-RX emulator — feature status & findings

Status of the standalone-emulator effort (QEMU `-M sabrelite` patched to the
XDJ-RX's real SABRE-SD board). The goal: a packaged emulator that takes only the
**unmodified** `.UPD` firmware, used to develop device features (exFAT, FLAC, …).
Prefer QEMU-side changes over firmware edits; exFAT/FLAC are device features that
will modify the firmware (the emulator is the dev/test vehicle).

Legend: ✅ done · 🔶 partial/diagnosed · ⬜ investigated, not started · ❌ blocked

## ✅ Boot to the live UI (unmodified firmware)
Splash (32bpp) + main UI (16bpp RGB565) render; dynamic bpp scanout reads the IPU
display stride. Launch: `qemu/watch-sabresd.sh`. Uses `sd-nogal.img` (the GAL
DirectFB gfxdriver removed so DirectFB software-renders). Committed.

## 🔶 A. galcore / Vivante stub → fully stock boot
Root-caused the `galcore probe … -22`: our 2D GPU stub reported a 3D chip (GC2000),
but galcore requires the 2D register slot to be type `gcvHARDWARE_2D`. **Fix found**
(per-core identity: 3D=GC2000, 2D=GC320) clears the -22 — saved at
`scratchpad/fsl-imx6-with-2d-identity.c`, reverted from the build because galcore
(a built-in initcall) then proceeds into 2D init and **hangs the kernel boot** on:
(1) a `__mxs_reset_block` reset-poll timeout (slow, bounded) and (2) an infinite
WFI completion-wait needing the 2D core's IRQ+completion faking. Finishing = a
minimal 2D-core model (identity + IRQ + completion + reset-done). Full notes in
memory `galcore-minus22-solved`. Until then, `-22` fast-fails harmlessly and the UI
path works via sd-nogal.

## 🔶 B. Ethernet → Pro DJ Link
FEC is modeled; `-nic user,id=net0` attaches and the FEC driver loads
("fec_enet_mii_bus: probed"). The firmware runs a **DHCP server** (`udhcpd`, pool
**192.168.0.100–120** on `eth0`) — the classic Pro-DJ-Link setup where the player
hands out IPs on its link subnet. But `eth0` is **not** brought up at boot: the
`/etc/rc.d/init.d/network` script only configures it when `SYSCFG_IFACE0=y`
/ `IPADDR0` is set in `rc.conf`, and they are not — so **rbp controls the Link
network** (brings up eth0 + starts the Link protocol), gated on enabling Link / a
linked device, which we can't trigger yet without working panel buttons. Next: RE
rbp's Link handler (`LnkHand`) to find the eth0/Link trigger, or drive it via the
panel once injection works; for real-LAN DJ-Link broadcast use a vmnet bridge.

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
`PLAYER_DECODER_0/1`). Add a FLAC decoder + register it in `playengine_Player`'s
format/decoder selection. (Device feature; modifies firmware.)

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
