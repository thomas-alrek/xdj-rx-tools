# Handover: deck plays but produces NO SOUND (silent mix) — emulation gap

**For:** a focused session that only needs to find why a *playing* deck produces
**silence** in the emulator. Everything else about audio is done: the i.MX6
SDMA/SSI/AUDMUX is modelled (patch 11), the host sink is wired
(`-audio coreaudio,timer-period=1451` in `qemu/watch.sh`), the **real-time pacing
is fixed** (see [audio-handover.md](audio-handover.md) §10a), and the deck loads a
track and its playhead/REMAIN counts down in real time. The ONE remaining problem:
the audible output is pure silence.

> ## ⚠️ THIS MUST BE A GAP IN OUR EMULATION — DO NOT HUNT FOR AN rbp BUG
> `rbp` is the **vanilla, unmodified XDJ-RX firmware**, and it **produces sound on the
> real hardware**. The exact same binary runs here. Therefore any divergence — the
> silence — is **by definition** caused by something our QEMU model does differently
> from (or fails to provide versus) the real i.MX6 SoC. Do **not** go looking for a
> logic bug in rbp, and do **not** patch rbp. The task is strictly: find which
> hardware input/behaviour the real SoC gives rbp that our emulation does not, and
> model it QEMU-side. Every "rbp does X / waits for Y" finding below is a *pointer to
> the missing emulated behaviour*, never a firmware defect.

## 0. One-paragraph status
The whole JUCE audio graph is wired and running, but the master mix comes out all
zeros. Proven with live gdb: the mixer callback's **output buffers are 0.0** and the
per-channel **input peak meter is 0.0** — i.e. the decoded deck audio never enters
the mix. Timing, mixer levels, the output/DMA path, the capture inputs, the
callback-enable flag, **and the CPU core count** are all ruled out with evidence
(below). The deck `Player` threads are command-driven and idle (not the continuous
producer); the running continuous reader is `PcmReader:0`. Next step: find why the
continuous producer (`PcmReader` / the source feeding the mixer) emits silence —
most likely a timer/IRQ or decode-pacing input the emulation doesn't deliver.

## 1. What is SOLID (don't re-derive). rbp = `build/extract/rbp`, REA/ghidra.
Audio output path, fully traced (all VAs; REA decompile needs file-offset = VA−0x8000
for procedures it *has* defined, and several of these are indirect-only vtable
targets it did NOT define — use objdump `--start-address` or the gdb reads below):

```
ALSAThread run loop  FUN_00303814   (juce::ALSAThread; the only snd_pcm_writei caller @ thunk 0xe184)
  reads 3 CAPTURE streams (snd_pcm_readi, thunk 0xdc38) -> float input buffers @ (ALSAThread+0x1bc/0x1c0/0x1c4)
  + 1 custom ioctl device (ioctl 0x800c4251, ALSAThread+0xb4) -> +0x1c8/0x1cc   (REC path; null if device absent)
  calls the mixer callback:  (*(*(ALSAThread+0x90))->vtable[+8])(inputs=+0x1bc, nIn=+0x1b8, outputs=+0x8b8? +0xb8, nOut=+0x2bc, nSamples=+0x48)
    -> AudioSourcePlayer cb  0x2f5a50  (forwarding stub: if inner->0x9c>0 call inner->0x94 source getNextAudioBlock; else leave output silent)
       inner->0x9c = 1 (ENABLED), inner->0x94 = MixerAudioSource @ runtime
    -> MixerAudioSource::getNextAudioBlock  FUN_000506b0 : memset outputs to 0, then render registered sources
       (sources are REGISTERED — a live source list of 5 real getNextAudioBlock entries exists)
    -> per-channel strip  FUN_00085bc0 (×2 = decks): pulls sub-source @ (chan+0xb8), then applies smoothed
       trim/EQ/fader gains (chan+0x120/+0x158/+0x1f8, targets +0x118/+0x150/+0x1f0)
       *** channel INPUT peak meter (chan+0x108) == 0.0  => audio is already silent BEFORE gains ***
    -> sub-source / deck reader  0xa4530  (indirect-only; the leaf that emits zero)
  converts the 3 output float buffers -> S24 (clamp) and snd_pcm_writei to master/booth/headphone (ALSAThread+0x9c/0xa0/0xa4)
```
The 6 "running" SDMA channels = 3 capture (mixer inputs) + 3 playback (master/booth/
phones). The QEMU-side peak probe (patch-11 build-tree debug, env `XDJ_AUDIO_DEBUG`)
reads the master SDMA buffer and the per-channel buffers; **all 6 are zero** while a
track plays.

## 2. RULED OUT (with live evidence — don't re-test these)
- **Timing / xrun storm**: FIXED via `timer-period=1451` (audio-handover §10a).
- **Mixer level controls** (master/trim/EQ/crossfader): set TWO ways — debug console
  (`masterv`/`knob`, confirmed working: `button browse` opened the browser) AND
  subucom frame injection — no effect. Also the channel input peak is 0 *before* the
  gains, so gains aren't the gate.
- **Output / DMA / conversion path**: the callback OUTPUT float buffers are 0.0, so
  nothing is lost downstream of rbp — rbp's mix is genuinely silent.
- **Capture inputs**: the 3 capture float buffers are 0.0 (that's mic/line — expected).
- **Callback skipped**: no — `inner->0x9c==1`, the mixer IS invoked; source list is
  populated (decks registered).
- **CPU core count** (owner's dual-core hypothesis): TESTED, negative. `-smp 2` now
  **boots reliably and faster** (the old dual-core boot issue did not recur) with 2
  online cores — this is now the **default** in `qemu/watch.sh` (real-HW config) — but
  the deck `Player` threads are STILL stuck at 0 CPU and the output is STILL silent,
  so core count is NOT the cause. Same binary either way: `rbp_solo` is a **symlink to
  `rbp`**; the init script launches the `rbp_solo` symlink on single-core and `rbp` on
  dual-core, so the silence is binary-identical and core-independent (the whole RE here
  is valid for both). **Tooling note:** process listings show `rbp_solo` on `-smp 1`
  vs `rbp` on `-smp 2` — `pidof rbp rbp_solo` to catch both. (Affinity reads are
  uninformative on `-smp 1`: the kernel clamps all threads to the single online CPU.)

## 3. The thread picture (live, `pidof rbp`, /proc/<tid>/{comm,wchan,stat})
- `Player:0`, `Player:1` = the deck command handlers. Loop `FUN_00077a30`: infinite-
  wait queue pop `FUN_0003febc(q=player+0x58, timeout=0xffffffff)` → `WaitableEvent`
  wait `FUN_002fdd58`. Switch on cmd: 0=load track (`FUN_00076fe4`), 1/2/3=play/seek,
  4=EXIT. They sit at **0 CPU** (Player:0 ~4 ticks, Player:1 0 ticks) because their
  queue is empty — **normal for a command handler**, so they are probably NOT the gate.
  (Open lead: confirm a LOAD/PLAY from the panel actually posts a command here.)
- `PcmReader:0` = **running** (hrtimer_nanosleep poll, ticks advancing). This is the
  continuous PCM reader/decoder and the prime suspect for the silent producer.
- Backtrace of a stuck Player (via gdb stack scan, see §4): `FUN_0003febc` ←
  `FUN_00077a30` ← thread entry `FUN_00300e9c/eb0`. Pure command-wait, nothing exotic.

## 4. THE REMAINING TASK + method
Find why the continuous producer emits silence. Two concrete leads:
1. **`PcmReader:0`**: what does it read and where does it write? Is its output buffer
   non-zero? If it reads from a kernel/driver FIFO or waits on a period IRQ that the
   emulation doesn't deliver, that's the gap (fits the owner's "missing timer IRQ"
   intuition). Find the thread fn (same method as §4 gdb below), decompile, check its
   source (file/decoded-buffer/ioctl/capture) and its wake/pacing.
2. **Leaf deck reader `0xa4530`** (indirect-only; objdump it): it feeds the channel
   strip and emits 0. Walk it to the actual decoded-PCM buffer; check (gdb) whether
   that buffer is non-zero (→ a read/transport-state gate) or zero (→ decode/producer
   not running).
Then whatever hardware input is missing, model it QEMU-side (do NOT patch rbp).

**gdb method that works here** (QEMU gdbstub = `tcp::1234`; it exposes CPU cores, NOT
guest pthreads, so inspect by address/VA, and read guest user memory by VA — all rbp
threads share one address space):
- Helpers written this session in the scratchpad: `gdbx.py` (break + regs), `gdbout.py`
  (read callback I/O float buffers), `gdblist.py` (walk the source list), `gdbgain.py`
  (channel gains/sub-source), `gdbstk.py` (scan a thread stack for rbp `.text` return
  addrs), `gdbread*.py` (pointer chasing). Reuse these.
- Thread user-PC/SP: `/proc/<tid>/stat` field 30 = kstkeip, field 29 = kstkesp.
  `/proc/<tid>/mem` reads EIO on a running thread — read the stack by **VA via the
  gdbstub `m`** instead (shared address space).
- The mixer callback object is at `*(ALSAThread+0x90)`; break at `0x303a98` (the
  `blx` to the callback) to capture r0=callback obj, r5=ALSAThread, r12=callback VA.
- Peak probes: launch with `XDJ_AUDIO_DEBUG=1`; `[sdma-au] … peak=` is the master
  output peak, `[sdma-ch] … bufpeak=` per channel, `[sdma-rate] chN produced=…/s` the
  real-time meter. (Build-tree-only instrumentation in `hw/arm/fsl-imx6.c`, env-gated,
  not in patch 11.)

## 5. How to run / drive (shared helpers)
- Launch + boot + USB + load/play: see [usb-accept-handover.md](usb-accept-handover.md)
  §1 (con4.py console helper, the hub-only boot + `device_add` + `touch /tmp/usb2_insert`,
  the USB-Error clear via the USB2 source key `echo 158 > /tmp/mainkey`), and
  [rotary-encoder-handover.md](rotary-encoder-handover.md). Load a track with PUSH
  `b 5d 10` (single track on the test image), PLAY `b 13 40` (press until REMAIN
  counts). Panel server: `python3 tools/xdj-panel/server.py` → :8765, `POST /ctrl`.
- Audio launch is already in `qemu/watch.sh` (`XDJAUDIO`, default `coreaudio,timer-period=1451`).

## 6. Pointers
- [audio-handover.md](audio-handover.md) §10 — the pacing fix (§10a, DONE) and the
  first silence writeup (§10b); this doc supersedes §10b with the full graph+thread trace.
- [docs/xdj-rx-control-map.md], [docs/subucom-frame-map.md] — mixer control codes/offsets.
- [docs/debug-console-protocol.md] — UDP `127.0.0.1:20000` console (`knob`, `masterv`, …).
