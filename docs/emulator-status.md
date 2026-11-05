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

**Remaining stock-boot blocker — ROOT-CAUSED (2026-11-05, Ghidra on a region ELF):**
the hang is **NOT galcore itself** and not a GPU IRQ. Clearing `-22` lets galcore's
2D init enable the GPU AXI/bus clocks, which triggers the **i.MX6 bus-frequency DVFS**
(`set_high_bus_freq` → `FUN_803cf2c0` disables IRQs → `FUN_803dc544` → the DDR
frequency-change routine). That routine does a **settling wait for a timer interrupt**
that never completes under QEMU:
- **single-core** path `FUN_803dc2d4`: sets CCM `CACRR` (ARM clock-root divider,
  phys CCM 0x020c4000 +0x10), waits the `CDHIPR` handshake (+0x48), then busy-polls
  `GICC_HPPIR` (`while (*(GICC+0x18) == 0x3ff)`, phys 0x00a00118) for the next tick.
- **dual-core** path `FUN_803dc4e8`: both CPUs set a per-core flag, one does the MMDC
  change, then **both execute `WFI`** (kernel 0x803de134) as the settling delay,
  expecting a timer tick to wake them.
The i.MX6 clockevent is the **GPT** (QEMU IRQ 55 = GIC ID 87, enabled) in oneshot
free-run mode; during the IRQ-disabled DVFS section its OCR1 isn't reprogrammed, so
the single already-scheduled tick doesn't carry the sequence to completion. **Tested,
none fix it:** `-smp 1`, `-smp 2` (both CPUs hang at the same WFI), injecting GPT
ID 87 pending, reprogramming GPT OCR1 ahead (a tick fires — `hrtimer: interrupt took…`
— but the DVFS still doesn't finish), `-icount shift=auto`, and raising the GPU IRQ
lines (41/42, the earlier dead-end). The RAM size (256 MB) does not affect it either.
**This is the i.MX6 DDR DVFS — a known hard-to-emulate feature** (timing-sensitive
settling + IRAM code + inter-core sync). Fix options (unresolved, needs a design
call): (A) QEMU-side — model the bus-freq-change completion / deliver the settling
wake (preferred per the stock-firmware constraint; moderate effort); (B) a QEMU ARM
`WFI` spurious-wakeup/timeout so the dual-core settling returns (architecturally
valid; pairs with `-smp 2`); (C) disable the busfreq driver (no cmdline param exists
on this 3.0.35 kernel, so this means a rootfs/init tweak — conflicts with "stock").
Until one lands, the UI path is **sd-nogal** (galcore `-22` fast-fails harmlessly).
The `XDJ_GPU2D=1` gate (patch 02) clears `-22` and advances the boot to exactly this
DVFS point; the GPU command submission is fully modelled (2D MMU regs 0x400–0x40c =
0x18400000, FE cmd-buf reg 0x654 = a `WAIT 200; LINK→self` idle loop, kick reg 0x658).
**RE tooling that cracked it:** QEMU `-gdb tcp::<port>` + raw-RSP Python client
(halt/`g`/`m`/`M`, multi-CPU via `Hg<n>`), kernel VA→phys page-table walk
(swapper_pg_dir VA 0x80004000), and **Ghidra via REA on a 160 KB region ELF**
(0x803c0000–0x803e8000 extracted at its real base — the full 13 MB kernel times out
Ghidra's import, but a sliced region decompiles in seconds).

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
