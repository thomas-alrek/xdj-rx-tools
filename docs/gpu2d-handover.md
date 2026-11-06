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
crashing on its absence. The rootfs is now **byte‑for‑byte** (commit keeps
`libdirectfb_gal.so`; `qemu/build-nand.sh` no longer strips it), so DirectFB blits are
routed to the real galcore device.

**The full live deck UI now renders through galcore** (DECK 1/DECK 2, BEAT EFFECTS,
120 BPM, "Not Loaded.") — pixel‑identical to the software‑render output, no `gui_task`
crash. This was the last blocker: galcore's **runtime 2D command‑completion** is now
emulated (the stub parses each submitted command's EVENT id and raises exactly that
completion, instead of a fixed mask — see "Runtime 2D completion" below). So on the
default path, vanilla rbp drives galcore all the way to the working UI.

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

## Runtime 2D completion (SOLVED — the deck UI renders through galcore)

**Was:** with the GAL gfxdriver present (byte‑for‑byte rootfs), the firmware booted to the
Pioneer/rekordbox splash and never advanced to the deck UI (rbp's `UiMain` hung in a
galcore ioctl on the first runtime 2D blit). With the gfxdriver stripped it reached the
deck UI in ~16 s via software DirectFB. **Now fixed:** the deck UI renders through galcore,
pixel‑identical to software render (same framebuffer md5), no `gui_task` crash.

**The fix (QEMU stub, patch 02 — `xdj_gpu_cmd_event` in `hw/arm/fsl-imx6.c`).** galcore
ends every submitted command with an EVENT command — a `LOAD_STATE` of one word to the
AQEventRegister (`0x0E01`) whose value is `(eventId | source)` — then waits for *that*
event id. Init used a fixed id (so the old fixed `events=0x3fffffff` mask happened to
work), but **runtime 2D blits allocate a rotating event id per commit**, so the fixed mask
left the commit's completion stall waiting forever. The stub now, on each 2D command kick
(`AQFEControl`, `0x658`): takes the command‑buffer address from the preceding
`AQFEStartAddress` (`0x654`) write — galcore runs the FE **MMU‑off**, so the address
(e.g. `0x1827a000`, in the GPU‑reserved DRAM above the `0x18000000` framebuffer) is
directly readable guest memory — reads the buffer (length = `0x658` low‑16 prefetch count
× 2 words), scans for the EVENT command (`(w>>27)==6 && (w&0xffff)==0x0E01`), extracts the
event id (`value & 0x1f`), and raises **exactly `1<<id`** on the completion IRQ. If a
command carries no EVENT command (some init commands), it keeps the default mask. No actual
pixel blitting is emulated — only completion — yet the UI is pixel‑perfect, so rbp/DirectFB
do the drawing and use galcore for the command/sync handshake (matches "rbp only blits").

**How it was found.** gdb + QEMU‑monitor PC sampling + Ghidra (REA) on kernel slices:
`UiMain` was mostly *sleeping* in galcore (caught on‑CPU ~1/40); the GPU trace was empty
during the stall (no command reached hardware); a task walk showed all galcore
daemon/Vivante‑Kernel threads idle‑waiting on `gckOS_WaitSignal`; the ~50 % CPU was the
separate benign swapper busfreq idle loop. That pointed at the completion handshake, and
the init trace gave the FE registers + the physical (MMU‑off) command‑buffer address.

**Diagnosis (this session, via gdb + QEMU‑monitor sampling + Ghidra on kernel slices).**
- rbp's UI thread (`UiMain`) enters a galcore ioctl for the first real 2D blit and
  **blocks**; it is caught on‑CPU only ~1 sample in 40, i.e. it is mostly *sleeping*, not
  spinning.
- The GPU register trace (`IMX6_GPU_TRACE=1`) during the stall is **empty** — only the 121
  boot‑init accesses, then nothing. **No 2D command ever reaches the GPU registers.** So
  galcore stalls *before* dispatching the blit to hardware, not while polling it.
- A task walk (`walk2.py`, tasks offset **0xb8**, comm +0x1ec, thread_info cpu_context
  sp +0x3c / pc +0x40) shows every galcore thread idle‑waiting on a signal: the 3
  "galcore daemon" per‑core ISR threads at `gckOS_WaitSignal` (`0x8067a048/0b4/120`), and
  the 2 "Vivante Kernel" command threads in `FUN_80692cf4` — a `while(WaitSignal(sig,
  INFINITE)) { … }` loop (waiting for work that never arrives).
- The host's ~50 %+ CPU during the stall is the **swapper/idle `busfreq` DVFS loop**
  (`0x803cf094`→`0x803dc608`, the documented GPT‑kick side effect) — a *separate, benign*
  burn, not the blocker (it runs on the stripped image too, which still reaches the UI).

**Conclusion.** galcore *initialises* (device exists, init commands complete via the
`fe_timer` IRQ on submit), but its **runtime 2D command‑completion handshake is not
emulated**. rbp's first blit commit stalls — most likely in the commit's "stall / wait
for previous completion" step, whose completion event the stub didn't account for
correctly. The stub delivers a fixed event mask (2D `events=0x3fffffff`); galcore's
runtime path allocates a **rotating event id per commit** (it writes an EVENT command
`LOAD_STATE 0x0E01, val = eventId|source` into the command buffer, exactly as the VG
path does with event 30 / `0x5e`), then waits for *that* id. A fixed mask mis‑accounts it.

**Fix plan (QEMU‑side, the real work).** Emulate per‑commit 2D completion properly:
1. On the 2D command kick (write to `0x654/0x658`), read the submitted command buffer
   from guest memory (the address galcore programmed), scan it for the trailing EVENT
   command (`LOAD_STATE` to `0x0E01`), and extract the event id from its value byte.
2. Deliver **exactly that event** in `AQIntrAcknowledge (0x010)` when the completion IRQ
   fires — generalising the VG fix (fixed event 30) to the dynamic 2D case, so galcore's
   `_Event`/signal accounting matches and the commit's stall releases.
3. Re‑check the init path still completes (don't regress the `-22`/probe fixes), then
   verify the deck UI renders through galcore and watch for the `gui_task` crash on the
   heavier USB‑browse blit path.

This is genuine command‑processor emulation (iterative RE + implement + test), materially
bigger than the init stub. Tooling for it is in the scratchpad: `walk2.py` (task walk +
sleep pc), `profpc.py` (monitor PC profile/bucket), `catchui.py` (catch a user thread
on‑CPU + stack), `darm.py` (capstone disasm at a kernel VA), `mkelf.py` (slice→ELF for
Ghidra via REA, e.g. `galcore.elf` 0x80670000‑0x80710000, `gal_os.elf` 0x803e0000‑0x80408000).

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

---

## Follow-up (2026-11-06): control-triggered dynamic render still crashes (next item)

With GPU‑2D merged and the panel protocol solved, we tried the end goal: inject a
decoded control and see the UI react. Result:

- **The input works end-to-end.** A decoded control event (MENU = `[0x1b]` bit0, or a
  deck-analog value) reaches rbp and makes it *act* — confirmed because injecting one
  reliably **kills `rbp_solo`** (a rejected/undecoded frame cannot). Baseline is stable
  (rbp alive, UI steady) right up to the inject.
- **But the first *dynamic* redraw crashes rbp in USERSPACE, before galcore.**
  `IMX6_GPU_TRACE=1` shows the GPU-register count **flat at 121 (the boot-init accesses)
  across the control press — delta 0**: no 2D command is ever submitted. So rbp faults
  in the libg2d/libGAL dynamic-blit *setup* (the old "call through an uninitialized GPU‑2D
  callback" path) before anything reaches the galcore completion logic you fixed.
- rbp catches the SIGSEGV itself (its own handler) → **no kernel oops, no core** (cores
  only enabled on NFS boot via `with_core_rbp`; SD boot uses `ulimit -c 0`). The
  framebuffer freezes on the last good frame (`e8b3e7…`, the settled deck UI).

**Distinction that matters:** your fix made the **static** deck UI render through galcore
(the 121 boot commits complete fine). This crash is the **first dynamic/control-triggered
redraw**, and it dies in **userspace g2d/GAL** *before* submitting a command — so it's a
different gap than the kernel-side completion. Likely the dynamic blit needs a
surface/context or callback that isn't initialized on this path (same family as the
anticipated USB-browse blit crash).

**To get the exact fault:** enable cores before rbp starts (set `kernel.core_pattern`
+ `ulimit -c unlimited` in `apl_start` before the `rbp_solo &`, or relaunch rbp by hand),
reproduce (`echo "b 1b 1" > /tmp/subucom_ctrl` on a booted UI), then read the core's PC
and map it against `/proc/<pid>/maps` (libg2d at ~`0x2ab63000`, rbp text `0x8000‑0x459000`).
Or catch it in gdb (breakpoints work) at the kernel user-fault path. That names the
crashing library/function to fix.

**Control-injection harness (QEMU slave, build tree):** `xdj_subucom_frame_start` in
`hw/arm/fsl-imx6.c` reads `/tmp/subucom_ctrl`:
`"b <off> <bitmask>"` holds a button (`b 1b 1` = MENU), `"a <off> <value>"` sets a 16-bit
analog (`a 24 200` = deck1 analog[0]); offsets/values hex. It builds the frame in rbp_buf
coords and computes the CRC‑16/X‑25 the parser requires. Full control map +
protocol: `docs/rbp-and-panel-protocol.md`.
