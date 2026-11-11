# Handover: deck plays but produces NO SOUND (silent master mix) — emulation gap

**For:** a focused session continuing ONLY the "a playing deck is silent" problem.
Everything else about audio is done: the i.MX6 SDMA/SSI/AUDMUX is modelled (patch 11),
the host sink is wired (`-audio coreaudio,timer-period=1451`), real-time pacing is fixed
(audio-handover.md §10a), USB mounts, a track loads and plays, and REMAIN counts down in
real time. The ONE remaining problem: the audible output is pure silence.

> ## ⚠️ THIS MUST BE A GAP IN OUR EMULATION — DO NOT HUNT FOR AN rbp BUG
> `rbp` is the **vanilla, unmodified XDJ-RX firmware** and it **produces sound on the real
> hardware**. The exact same binary runs here. Any divergence — the silence — is **by
> definition** something our QEMU model does differently from (or fails to provide versus)
> the real i.MX6 SoC. Do **not** patch rbp. Every "rbp does X / waits for Y" finding is a
> *pointer to the missing emulated behaviour*, never a firmware defect.

---

## 0. One-paragraph status (UPDATED 2026-11-11 — supersedes the old framing below)

**The deck is NOT the silent producer. The previous version of this handover was analysing
the wrong channel.** Live gdb on a playing deck proves `playengine::Player` (DECK1) renders
**real, full-level audio (peak ≈ 0.6)** into its per-channel buffer `0x02abfd08`. Decode,
time-stretch and the deck source all work. The silence is introduced **downstream, in the
playengine mix-down → master stage**: the three master/booth/phones output buffers are only
ever `memset` to 0 — the deck's 0.6 is never summed into them, so the master that reaches
ALSA/SDMA is silence. The leading hypothesis (shared with the owner) is that a mix gain is at
0 (Deck1 trim / channel fader / EQ / crossfader / master level) **or** the deck's per-channel
buffer is never wired into the master router. Those analog mixer controls arrive via the
subucom MCU; if the emulator never delivers their position they may sit at 0. **Blocker:** we
do not yet have reliable control of the analog gain controls, so the "sweep each control and
watch what unmutes" test can't be run yet — wiring/driving those controls is a prerequisite
(see §4).

---

## 1. CORRECTED signal-flow model (verified live — build this on, not the old §1)

rbp has **two** mixing layers. The old handover traced the wrong one.

### 1a. The deck path = the `playengine` mixer (THIS is where the deck lives)

```
mixerengine render  FUN_000506b0
  - memset the 3 OUTPUT buffers (master/booth/phones) to 0   [libc memset, lr=0x50708]
  - lazily build 2 singletons: DAT_0107a1b0 (0x54B, FUN_00065350),
                               DAT_01079cb0 (0x58B, FUN_00046d7c = the CHANNEL mixer)
  - uVar1 = FUN_000635bc(DAT_0107a1b0, n)                     [work/accumulator buffer]
  - FUN_00043a2c(DAT_01079cb0, n, uVar1)   = per-channel mix-down (iterate channel vector)
  - FUN_00065270(DAT_0107a1b0, uVar1, n)   = output routing (3 router vectors) + FUN_00065028
  - FUN_0005c28c(*(param_1+0x220), uVar1)  = position/counter bookkeeping (NOT audio)

FUN_00043a2c: for each ch in [*mixer .. mixer[1]): FUN_0006a360(ch, n, uVar1, end)
FUN_0006a360: (*(*(ch+8)->vtable+8))(ch+8 /*source obj*/, ch+0xc /*dst buf*/, n, end, extra)
              i.e. a vtable dispatch (method +8) on the channel's SOURCE object (ch+8),
              rendering into the channel's own dst buffer (ch+0xc).
```

**The channel vector (live this session), identified by C++ RTTI:**

| ch | class (`N10playengine…`) | source obj | source vtable | render m8 | dst buffer | peak |
|----|--------------------------|-----------|---------------|-----------|------------|------|
| 0  | **`Player` (DECK1)**     | 0x029cfe08 | 0x0037a9f0 | **0x00075eb0** | **0x02abfd08** | **≈0.6 ✓** |
| 1  | `Player` (DECK2)         | 0x02a08718 | 0x0037a9f0 | 0x00075eb0 | 0x02abff10 | 0 (not loaded) |
| 2  | `Microphone`             | …          | 0x0037a738 | 0x0006a7c4 | 0x02ac0118 | 0 |
| 3  | `ExtIn` (line)           | …          | 0x0037a6f8 | 0x0006a4a8 | 0x02ac0320 | 0 |
| 4  | `ExtIn` (line)           | …          | 0x0037a6f8 | 0x0006a4a8 | 0x02ac0528 | 0 |
| 5  | `UsbAudio`               | …          | 0x0037b0f8 | 0x0007d108 | 0x02ac0730 | 0 |
| 6  | `UsbAudio`               | …          | 0x0037b0f8 | 0x0007d108 | 0x02ac0938 | 0 |

- Mixer singleton `DAT_01079cb0` → obj `0x029a6ce8`; channel vector `[0x02a412f0 .. 0x02a4130c)`.
- The per-channel dst buffers are a contiguous array, **stride 0x208**, base `0x02abfd08`.
- The 3 **output** buffers (from `FUN_000506b0` param_4 = r3 at entry, array @ `0x02ab9bf0`):
  master `0x02abcec8`, booth `0x02abd0d0`, phones `0x02abd2d8` — a DIFFERENT region, just
  below the per-channel dsts.
- The `Player` render (m8 `0x75eb0`) runs a **time-stretch/crossfade** (`TimeStretchManager`,
  seen at `0x7ddfc`): `*acc = deck[i]*c + *acc*(1-c)`, where `c` is a coeff at `obj+0x48`
  (`DAT_0107aa40` = 1.0f constant is the "max"). A 0 coeff here drops the deck from the mix.

### 1b. The line-input path = `FUN_00085bc0` (what the OLD handover wrongly called "the deck")

```
FUN_00085bc0  (JUCE "channel strip") — ONLY ONE INSTANCE RUNS, and it is a LINE channel
  chan object this session = 0x02a49ca0, chan+0x14 = idx 0
  input  = DAT_0107acdc[idx].word0  (per-channel "bus"; idx0 -> 0x02ac0320 = ExtIn ch3 dst)
         pulled via FUN_00085a54 (gain state machine) -> FUN_00085914 (gain multiply copy)
  gains  : trim chan+0x120 / eq chan+0x158 / fader chan+0x1f8 (targets +0x118/+0x150/+0x1f0)
  peak   : chan+0x108  (== 0 here, CORRECT: nothing is plugged into line-in)
  sub    : chan+0xb8 = mixerengine::Equalizer (vtable 0x38a860, getNextAudioBlock = 0xa4530),
           which runs 3x dsp::secondOrder_IIRFilter (vtable 0x376cd8)
```

Shared-buffer accessors (both are plain index→pointer helpers):
- `FUN_00080ac0(idx) = &DAT_0107acdc + idx*12` (3 words/entry) — the per-channel **bus**
  the line strip reads. `DAT_0107acdc[0] = {0x02ac0320, 3, 0}`.
- `FUN_000808a4(idx) = &DAT_0107abc4 + idx*8` (2 words/entry) — the **capture staging**
  descriptor; `[idx]+4` = capture float buffer (e.g. `0x02abcaf8`).

### 1c. Capture + output (ALSA) layer

```
ALSAThread  FUN_00303814
  reads the 3 CAPTURE streams, converts S24->float with a NEON loop @ 0x3039c0..0x3039e4
     (vldmia int32 ; vshl.i32 #8 ; vcvt.f32.s32 ; vmul scale ; vstmia -> capture float buf)
  ...calls the mix/output path and writes master/booth/phones to ALSA (-> SDMA).
```

**ALSA topology (`/proc/asound/pcm`, all RUNNING):**
- pcm0 = **MasterOut** (playback) & **Input2** (capture), codecs ak4382(DAC)/ak5358(ADC)
- pcm1 = **HeadphoneOut** (playback) & **MicInput** (capture), ak4387/ak5358
- pcm2 = **BoothOut** (playback) & **Input1** (capture), ak4387/ak5358

The 3 captures are **real external analog inputs** (ADC) and correctly read 0 (nothing
plugged in). They are NOT a deck loopback. MasterOut playback (pcm0p) is healthy
(delay≈128). So the deck audio does NOT travel via ALSA capture — it is pure software in
the playengine, and the loss is before ALSA.

---

## 2. What is SOLID — verified LIVE this session (don't re-derive)

- **The deck decodes and produces audio.** DECK1 `playengine::Player` → buffer `0x02abfd08`,
  peak ≈ 0.6 (measured repeatedly at the mixer render). Decode + time-stretch work.
- **The MP3 is read fully into RAM and played from RAM** (not streamed): rbp fd (this session
  fd 66) → `…/Contents/BITCRUSHR/Play It Hard/BITCRUSHR - Play It Hard.mp3`, 5,910,103 bytes,
  valid ID3+frames, and `/proc/<pid>/fdinfo/<fd>` pos == file size (read to EOF). So the
  source file and USB/vfat read path are fine.
- **The silence is in the mix-down → master.** The 3 playengine output buffers
  (master `0x02abcec8`, booth `0x02abd0d0`, phones `0x02abd2d8`) are **only ever `memset`
  to 0** (write-watchpoint: the only writer is libc `memset`, value 0, called from `0x50708`
  inside `FUN_000506b0`). Nothing ever sums the deck's 0.6 into them. SDMA MasterOut peak = 0.
- **All 6 SDMA channels run at ~44100 frames/s with bufpeak = 0** (3 playback = master/booth/
  phones, 3 capture = Input2/Mic/Input1). The QEMU peak probe reads the BD buffers directly
  (env `XDJ_AUDIO_DEBUG=1`).

## 3. What the OLD handover got WRONG (corrected — do not repeat)

- ❌ "channel strip `FUN_00085bc0` (×2 = decks)" → it is the **line-input** path, and **only
  one** instance runs (a `playengine::ExtIn` channel, chan `0x02a49ca0`, idx 0).
- ❌ "channel input peak (chan+0x108)=0 ⇒ the deck audio is silent before gains" → that peak
  is the **line channel's** input, correctly 0 (no line-in signal). It says nothing about the
  deck.
- ❌ "`0xa4530` = sub-source / deck reader" → `0xa4530` is
  `mixerengine::Equalizer::getNextAudioBlock` on the **line** channel. The deck never calls it.
- ❌ "`PcmReader:0` is the continuous deck decoder / prime suspect" → `PcmReader`
  (`FUN_0003c3bc`) is the **hot-cue buffer cacher** ("ERR HotCueBuffer store",
  `FUN_000393d4`→`FUN_0003b46c`). `FUN_0003b46c` fired **0 times** in 8s of playback; it is
  not the realtime deck decoder.
- ⚠️ "Mixer level controls ruled out (set via console/subucom, no effect)" → re-open this.
  Those tests were made under the wrong model; the mix gains are now the prime suspect (§4).
  The "callback output float buffers are 0.0" observation is consistent with the master mix
  being zero and does NOT contradict the deck producing 0.6 upstream.

What still holds from before: timing/xrun fixed (§10a); CPU core count irrelevant; the deck
"plays" (REMAIN counts) but is silent.

---

## 4. THE REMAINING TASK + how to close it

The deck renders 0.6 into `0x02abfd08`; the master output is 0. Find the step that should
sum/route the deck into the master and show why it emits 0. Two hypotheses:

1. **A mix GAIN is 0** (owner's hypothesis): Deck1 trim, channel fader, EQ, crossfader
   assign, or master level. In the `Player` render the deck is summed with a crossfade coeff
   at `source+0x48`; the channel/master routers in `FUN_00065270`/`FUN_00065028` apply the
   fader/assign. If any is 0 the deck sums in at ×0. These are **analog** controls read via
   the subucom MCU analog section — if the emulator never delivers their position they may
   default to 0 (= the emulation gap).
2. **Buffer-wiring / "shared buffer" gap**: the deck's per-channel buffer `0x02abfd08` is
   never linked into the master router's input list (so the router sums only silent channels),
   because some setup rbp does on real hardware didn't run here.

**Decisive tests (in priority order):**

- **(blocked) Control sweep.** With the analog controls drivable, set Deck1 CH-fader → max,
  then trim, master level, crossfader to Deck1, EQ to center, one at a time, while watching
  `0x02abfd08` (deck, expect ~0.6), the master buffer `0x02abcec8`, and the SDMA peak. The
  control that flips the master non-zero is the gate. **Prerequisite: reliable control of the
  analog mixer gains — not available yet** (owner, 2026-11-11). Wiring/driving the trim/fader/
  EQ/crossfader/master via the subucom analog continuous section is step 0.
- **Find the TRUE master buffer.** It is unconfirmed whether `0x02abcec8` (param_4[0]) is the
  buffer the ALSAThread actually sends to SDMA MasterOut, or a scratch/legacy buffer. Confirm
  by write-watching the SDMA MasterOut BD buffer (or the ALSA mmap region for pcm0p) to catch
  rbp's final S24 store, then walk BACK from that writer. If that true master is non-zero but
  our SDMA reads 0 → the bug is in our SDMA/ALSA-mmap buffer addressing (QEMU side), not a
  gain. If it is zero → confirm which gain/route zeroed it (hypothesis 1 or 2).
- **Read the router gains.** Decompile `FUN_00065270`'s router objects (3 vectors at
  mixer-singleton `DAT_0107a1b0[0..1]/[3..4]/[6..7]`) and `FUN_00065028`; read their per-input
  gain/assign for the deck channel live. A 0 there is the smoking gun.

Then model the missing hardware input QEMU-side (likely: deliver the analog mixer-control
positions so the deck's fader/trim/master aren't stuck at 0), or fix the buffer wiring.
Do NOT patch rbp.

---

## 5. How to run / drive (this session's working setup)

Launch (headless, instrumented) — hub-only so the USB inserts after rbp is polling:

```bash
XDJ_AUDIO_DEBUG=1 build/qemu-src/qemu-11.1.0/build/qemu-system-arm \
  -M sabrelite -smp 2 -m 256 -no-reboot -display none \
  -audio coreaudio,timer-period=1451 \
  -drive if=none,id=usbstick,file=test/usb/rekordbox_new.img,format=raw,snapshot=on \
  -device usb-hub,bus=usb-bus.1,port=1,id=xdjhub \
  -device loader,file=build/qemu/vmlinux.bin,addr=0x10008000,force-raw=on \
  -device loader,file=build/qemu/atags.bin,addr=0x10000100,force-raw=on \
  -device loader,file=build/qemu/tramp.bin,addr=0x10000000,cpu-num=0 \
  -drive if=none,id=xdjsd,file=build/qemu/nand.img,format=raw,snapshot=on \
  -nic user \
  -monitor unix:/tmp/d_mon4.sock,server,nowait \
  -serial unix:/tmp/d_con4.sock,server,nowait -serial null -gdb tcp::1234  > qemu_audio.log 2>&1 &
```

Endpoints: monitor `/tmp/d_mon4.sock`, serial `/tmp/d_con4.sock`, gdb `tcp::1234`,
`XDJ_AUDIO_DEBUG=1` traces `[sdma-ch] … bufpeak=` (per-channel BD peak), `[sdma-au] … peak=`
(master), `[sdma-rate] … frames/s` to the log.

Mount the USB (after `pidof rbp`'s `UsbMountManager` thread is at `poll_schedule_timeout`):
```
# monitor:  device_add usb-storage,bus=usb-bus.1,port=1.1,drive=usbstick,id=xdjusb,removable=on
# host:     touch /tmp/usb2_insert
# press USB2 source:  echo 158 > /tmp/mainkey   (then rm)  — opens the browser
```
NB a **"USB Error. Remove the device."** modal often appears on insert (the cold-plug
rendezvous race, usb-accept-handover §0c), but opening the USB2 browser clears it and the
library loads fine — rbp opens `export.pdb` and `Contents/.../track.mp3`.

Panel web UI (drive load/play/loop): `python3 tools/xdj-panel/server.py --port 8765
--mon /tmp/d_mon4.sock` → http://127.0.0.1:8765 (writes `/tmp/subucom_ctrl` + `/tmp/mainkey`,
screendumps via the monitor socket). Load a track with PUSH, PLAY with `b 13 40`.

Console helper `con4.py` and monitor helper `monc.py`: in this session's scratchpad (recreate
from usb-accept-handover.md §1 if gone). The guest serial is flooded with benign guest-side
`ALSA: Resolve xrun` messages — grep them out; QEMU logs 0 xruns and drains at realtime.

## 6. gdb method + scratchpad probe inventory (reuse these)

QEMU gdbstub = `tcp::1234`; it exposes CPU cores, not pthreads, so inspect by VA (all rbp
threads share one address space). rbp-VA hardware breakpoints (`Z1,<va>,4`) fire in rbp
context; memory reads (`m<addr>,<len>`) and write-watchpoints (`Z2,<addr>,4`) work. Always
remove breakpoints and `c` (continue) before disconnecting, or the next probe can wedge.

Probes written this session (session scratchpad `…/2803848f-…/scratchpad/`):
- `gdbh.py` — generic RSP client (regs / read / bphit / bpread).
- `probe_chan.py` — break `FUN_00085bc0`, map a channel-strip object graph.
- `probe3.py` — leaf render-vs-skip path + decision fields.
- `probe4.py` — dump deck reader objects / count decode-fn hits.
- `probe5.py` — generic **write-watchpoint** on an address (finds producers).
- `probe6.py` — the `DAT_0107acdc` bus buffer + watch its writer.
- `probe7.py` — **enumerate the mixer channel vector + RTTI** (the key tool: found the decks).
- `probe8.py` — break `FUN_000506b0`, read deck buf + the 3 output buffers' peak.
- `probe10.py` — enumerate `FUN_00085bc0` strips + gains/peak.
- `rtti.py` — C++ RTTI name + vtable[+8] for a vptr.

REA notes: Ghidra image base 0x8000 (VA = file-offset + 0x8000). Some leaves (e.g. 0xa4530)
are indirect-only and not Ghidra-defined — disassemble from the ELF with
`objdump -d build/extract/rbp --start-address=… --stop-address=…`. `resolve_containing_procedure`
and `procedure_pseudo_code` take real VAs; a bare value that isn't a defined proc may be
reinterpreted as a file-offset (watch the `selection` field).

## 7. Pointers
- `docs/audio-handover.md` §8/§9/§10 — SDMA/SSI/AUDMUX model, host sink, pacing fix.
- `docs/xdj-rx-control-map.md`, `docs/subucom-frame-map.md` — mixer control codes/offsets
  (needed to drive the analog gains for the §4 sweep test).
- `docs/debug-console-protocol.md` — UDP `127.0.0.1:20000` console (`knob`, `masterv`, …).
- memory `deck-playback-audio-gated.md` — the running RE trail (addresses, corrections).
