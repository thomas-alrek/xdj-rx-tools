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
**`-22` is SOLVED.** Root cause: our 2D GPU stub reported a 3D chip (GC2000), but
galcore requires the 2D register slot to identify as `gcvHARDWARE_2D`. Fix: the 2D
core reports **GC320** (model 0x320, rev 0x5007). This is now in the build, **gated
behind the `XDJ_GPU2D=1` env var** (hw/arm/fsl-imx6.c, patch 02): default = old
behaviour (2D reports GC2000 → `-22` fast-fails → working sd-nogal UI path
untouched); `XDJ_GPU2D=1` = full-stock attempt (2D=GC320, clears `-22`, galcore
loads and boot advances to mount the SD card).

**Remaining stock-boot blocker (precisely localized, needs more RE):** with `-22`
cleared, galcore's 2D init reaches a **busy-poll** at kernel `0x803dc2ac`:
`while (GICC_HPPIR == 0x3ff) dsb/isb;` — i.e. it polls the **GIC CPU interface's
Highest-Priority-Pending register** (phys `0x00a00118`, galcore's ioremap
`0xf2a00100`; A9 PERIPHBASE = phys `0x00a00000`) waiting for *some* interrupt to go
pending, with CPU IRQs masked (`cpsr.I=1`; nearby galcore code does `cpsie i`). The
GIC Distributor (VA `0xf2a01000`) shows the enabled SPI set does **not** include the
GPU lines (ID 41 = GPU3D / ID 42 = GPU2D) — they only appear in ISPENDR once the
stub raises them, and *pending+disabled never reaches HPPIR*. So galcore is waiting
on a **different, enabled interrupt source** (enabled IDs include 34,35,37,38,44,…),
not the per-core GPU IRQ. Attempts that did **not** unblock it: raise R2D (ID 42)
synchronously, raise it from a timer after the kick, and raise both GPU lines
(41+42). Next: identify which enabled interrupt galcore's 2D init actually waits on
(disassemble the galcore caller at ~`0x806eee58`/`0x806f27f0` and its arch helper
`0x803cf2e0`→`0x80360784`→spin), then make that source fire in the model. The
command submission is understood: galcore sets the 2D MMU (regs 0x400–0x40c =
0x18400000), writes the FE command-buffer phys addr (reg 0x654 = 0x1827a000; the
buffer is a `WAIT 200; LINK→self` idle loop), and kicks the FE (reg 0x658 =
0x00010002). Scaffolding for timer-delivered completion IRQs on both GPU lines is in
the `XDJ_GPU2D` path (patch 02), ready once the right source is known. Full notes in
memory `galcore-minus22-solved`. Until finished, the UI path is sd-nogal (`-22`
fast-fails harmlessly). **RE tooling:** QEMU `-gdb tcp::<port>` + a raw-RSP Python
client (halt/`g`/`m`/`M`), kernel ELF wrapped at base 0x80008000, page-table walk to
translate kernel VAs (swapper_pg_dir VA 0x80004000).

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
