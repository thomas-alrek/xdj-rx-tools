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

**Boot-speed:** the multi-second stall at the splash (`wait otg vbus change
timeout!`, between `ehci_hcd` and `fsl-ehci.0`) was the i.MX6 OTG controller
waiting for a VBUS/session-valid transition that QEMU's stubbed USB PHY + chipidea
OTGSC (both read 0) could never deliver, so it only continued at the kernel
timeout. Fixed by `qemu/patches/12-imx6-otg-vbus-valid.patch` (OTGSC reports a
settled host VBUS/session); the OTG core now registers with no gap and USB still
mounts. Firmware untouched.

**Auto-level:** the mixer is auto-initialised to an audible default on boot
(`qemu/patches/13-imx6-subucom-autolevel.patch`) so a loaded deck plays without a
manual gain/routing inject. The sub-ucom harness uses the panel SPI poll itself as
the "rbp is up" signal and, after a short settle, holds both channels to DECK +
unity-ish gains whenever no `/tmp/subucom_ctrl` override is present. Verified: the
routing tables flip to DECK on their own. `XDJ_AUTOLEVEL=0` disables it;
`XDJ_AUTOLEVEL_DELAY_MS` tunes the settle (default 4000).

## ✅ A. galcore / Vivante GPU — fully initialises (default path)
The built-in galcore (Vivante) driver now fully initialises on the default boot path:
the QEMU GPU stub (patch 02) + GC355 VG core + DVFS GPT kick (patch 05) carry it through
init, so `/dev/galcore` exists and boot reaches `#### Application Start!! ###` (details
below). The rootfs is now byte-for-byte (the DirectFB GAL gfxdriver `libdirectfb_gal.so`
is kept), and **the full live deck UI renders through galcore** (pixel-identical to
software render, no `gui_task` crash). The last blocker — galcore's runtime 2D
command-completion — is emulated in patch 02 (`xdj_gpu_cmd_event`): the stub parses each
submitted command's EVENT id (`LOAD_STATE 0x0E01`) from the command buffer and raises
exactly that completion event, instead of a fixed mask, so rbp's per-commit blit stalls
release. Full write-up in `docs/gpu2d-handover.md` ("Runtime 2D completion").

**The full-galcore path is now the DEFAULT (and only) boot path.** Making galcore fully
initialise needs patch 02's GC320 2D identity (so galcore derives `gcvHARDWARE_2D` and
clears `-22`) plus the mapped GC355 VG core and the DVFS GPT kick. That path is now
**always on** and boots to the live UI on `-smp 1` (the launch default); the old
software-render behaviour is reachable only as a regression escape hatch
(`XDJ_GPU2D_OFF=1`: 2D slot reports GC2000, galcore fails `-22` and fast-aborts). The
remaining open work is the real dual-core `-smp 2` config — see `docs/gpu2d-handover.md`.

The galcore **boot hang is SOLVED** (session 4); galcore runs its full init and the
firmware boots to `#### Application Start!! ###`. Three nested blockers were found and
cleared:
1. **i.MX6 bus-frequency DVFS settling wait (SOLVED).** Enabling the GPU clocks makes
   galcore call `set_high_bus_freq`, which steps the ARM/DDR clocks with CPU IRQs
   masked then busy-polls `GICC_HPPIR` for a settling tick. Under emulation the
   one-shot GPT clockevent isn't reprogrammed in that masked window, so nothing goes
   pending and the poll spins forever (proved at the hang via gdb: no enabled IRQ
   pending → HPPIR stays `0x3ff`). **Fix: `imx_gpt_kick()`** (patch 05) raises the GPT
   OF1 interrupt (GIC ID 87) from the CCM clock-step register writes (patch 05 hooks
   `imx6_ccm_write` on CCSR/CACRR/CGPR; patch 02 arms it on the full-galcore path,
   now the default).
2. **Idle-loop feedback (benign side-effect).** The GPT kick also wakes the CPU-idle
   WFI, whose `imx6q_enter_wait` runs its own bus-freq DVFS → a busy idle↔busfreq loop.
   It does not block progress (galcore's init still completes); it only burns host CPU.
   A `GICC_HPPIR` read-overlay would avoid it but doesn't reach guest GIC accesses under
   QEMU's a9mpcore mapping, so the kick is kept as-is.
3. **The GC355 OpenVG core (SOLVED — the real hang).** The deadlock that masked
   everything else was galcore's **VG core** init: i.MX6 has a *third* Vivante core,
   the GC355 OpenVG engine at `0x02204000` (`gcvCORE_VG`, core index 2), which QEMU did
   not map. galcore enumerates and initialises all three cores; its VG clock/idle poll
   (`gckOS_ReadRegisterEx` core 2, regs 0x004/0x000) read the unmapped region, got 0,
   and spun forever (the "idle"/"clock-running" bits never set) — found by walking the
   blocked init thread's stack (PID 1, `tasks_off=0xb8`) to `gckOS_Delay` inside the
   poll. **Fix: map a GC355 VG stub at `0x02204000`** (patch 02, on the default path),
   reusing the idle/clock-reporting stub ops. With it, galcore's core init completes and
   boot runs through to Application Start.

**SOLVED (Part 2): galcore fully initialises — `/dev/galcore` exists.** On the default
path the firmware now boots to `#### Application Start!! ###` with no `-22`,
no MMU/AXI error and no oops; a guest shell confirms `crw-rw-rw- 199,0 /dev/galcore`,
and a framebuffer screendump shows the Pioneer DJ / rekordbox boot splash
(`docs/img/galcore-boot-splash.png`). The `-22` and the crashes after it were traced
with Ghidra (on a sliced region ELF — the full 13 MB kernel times out Ghidra's import)
and with gdb hardware breakpoints (which *do* work here — the old "breakpoints
non-functional" note was wrong). `gckGALDEVICE_Construct` builds three cores (3D/2D/VG);
the 2D-type check passes (GC320 derives type 2), and the real blockers were all in the
**GC355 VG (OpenVG) core**, fixed in the GPU stub (patch 02):
- **VG construct `-13` (gcvSTATUS_NOT_SUPPORTED):** `FUN_806919a0` requires VG
  chipFeatures (0x01c) **bit 28** set (it derives `(chipFeatures & 0x1fffffff) >> 28`
  and rejects the core if zero). The stub now reports VG features with that bit.
- **"AXI BUS ERROR" / "MMU Exception!":** `AQIntrAcknowledge` (0x010) was returning
  `0xffffffff`, whose bits 31/30 are the AXI-error / MMU-exception flags. Masked off.
- **Command-completion hang + "Vivante Kernel" NULL oops:** the VG core has its own GIC
  line (`V2D_IRQ` = ID 43), which galcore `request_irq`s and then blocks in
  `gckOS_WaitSignal`. The stub now wires that IRQ and, on the VG command kick (DMA
  control 0x504, not the interrupt-enable 0x014), raises it and reports **only** the
  submitted completion event (event 30, from the command's `0x0E01` load of `0x5e`) in
  0x010 — signalling all 30 events made the event handler dereference unallocated event
  structs.

So the GPU-2D path is now live and dynamic blits have a real `/dev/galcore` to go
through. Under `-smp 2` (the real dual-core config) galcore also fully inits (no -22, no
crash, 2 CPUs up); a separate **userspace udev-coldplug hang** remains on that path.

RE tooling: QEMU `-gdb` + a raw-RSP python client (halt+read; breakpoints are
non-functional here), kernel ELF wrapped at base `0x80008000` for objdump, a validated
frame-pointerless backtrace (stack words whose `[addr-4]` is a `bl`/`blx`), and a
statistical PC profiler. Ghidra (REA) on a sliced region ELF (the full 13 MB kernel
times out Ghidra's import).

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

## 🔶 D. USB mass storage (= the original exFAT goal)
**Cold-plug now works end to end.** A `usb-storage` disk present at boot
(`XDJUSB=<image> ./run.sh`, wired in `qemu/watch.sh`, on `usb-bus.0`) fully
enumerates — `usb 1-1: new … device`, `scsi … Direct-Access`, `sd … [sda]`,
`sda: sda1` — and the firmware **auto-mounts** it at `/media/usb1/sda1` (vfat)
and writes `mount /media/usb1/sda1` into `/proc/udev_usb1`, which is exactly what
rbp's `DBSA_Task_USB` thread watches. Verified with the rekordbox test image
(`test/usb/`, PIONEER/rekordbox/export.pdb etc.). The earlier "doesn't enumerate"
note was from the pre-SABRE-SD wiring and is obsolete.

Two live caveats:
- **Hot-plug does NOT enumerate.** `device_add usb-storage` after boot leaves
  EHCI PORTSC = `0x00501003` (CCS=1 connect, CSC=1, **PED=0** not-enabled) and the
  host suspends — the port is never reset/enabled. So the stick must be present at
  boot. (Fixing hot-plug = the ChipIdea EHCI port-enable/resume path.)
- **rbp crashes rendering the rekordbox content.** With the stick cold-plugged,
  rbp's `gui_task` SIGSEGVs (signal 11): PC lands in an anonymous `rwxp` buffer
  via a garbage function pointer — a **call through an uninitialized GPU 2D
  callback**. `libg2d.so`/`libGAL-fb.so` are loaded but galcore is dead (-22), so
  when rbp reaches the artwork/browse blit path it jumps into garbage. The idle UI
  software-renders fine; USB content reaches the dead GPU path. **This re-opens the
  galcore/GPU-2D problem (feature A).** To "see" the USB track we need that 2D path
  to work (or a software fallback / to avoid the blit).

**exFAT itself:** the 3.0.35 kernel has vfat/msdos/**ntfs** but **no exFAT and no
FUSE** — so the old "fuse-exfat" plan won't work; exFAT needs an out-of-tree kernel
module (e.g. exfat-nofuse) built against this kernel + a mount rule. (FAT32 is used
for the current bring-up test image.)

## ✅ E. Audio
**DONE (2026-11-11/12).** The i.MX6 audio path is modelled in `qemu/patches/11` (a minimal
SDMA engine + RAM-backed SSI1/2/3 + AUDMUX), so imx-sdma/ASoC/ALSA run and the ALSA playhead
advances in real time. A host sonifier pinned to SDMA **ch1 = MasterOut** emits **real deck
audio to the host** (both decks, user-confirmed). Gated on: routing = DECK (source switch
`B 33 80`/`B 43 80`) + gains up (on-air gate `FUN_00065028`). See `docs/audio-handover.md`,
`docs/audio-silence-handover.md`, memory `[[deck-playback-audio-gated]]`.

## ⬜ F. FLAC playback
rbp decodes **MP3 (libmpg123), AAC, M4A, AIFF, WAV — no FLAC** (two decoder slots
`PLAYER_DECODER_0/1`). The codecs are **dlopen'd** (e.g. `libmpg123.so.0` with
`mpg123_*` resolved by name, not a static PLT symbol), so the player has a
format→codec-lib factory. Adding FLAC = ship a FLAC codec lib + register it in that
factory's format map (follow-up: locate the dlopen/format-map factory in
`playengine_Player`). (Device feature; modifies firmware.)

## 🔶 C / G. Controls → app (panel buttons)
Control surface (buttons/jogs/faders) comes via the Sub-ucom panel MCU over SPI
(`/dev/subucom_spi1.0`). **The panel now boots and rbp actively reads it.** The
root cause of "rbp never reads the panel" was *not* a disabled poll — it was a
**boot deadlock** in panel init: rbp pushes an initial USB state to the panel
(triggered by a USB-detect GPIO reading 0) via `peer->send()` *before* the panel
poll thread exists, and spins forever. QEMU reset every GPIO input to 0 ("USB
present"); on real hw the USB-detect lines idle HIGH. **Fixed** by
`qemu/patches/04-imx6-usb-detect-gpio.patch` (reset GPIO4_IO30 / GPIO7_IO12 high —
no firmware change). Verified on a stock boot: `/dev/subucom_spi1.0` open, the
`UiMain` poll thread running + epoll up, and `SUBUCOM_TRACE=1` shows it clocking
the SSI slave (~300k MOSI bytes). Full story + symbols:
`docs/rbp-and-panel-protocol.md` → "SOLVED".

**SOLVED + DECODE CONFIRMED (2026-11-05).** The panel input protocol is reverse-
engineered and the `xdj-sub-ucom` slave now feeds frames that rbp **decodes end to
end** (proven: injecting a decoded control crashes rbp in its render path — a
rejected frame could not). The real gate was never the auth/handshake — it was a
**per-frame CRC**: the MISO `SubRxData` frame (class `ui::SubRxFormat`) is a flat
100-byte frame and parser `FUN_00284d44` decodes it only when a **CRC-16/X-25**
(poly 0x8408, init/xorout 0xFFFF) over the first 96 bytes (each 32-bit word
byte-swapped) matches the LE halfword at `[0x62]`. Sections: `refFmtData` +0x04,
`refCommonData` +0x14 (global buttons `[0x1a]/[0x1b]`), `tMixerData` +0x24/+0x34
(per-deck analog), `refOtherData` +0x44. The `E-7206 AUTH CHIP ERROR` was a red
herring — it auto-clears, and the StaticInfomation handshake is a lazy
version-screen cache (confirmed uninitialized in rbp's live memory), not on the
button path. Full protocol: `docs/rbp-and-panel-protocol.md`.

**UPDATE (2026-11-06): DECODE NOW TRULY WORKS — the slave frame byte-order was
wrong; several 2025-11-05 claims above are corrected here.** The real blocker was
a QEMU-slave byte-order bug: the ECSPI shifts each 32-bit word MSB-first, so
`rbp_buf[j] = s->frame[(j&~3)+(3-(j&3))]` (a **per-word byte reversal, no linear
shift**), but the slave used `memcpy(s->frame+3, …)` (bogus +3). That pushed the
CRC off the 100-byte burst → rbp read CRC=0 → every frame rejected → **zero
decode**. **Fixed in patch 02** (`s->frame[i]=rbp[(i&~3)+(3-(i&3))]`). Verified
live (gdb): CRC matches, the handler runs the NORMAL decode path (`this+0x88`
initialised, frames diffed), and a control change drives the section decoder →
dispatch. Corrections to above: the "injecting a control crashes rbp" proof was a
**phantom crash** (a `ps|grep` artifact — rbp is daemonised; use `pidof`), and
**gdb breakpoints DO work here** (the "non-functional" note was wrong). Full
detail: `docs/rbp-and-panel-protocol.md` → "FRAME TRANSPORT SOLVED".

**USB mount is crash-free (2026-11-06):** booting with
`XDJUSB=test/usb/rekordbox_test.img` (cold-plug `usb-storage`), the stick
enumerates and mounts (`/dev/sda1 → /media/usb1/sda1 vfat`) and rbp stays healthy
(pid alive, 79 threads) — no crash. The deck screen does not auto-switch on insert
(expected; you open the browser with BROWSE).

**✅ UI VISIBLY REACTS — SEEN LIVE (2026-11-09).** Injected panel controls now
**visibly change the deck UI**, reproduced live in a Cocoa window, with rbp healthy
throughout (pid 2405, 79 threads). The earlier "no fb change / nothing reacts" was a
**measurement artifact** (by-eye diffing of subtle sticky toggles on pre-fix builds);
md5-diffing each screendump catches the changes. Confirmed reacting controls (all-bits
granularity): `0x12`→**DECK1 TEMPO ±10→±16**, `0x22`→**DECK2 TEMPO ±16**, `0x56`→**BEAT
EFFECTS 500ms→10ms**, `0x5b`→**ROLL (orange)+TAP BPM (green)**. The render path is NOT
the bottleneck: this build renders via **software DirectFB straight to fb 0x18000000**
(GPU cmd traffic frozen at 1× `0x658` since init), so the 2D stub isn't even in the live
redraw path. Full detail + method: `docs/rbp-and-panel-protocol.md` → "UI VISIBLY REACTS".

**Remaining (refinement, not a blocker): complete the XDJ-RX control map.** The all-bits
byte sweep proves visible reaction but doesn't isolate individual controls; the full map
needs (a) a **per-bit pre/post-diff** sweep of each reacting byte, (b) an **analog sweep**
of the section ADC offsets (faders / jog / knobs), and (c) correlation of each hit to the
visible UI element, the static decoder code table (`docs/rbp-and-panel-protocol.md` →
"FULL control map"), and the physical XDJ-RX control (BROWSE/rotary/LOAD still to pin).
Verification tooling: guest shell (root, empty pw, `-serial` socket), `pidof`/`/proc`,
the **working** gdbstub (`-gdb tcp::1234`, resume with gdb `c`), `scratchpad/launch_live.sh`
(visible window), `scratchpad/sweep_btn.py` (md5-diff sweep).

## RE tooling notes
- Kernel: link base **0x80008000** (PAGE_OFFSET 0x80000000); wrap `vmlinux.bin` as
  an ELF at that base for objdump/Ghidra. Ghidra-via-REA times out on the 13MB
  kernel — use `objdump -d` + QEMU `-s -S` gdbstub instead.
- rbp: ARM ELF, C++ RTTI survives; REA/Ghidra works well (literal pools resolve).
