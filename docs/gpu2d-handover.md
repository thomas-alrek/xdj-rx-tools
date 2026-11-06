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

## Follow-up (2026-11-06): control-triggered dynamic render crash — root cause found + fix

With GPU‑2D merged and the panel protocol solved, the end goal (inject a decoded control
and see the UI react) exposed a crash: the first *dynamic* control-triggered redraw
SIGSEGVs rbp in **userspace**, `IMX6_GPU_TRACE` flat (delta 0 — no 2D command submitted),
PC in an anonymous `rwxp` buffer via a garbage pointer — a "call through an uninitialized
GPU‑2D callback" in libg2d/libGAL, *before* reaching the kernel-side completion logic.

**Root cause (found by static RE of the shipped libs — deterministic, no live repro
needed).** Extracted `libdirectfb_gal.so` / `libGAL-fb.so` from the rootfs (not stripped;
`debugfs` dump of nand `p1.ext4` in a container) and decompiled with Ghidra via REA:
- `driver_init_driver` (the gal gfxdriver) branches the whole blit setup on
  **`gcoHAL_IsFeatureAvailable(hal, 0xe)` and `(hal, 0x35)`** — it picks different
  blend‑function tables (`gal_source_blend_funcs` vs `…_full_dfb`) and zeroes vs sets
  different field groups based on them.
- `galBlit` sets hardware type **2 (`gcvHARDWARE_2D` = the GC320)** and dispatches to
  `gco2D_*` in libGAL, whose implementation is selected from the chip's **feature DB**.
- That feature DB is built by **galcore decoding the 2D core's feature registers**
  (chipFeatures `0x01c` + chipMinorFeatures0‑3 `0x034/0x074/0x084/0x088`) — this galcore
  4.6.9 has **no hardcoded chip DB** (no `GC320/0x5007` entry, no `FEATURE_DATABASE` in
  the kernel), so it relies entirely on the registers.
- **Our 2D stub reported `features = 0`** (and `0` for the minor‑feature regs), so the 2D
  feature DB was empty → `gcoHAL_IsFeatureAvailable` returned FALSE → the gfxdriver/gco2D
  took the degraded path and the dynamic blit called through a garbage `gco2D` pointer.

**The fix (patch 02, `hw/arm/fsl-imx6.c`).** The GC320 2D core now advertises its feature
registers: `chipFeatures (0x01c)` and `chipMinorFeatures0‑3 (0x034/0x074/0x084/0x088)`
(new `minor_features` field), so galcore builds a populated 2D feature DB and libGAL takes
the real blit path. (The minor‑feature regs are bitfields, not numeric specs, so
advertising them is safe; the GC2000 fallback keeps `0` — it `‑22` fast‑fails anyway.)

**Verified:** galcore now **reads** all five 2D feature regs (`0x01c/0x034/0x074/0x084/
0x088` on core `0320`, confirmed by `IMX6_GPU_TRACE`) and gets the advertised values
instead of 0; **no regression** — galcore still initialises clean and the deck UI still
renders (`e8b3e7…`), 0 oops/segfault. **Not yet verified in‑vivo:** I could not reproduce
the control‑injection crash in the headless harness here (injected frames were consumed by
the slave but not acted on by rbp — a panel‑decode/timing gap in this environment, separate
from GPU‑2D), so the end‑to‑end "press a control, UI redraws through the 2D core" path
still needs a live confirmation on a setup where control injection lands. The change cannot
regress what worked (boot/static UI verified; the dynamic path was already crashing), and
it directly removes the RE‑confirmed cause.

**To confirm end‑to‑end:** on a build where `echo "b 1b 1" > /tmp/subucom_ctrl` actually
makes the UI react, press a control and watch for a frame change (and GC320 `0x658`
command writes in `IMX6_GPU_TRACE`) instead of the SIGSEGV. If it still faults, the exact
site is now cheap to name: boot with `user_debug=31` on the kernel cmdline (atags) — the
kernel then prints the faulting thread's full register dump (PC + LR = the libGAL caller)
to the console on any user segfault — and map LR against `/proc/<pid>/maps` (libGAL‑fb at
`0x2b0f0000`, libg2d at `0x2ab99000`).

**Control-injection harness (QEMU slave, build tree):** `xdj_subucom_frame_start` in
`hw/arm/fsl-imx6.c` reads `/tmp/subucom_ctrl`:
`"b <off> <bitmask>"` holds a button (`b 1b 1` = MENU), `"a <off> <value>"` sets a 16-bit
analog (`a 24 200` = deck1 analog[0]); offsets/values hex. It builds the frame in rbp_buf
coords and computes the CRC‑16/X‑25 the parser requires. Full control map +
protocol: `docs/rbp-and-panel-protocol.md`.

---

## Follow-up 2 (2026-11-06, panel session): rbp DIES ~seconds after rendering the UI — the real blocker

The feature-register fix above (commit `6ae6347`) did **not** make control feedback work,
and in chasing it I found a bigger problem that the earlier "deck UI renders / no crash"
verification missed:

> **On the default galcore path, `rbp_solo` renders the deck UI successfully and then
> DIES within a few seconds.** The framebuffer freezes on the correct deck image
> (`e8b3e7…`), so a screendump looks like success — but the process is gone. It is not
> surviving to run, so no sustained/dynamic behaviour (control feedback, USB browse) is
> possible.

### Evidence (lifetime poll, `-smp 1`, default build)
```
t~20s:  fb=1cafe709            # boot splash
t~26s:  RBP=0  fb=e8b3e7ae     # deck UI rendered AND rbp already dead
t~32..50s: RBP=0 fb=e8b3e7ae   # frozen correct UI, rbp stays dead
```
So rbp draws the full deck UI through galcore, then exits between ~20–26 s.

### It is NOT any of these (ruled out this session)
- **Not the panel / my control injection.** rbp dies the same with the slave feeding
  *invalid* frames (`SUBUCOM_BADCRC=1` → rbp rejects every frame, no decode). My earlier
  "the menu/analog press crashes rbp" reports were a **misread**: rbp was already dead
  (frozen fb) *before* each trigger. The panel decode + CRC + control-inject harness are
  confirmed working — they're just blocked by this.
- **Not the feature-register fix.** rbp dies the same with the fix reverted
  (`XDJ_GPU2D_NOFEAT=1` zeros the GC320 `features`/`minor_features`).
- **Not a page-fault SIGSEGV.** Booting with `user_debug=31` on the kernel cmdline
  produces **no kernel fault dump** in `dmesg` or on the console when rbp dies. A real
  segfault *would* print with `user_debug=31`. So this is **not** the old
  "garbage gco2D pointer" SIGSEGV — rbp is dying by **SIGABRT/assert or a clean exit()**,
  silently (no console output, and no core because SD boot uses `ulimit -c 0`).

**Important context (from the user):** the non-GPU path (`XDJ_GPU2D_OFF=1`) does **not**
boot properly with the GAL gfxdriver present, so it's not a useful comparison. The only
historically-stable config was the *original* image (GAL gfxdriver **stripped** → pure
software DirectFB), where rbp ran for minutes and all the panel RE was done against a live
process. Keeping the gfxdriver (byte-for-byte rootfs) + galcore is where rbp stops
surviving.

### HOW TO REPRODUCE AND TEST (copy-paste)

Prereqs (build is current):
```
cd <repo>
export PATH="/opt/homebrew/bin:$PATH"
(cd build/qemu-src/qemu-11.1.0/build && ninja qemu-system-arm)   # QEMU
# build/qemu/{vmlinux.bin,atags.bin,tramp.bin,nand.img} already present
```

1) **Launch headless with a monitor + serial socket** (snapshot so the image is safe):
```
Q=build/qemu; QEMU=build/qemu-src/qemu-11.1.0/build/qemu-system-arm
rm -f /tmp/mon.sock /tmp/con.sock
"$QEMU" -M sabrelite -smp 1 -m 256 -no-reboot -display none \
  -device loader,file="$Q/vmlinux.bin",addr=0x10008000,force-raw=on \
  -device loader,file="$Q/atags.bin",addr=0x10000100,force-raw=on \
  -device loader,file="$Q/tramp.bin",addr=0x10000000,cpu-num=0 \
  -drive if=none,id=xdjsd,file="$Q/nand.img",format=raw,snapshot=on \
  -nic user -monitor unix:/tmp/mon.sock,server,nowait \
  -serial unix:/tmp/con.sock,server,nowait -serial null &
```

2) **Poll rbp's lifetime** (prove it dies after the UI renders). Two tools:
   - Framebuffer: connect to `/tmp/mon.sock`, send `screendump /tmp/fb.ppm\n`, hash it.
   - Process: log in on `/tmp/con.sock` (serial getty, **root / empty password**) and run
     `ps | grep -c '[r]bp_solo'` → prints `1` while alive, `0` once dead.
   Poll both every ~6 s from t≈20 s. You will see the fb reach the deck image while the
   `ps` count drops to **0** within a few seconds. (Scratchpad helpers used here:
   `shot4.py` = screendump via the monitor socket; `con4.py` = login+run-one-command via
   the serial socket, using a split marker `printf 'EN''DX_%d' $?` so the echo of the
   command isn't mistaken for the result.)

3) **Confirm it's not a segfault** — regenerate atags with `user_debug=31` and boot with it:
```
python3 - <<'PY'
import sys; sys.path.insert(0,".")
import qemu_prepare as q
open("/tmp/atags_dbg.bin","wb").write(q._atags(q.CMDLINE+" user_debug=31", revision=q.BOARD_REV_SABRESD))
PY
# relaunch as in (1) but swap the atags loader to file=/tmp/atags_dbg.bin
# then in the guest: `dmesg | tail -40` after rbp dies -> NO "unhandled fault / PC is at".
```

4) **Diagnostic toggles** (env, build tree `hw/arm/fsl-imx6.c`, not committed):
   - `SUBUCOM_BADCRC=1` — slave corrupts the frame CRC so rbp rejects all panel frames
     (isolates the panel: rbp still dies → not the panel).
   - `XDJ_GPU2D_NOFEAT=1` — zeros the GC320 `features`/`minor_features` (reverts the
     feature fix while keeping galcore on: rbp still dies → not the fix).

### Next step for the GPU session: get the death cause
It's not a page fault, so `user_debug` won't name it. Options, in order:
1. **Enable a core**: edit the guest `/root/pioneer/apl_start` (or relaunch rbp by hand)
   with `ulimit -c unlimited` + `echo /tmp/core.%e.%s > /proc/sys/kernel/core_pattern`
   before `rbp_solo &`, reproduce, then read the core's **signal** and **PC/LR** and map
   against `/proc/<pid>/maps` (libGAL-fb `~0x2b0f0000`, libg2d `~0x2ab99000`, rbp text
   `0x8000–0x459000`).
2. **gdb** (breakpoints work in this build): breakpoint the kernel's `do_exit` / signal-
   delivery path, catch `rbp_solo`, read the exit code / signal number and the user PC.
3. Check whether rbp writes any log file in the guest around the death (`/root/pioneer`,
   `/tmp`, `/var/log`).
The question to answer: **why does rbp exit/abort within seconds of the first galcore
render?** (A failed assert on a galcore ioctl result is the leading guess — galcore
"renders" the static UI via the completion stub, but something it returns to rbp is wrong
enough that rbp aborts shortly after.)
