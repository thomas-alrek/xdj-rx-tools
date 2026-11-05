# GPU‑2D bring‑up — handover brief (start of a fresh session)

**Goal:** get the i.MX6 GPU‑2D (Vivante GC320) working under QEMU so rbp's
**dynamic** rendering stops crashing. This unblocks two things at once:
- **visible panel-control feedback** (a moved fader / pressed button updating the UI),
- **USB track browsing** (artwork/waveform blits) — feature D.

**Hard constraints**
- **Never patch the `rbp` binary** (the user needs vanilla rbp so future custom
  firmware is tested against a real controller). QEMU‑side fixes strongly preferred;
  a kernel/rootfs change is a last resort and deviates from "unmodified firmware".
- Keep the **current shipping path working** (default `nand.img`, galcore dead `-22`,
  DirectFB software render). Gate all GPU‑2D changes behind an env flag like the
  existing `XDJ_GPU2D=1` so the default boot stays byte‑identical.

---

## Where things stand (context)

The **panel/control protocol is fully solved** (see `docs/rbp-and-panel-protocol.md`):
CRC‑16/X‑25 cracked, decode confirmed end‑to‑end, full control map documented. The
*only* reason control changes aren't visible on screen is this GPU‑2D crash:

> galcore fails `-22` → `/dev/galcore` absent → libg2d's device never initializes →
> any dynamic **blit** calls through an **uninitialized 2D callback** → SIGSEGV.
> The idle UI survives only because it software‑renders (the GAL gfxdriver was removed
> from `nand.img`). Proven: injecting a decoded control (a deck analog value) crashes
> rbp in the render path; kernel fault dump shows the 800×480 fb dims = a blit.

So GPU‑2D is **the** remaining blocker. Also read the two memory files
`galcore-minus22-solved.md` and `panel-and-usb-bringup.md` for the deep history.

---

## Part 1 — get galcore to finish init (beat the DVFS hang)

The `-22` is **already solved & committed, gated behind `XDJ_GPU2D=1`**
(`qemu/patches/02`): make the 2D core report **GC320** (model `0x0320`, rev `0x5007`)
so galcore derives `gcvHARDWARE_2D`. With `XDJ_GPU2D=1` the `-22` clears and galcore
starts 2D init — **but boot then hangs** in the i.MX6 DDR **DVFS**, because enabling
the GPU bus clocks triggers `request_bus_freq(HIGH)` → `set_high_bus_freq`.

### The exact hang (confirmed this session, kernel link base `0x80008000`)
Single‑core DVFS routine `FUN_803dc2d4` writes **CCM_CACRR** (`CCM`+0x10, phys
`0x020c4010`), polls **CCM_CDHIPR** (+0x48 — QEMU returns 0, so that's fine), then
calls **`0x803dc2ac`**, which is the hang:
```
803dc2ac: movw r4, #0x3ff          ; 0x3ff = GICC "no pending IRQ"
803dc2b8: bl   0x803dae84          ; per-iter dsb / cpu_relax
803dc2bc: ldr  r3, [GICC_base_ptr] ; GICC base = *(0x80cc7a9c) = 0xf2a00100 (phys 0x00a00100)
803dc2c0: ldr  r3, [r3, #0x18]     ; GICC_HPPIR (phys 0x00a00118)
803dc2c4: cmp  r3, #0x3ff
803dc2c8: beq  0x803dc2b8          ; spin while NOTHING is pending  ← HANG
```
i.e. with CPU IRQs masked, galcore busy‑waits for **any** interrupt to go pending in
the GIC. Nothing does, so it spins forever.

### What was already tried and FAILED (don't repeat blindly)
`-smp 1`, `-smp 2`, inject ID87 pending, reprogram GPT OCR1 so a tick fires
(*tick fired but `HPPIR` stayed `0x3ff`*), raise GPU IRQ 41/42, `-icount shift=auto`,
256 MB RAM. GPU lines **ID 41 (GPU3D) / ID 42 (GPU2D) are NOT enabled** in the GIC
distributor. Enabled SPI IDs = `[34,35,37,38,44,50,51,56,63,64,67,68,69,72,75,87,
98‑112,150]` (87 = the GPT clockevent).

### The real question to crack
**Why does making an enabled IRQ pending not make `GICC_HPPIR` != `0x3ff`?** That's a
QEMU GIC CPU‑interface subtlety. Concrete things to try next:
1. **Halt at the hang via gdb and dump the live GIC state** (`-S -gdb tcp::PORT` at
   launch — breakpoints worked in the galcore sessions; note the *monitor*
   `gdbserver`+breakpoint path was flaky in the panel session, so launch with `-gdb`).
   Read GICC_PMR (`+0x04`), GICC_CTLR (`+0x00`), GICC_RPR (`+0x14`), GICD enable/pending/
   priority for ID 87. Hypothesis: the pending IRQ's priority is **below GICC_PMR**, or
   the CPU interface sees a raised **running priority**, so HPPIR reports `0x3ff`.
2. Disassemble the DVFS caller's **continuation after `0x803dc2ac` returns**
   (`FUN_803dc2d4` around the `bl 0x803dc2ac` sites `0x803dc30c/0x803dc3f0/0x803dc44c`)
   to see **which** interrupt it expects / acks — that names the IRQ to deliver.
3. **Model the MMDC** (DDR controller, phys `0x021b0000`) — QEMU models it *not at all*.
   Check whether the DVFS enables/awaits an MMDC completion IRQ before the poll; if so,
   a tiny MMDC stub that raises it is the clean fix.
4. Clean QEMU hook: on the **CCM_CACRR write** (hw/misc/imx6_ccm.c), pulse an enabled,
   high‑priority interrupt that *will* clear HPPIR — but only after #1 explains why
   prior pending‑injection didn't take.

Kernel addresses: DVFS `FUN_803dc2d4`; HPPIR poll `0x803dc2ac`; per‑iter helper
`0x803dae84`; GICC base ptr var `0x80cc7a9c` (→ `0xf2a00100`). Dual‑core path is
`FUN_803dc4e8` (both CPUs WFI at `0x803de134`).

---

## Part 2 — make the 2D blit complete (so libg2d doesn't crash)

Even after galcore inits, the GPU‑2D stub must **complete blits** so libg2d's blit call
returns instead of jumping through garbage. The command submit is already understood
(from the galcore sessions): 2D MMU regs `0x400‑0x40c = 0x18400000`; FE command‑buffer
reg `0x654 = 0x1827a000` (a `WAIT 200; LINK->self` idle loop); kick reg
`0x658 = 0x00010002`. The stub lives in `imx6_gpu_stub` (`hw/arm/fsl-imx6.c`,
`qemu/patches/02`). Plan:
1. With Part 1 done, boot `XDJ_GPU2D=1` and find where galcore's 2D init next
   stalls/faults (`IMX6_GPU_TRACE=1` logs stub register access).
2. Extend the stub to execute/acknowledge the FE command buffer and **signal blit
   completion** (the status bit / IRQ galcore polls). The blit may be a **no‑op**
   (complete without producing pixels) — the goal is a non‑crashing render, not a
   pixel‑perfect 2D engine. Software‑copying the blit rectangle is a later nicety.
3. Success test: boot to UI, `touch /tmp/subucom_menu` (the slave injects a deck analog
   value — currently crashes); rbp should now render the control change instead of dying.

---

## Tooling & repro

- **Reproduce the DVFS hang:** `XDJ_GPU2D=1 qemu/watch.sh` (or add `XDJ_GPU2D=1` to the
  launch env). Default (no flag) boots fine via the software path.
- **Disassemble the kernel:** Apple `objdump` can't do raw binary. Wrap it:
  `python3 /tmp/mkelf.py build/qemu/vmlinux.bin out.elf 0x80008000` then
  `objdump -d --start-address=0x... out.elf`. Kernel link base **0x80008000**
  (PAGE_OFFSET 0x80000000). Ghidra‑via‑REA times out on the 13 MB kernel — use objdump+gdb.
- **gdb:** launch QEMU with `-S -gdb tcp::<port>`; drive with a raw‑RSP python client
  (halt `\x03`, `g`, `m`, `M`). A VA→phys page‑table walk (swapper_pg_dir VA
  `0x80004000`) lets you read the live GICC/GICD.
- **Guest shell / live memory:** root, empty password, on the `-serial unix:` socket;
  read process memory with `dd if=/proc/<pid>/mem bs=4 skip=$((addr/4)) count=N | hexdump -e '16/1 "%02x " "\n"'`
  (busybox `od -tx1`/`hexdump -C` render as `*`; use `hexdump -e`).
- **Key files:** `qemu/patches/02-fsl-imx6-gpu-stub.patch`,
  `build/qemu-src/qemu-11.1.0/hw/misc/imx6_ccm.c`,
  `build/qemu-src/qemu-11.1.0/hw/arm/fsl-imx6.c`, `docs/emulator-status.md` (features A/D).

## Reality check
This resisted several focused sessions already; Part 1 (the GIC/DVFS wake) is the crux
and is deep QEMU‑internals work. Part 2 is a second, smaller modeling effort. Budget
accordingly and keep every change gated behind `XDJ_GPU2D=1`.
