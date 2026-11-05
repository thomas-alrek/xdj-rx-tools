# GPU‑2D / galcore bring‑up — session handover

**Status: the galcore/GPU‑2D device bring‑up is SOLVED and is now the default (and only)
boot path.** The unmodified firmware boots all the way to `#### Application Start!! ###`,
galcore fully initialises (`Galcore version 4.6.9.6622`, `/dev/galcore` exists), and the
Pioneer DJ / rekordbox boot splash renders (`docs/img/galcore-boot-splash.png`). The
GPU‑2D stubs, the GC355 VG core and the DVFS GPT kick are **always on**; there is no
longer an `XDJ_GPU2D=1` gate to set. An escape hatch remains for regression work:
`XDJ_GPU2D_OFF=1` restores the old behaviour (2D slot reports GC2000, galcore fails `-22`
and fast‑aborts).

This emulates the real controller's GPU so the kernel‑side galcore stack comes up like on
hardware — the device the firmware's dynamic GPU‑2D rendering needs now exists instead of
crashing on its absence. **Caveat (next step):** `qemu/build-nand.sh` still removes the
one DirectFB GAL gfxdriver (`libdirectfb_gal.so`), so DirectFB *itself* still
software‑renders the idle UI. That removal predates this fix (it was the workaround for
the galcore hang). Now that galcore initialises, the logical next step is to **restore
the gfxdriver and let DirectFB blit through galcore** — but note the known `gui_task`
crash on the hardware GPU‑2D blit path (USB browse, see memory `panel-and-usb-bringup`),
so that is a test, not a given.

**It boots on `-smp 1`, which is the launch default (`qemu/watch.sh`).** The real
hardware is dual‑core, and `-smp 2` is the eventual target, but `-smp 2` still has an
open DVFS rendezvous deadlock (below) — so the default stays `-smp 1` and is fully
bootable. Set `XDJSMP=2` to exercise the (not‑yet‑working) dual‑core path.

Authoritative detail: `docs/emulator-status.md` (feature A). Memory:
`galcore-minus22-solved.md`, `panel-and-usb-bringup.md`.

---

## What this session did

Starting point: the full‑galcore path was gated behind `XDJ_GPU2D=1` and hung at boot;
the earlier brief blamed the i.MX6 bus‑frequency DVFS and framed a "Part 2" GPU‑command
problem. Both are now solved, and the path is de‑gated to the default. The full chain,
and the fix for each link (all in the QEMU GPU stub, `qemu/patches/02` + the GPT‑kick
`qemu/patches/05`):

1. **galcore probe `-22` (2D core identity).** The 2D register slot must identify as a
   2D part. Fix: the 2D core reports **GC320** (model 0x0320) so galcore derives
   `gcvHARDWARE_2D`.
2. **i.MX6 bus‑frequency DVFS hang.** Enabling the GPU clocks triggers
   `set_high_bus_freq`, which steps the ARM/DDR clocks with CPU IRQs masked and
   busy‑polls `GICC_HPPIR` for a settling tick that never comes (the one‑shot GPT isn't
   reprogrammed in the masked window). Fix: **`imx_gpt_kick()`** (patch 05) raises the
   GPT OF1 interrupt (GIC ID 87) from `imx6_ccm_write` on the clock‑step registers
   (CCSR/CACRR/CGPR). Proven at the hang via gdb.
3. **The hidden GC355 OpenVG core.** i.MX6 has a *third* Vivante core (`gcvCORE_VG` at
   `0x02204000`, IRQ `V2D_IRQ` = GIC ID 43) that QEMU never mapped; galcore's VG
   clock/idle poll spun on the unmapped region forever. Fix: map a **GC355 VG stub**.
   (Found by walking the blocked init thread's kernel stack — PID 1, `tasks` offset
   0xb8 — to the poll loop.)
4. **VG construct `-13` (`gcvSTATUS_NOT_SUPPORTED`).** `gckVGCOMMAND` (`FUN_806919a0`)
   requires VG **chipFeatures (0x01c) bit 28** set — it derives
   `(chipFeatures & 0x1fffffff) >> 28` and rejects the core if zero. Fix: the VG core
   reports features with bit 28 (stub returns `0xffffffff` for VG 0x01c).
5. **"AXI BUS ERROR" / "MMU Exception!".** `AQIntrAcknowledge` (0x010) returned
   `0xffffffff`, whose **bits 31/30 are the AXI‑error / MMU‑exception flags**. Fix: mask
   them off.
6. **Command‑completion hang + "Vivante Kernel" NULL oops.** galcore `request_irq`s the
   VG line and blocks in `gckOS_WaitSignal` for the VG command to complete. Fix: **wire
   the VG IRQ + a completion timer**, raise it on the **VG DMA command kick (reg 0x504)**
   (not the interrupt‑enable 0x014), and report **only the submitted completion event**
   (event 30 — from the command's `0x0E01` load of value `0x5e`) **statefully** via a
   per‑core `cmd_pending` flag. Signalling all 30 events made galcore's event handler
   dereference unallocated event structs (the oops).

Then, this session's last step: **de‑gated the path to the default** (flip `gpu2d_full`
to default‑on in `hw/arm/fsl-imx6.c`; `XDJ_GPU2D_OFF=1` opts out) and **re‑verified a
clean `-smp 1` boot with no env set** — galcore 4.6.9 comes up, `rc_gpu.S` runs, and the
guest reaches `#### Application Start!! ###` with no panic/oops. Patches `02` and `05`
were regenerated so they carry the de‑gate and reproduce the build tree byte‑for‑byte.

**Key tooling unlock:** gdb hardware breakpoints **do work** in this QEMU build (the
old note "breakpoints non‑functional" was wrong). Set `Z1,<addr>,4`; to continue past a
breakpoint, step over it first (remove / single‑step / re‑add) or it re‑triggers. This,
plus Ghidra on a sliced region ELF (`galcore.elf`, 0x80670000‑0x80710000 — the full
13 MB kernel times out Ghidra), is what cracked the -22 chain.

---

## How to run / verify

```
python3 qemu_prepare.py        # once: build/qemu/{vmlinux.bin,atags.bin,tramp.bin}
qemu/build-nand.sh             # build/qemu/nand.img
qemu/build-qemu.sh             # applies qemu/patches/*, builds qemu-system-arm

qemu/watch.sh                  # live window: boots GPU-2D galcore on -smp 1 (default)
# headless + guest shell: scratchpad/diag.sh with SERIAL0_TCP=<port>, then log in
#   (root, empty password) and `ls -l /dev/galcore`  -> crw-rw-rw- 199,0
# screendump: monitor `screendump out.ppm`  -> the rekordbox splash
# regression compare: XDJ_GPU2D_OFF=1 qemu/watch.sh   (old -22 software-render path)
# dual-core (NOT yet booting): XDJSMP=2 qemu/watch.sh
```

The single‑core diagnostic tooling is in the session scratchpad (not committed):
`diag.sh` (headless, TCP monitor + `-gdb`, `ATAGS=`/`SERIAL0_TCP=` overrides), `rsp.py`
(GIC dump), `taskwalk.py` (walk the task list, find a thread's stack), `smp2probe.py`
(dump PC + current task per CPU at the `-smp 2` hang), `trace_c3.py` / `trace_vg2.py`
(breakpoint step‑over tracers), `mkelf.py` (ELF‑wrap at 0x80008000), `gc_console.py`
(guest‑shell expect client).

---

## Remaining: the `-smp 2` (dual‑core) path

`-smp 2` is the real hardware's core count and the eventual target, but it does **not**
boot yet — two blockers. The default is `-smp 1` precisely so the handed‑over build is
always bootable; `XDJSMP=2` opts into the broken path for debugging.

1. **Dual‑core DVFS rendezvous deadlock (known‑hard).** Under `-smp 2`, galcore's
   bus‑freq DVFS sometimes deadlocks with **both CPUs in WFI at `0x803de134`** (the
   dual‑core routine `FUN_803dc4e8`). It is a shared‑flag rendezvous: each CPU sets its
   byte in a per‑CPU array and spins (WFI between polls) waiting for `[flags]==0xff`
   (all CPUs ready) and the DDR change to complete — which never converges under
   emulation (the GPT kick wakes CPU0 but not CPU1, and the long‑suspected **MMDC**
   completion — DDR controller phys `0x021b0000`, modelled *not at all* — never
   signals). It is a **race**: galcore sometimes passes it (then hits #2). Prior
   sessions tried and failed: `-smp 2`, inject ID 87 pending, reprogram GPT, raise GPU
   IRQ 41/42, 256 MB RAM, `-icount`. Likely needs either a small **MMDC stub** that
   raises the awaited completion IRQ, or a QEMU ARM **WFI spurious‑wakeup** so both CPUs
   re‑poll. `smp2probe.py` dumps both CPUs' PC + current task at the hang; disassemble
   `FUN_803dc4e8` and the `0x803de130` WFI loop to find exactly which flag/IRQ each CPU
   awaits.
2. **Userspace udev‑coldplug hang.** When `-smp 2` *does* pass galcore, it then hangs in
   userspace at **"Synthesizing initial hotplug events"** (udevd replaying coldplug
   uevents) — a driver probe or SMP race triggered by a uevent, past galcore entirely.
   Not yet investigated. (On `-smp 1` this step passes cleanly — confirmed this
   session.)

When `-smp 2` boots reliably, flip the launch default (`XDJSMP` default in
`qemu/watch.sh`, and `run.sh`) to 2 and re‑test the panel/USB GPU‑2D blit paths
(features C/D) now that `/dev/galcore` is live.

---

## Commits (branch `gpu2d-dvfs-kick`)

- `5700f6f` DVFS settling‑wait GPT kick (patch 05)
- `b5a5a84` map the GC355 OpenVG core — galcore stops hanging
- `16fd40f`, `b7f1cee` -22 trace docs (Ghidra)
- `6f9a877` **galcore fully initialises — /dev/galcore exists, UI renders** (the fix)
- `75b88f6` patch‑02 regeneration fix (stop absorbing patch 04's change)
- _this session_ — **de‑gate: GPU‑2D galcore is the default/only path, `-smp 1` default**
  (patches 02 + 05 regenerated; `XDJ_GPU2D_OFF=1` escape hatch)
