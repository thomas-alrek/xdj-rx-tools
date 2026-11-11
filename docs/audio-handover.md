# Audio handover — making decks actually play

**Status: IMPLEMENTED (Options A + B) and verified — 2026-11-11.**
The i.MX6 SDMA engine + SSI/AUDMUX are now modelled in QEMU
([qemu/patches/11-imx6-audio-sdma-ssi.patch](../qemu/patches/11-imx6-audio-sdma-ssi.patch)).
SDMA probes, the ASoC card instantiates, and on the real `hw:ak4387audio` device
ALSA's playback pointer now **advances in real time** where it was previously
frozen at 0 with `imx-pcm-audio hw params failed`.

- **Option A** (time base): a BD-paced model makes the playhead advance (verified
  ~44100 frames/s via `aplay`).
- **Option B** (real sound): a host audio voice (`-audio`/`-audiodev`) pulls the
  PCM at the real audio rate and that drives period completion — so there's
  actual sound **and** hardware-accurate timing (no calibration). Verified with
  `-audio wav`: the captured file is non-silent, stereo/44100, real-time
  duration. Option B is the default clock; Option A's timer is the fallback when
  no audio backend is available.

See **§8 Implemented — results** for the full writeup. The remaining end-to-end
demo (rbp's on-screen `REMAIN` counting down) is gated on the separate
browse/track-load feature, not on the audio path — see §8.

The diagnosis below (the original handover) is kept for context.

**One-line goal:** press PLAY on a loaded deck and have the `REMAIN` time count
down in real time (and, ideally, produce sound). Today the deck latches "play"
but the position is frozen.

---

## 1. The symptom and why it happens

With a track loaded (`Play It Hard`, `REMAIN 02:56.640`), pressing PLAY
(`0x13.6` = subucom `b 13 40`) **does** reach firmware — the deck view/state
changes on the LCD (verified by screendump pixel-diff). But `REMAIN` stays at
`02:56.640`, unchanged seconds later. **The PLAY mapping is correct; playback
just has no time base.**

On a CDJ/XDJ, the deck playhead is **slaved to the audio sample clock** — the
position advances as PCM frames are consumed by the audio hardware. No audio
pipeline → no frames consumed → playhead frozen even while "play" is latched.

This is **not** a panel/mapping bug. See
[subucom-frame-map.md](subucom-frame-map.md) → "PLAY works but playback
position does not advance".

---

## 2. Confirmed facts (and how to re-check each)

| Fact | Evidence / how to reproduce |
|------|------------------------------|
| rbp opens **`hw:ak4387audio,0`, `,1`, `,2`** and drives the full ALSA playback path (`snd_pcm_hw_params`, `set_rate_near`, `set_period_size_near`, `snd_pcm_link`, `snd_pcm_prepare`, `snd_pcm_readi`, `sw_params` start/silence thresholds). | `strings -a build/extract/rbp \| grep -iE 'hw:ak4387\|snd_pcm'` |
| Guest ALSA **enumerates**: card0 `ak4387-audio`; ASoC machine drivers `ak4382_ak5358`/`ak4387_ak5358` map to `imx-ssi.0/1/2`; `/dev/snd/pcmC0D{0,1,2}{p,c}` exist. | guest `cat /proc/asound/cards`, `ls /dev/snd`, `dmesg \| grep -i asoc` |
| **`asoc: platform imx-pcm-audio.0 hw params failed`** at init. This is the ALSA **DMA** layer failing — imx-pcm allocates an SDMA channel in `hw_params`, and there is no SDMA. | guest `dmesg \| grep -i 'hw params'` |
| Every playback substream is **`closed`** — rbp never successfully opens audio. | guest `cat /proc/asound/card0/pcm{0,1,2}p/sub0/status` |
| QEMU models **no audio peripheral**: the only SSI in `hw/arm/fsl-imx6.c` is the *subucom panel SPI slave*. No SSI-audio, no SDMA, no AUDMUX, no SPDIF are created. | `grep -niE 'SSI\|SDMA\|AUDMUX\|create_unimplemented' build/qemu-src/qemu-11.1.0/hw/arm/fsl-imx6.c` — nothing for audio |

---

## 3. The hardware chain (what has to exist)

```
rbp (userspace, ALSA)                    hw:ak4387audio,{0,1,2}
   └─ snd_pcm_* ─────────────────────────────────────────────┐
ALSA / ASoC (kernel)                                          │
   ├─ codec drivers:  ak4387 / ak4382 (DACs),  ak5358 (ADC)   │ <- probe OK today
   ├─ cpu-dai:        imx-ssi.0 / .1 / .2  (SSI audio)         │ <- region NOT modeled
   ├─ routing:        AUDMUX                                   │ <- NOT modeled
   └─ pcm/dma:        imx-pcm-audio  ──requests──► SDMA ch     │ <- NOT modeled  ← hw_params FAILS HERE
i.MX6 SoC (QEMU hw/arm/fsl-imx6.c)
   └─ SDMA raises a period IRQ every N frames ──► ALSA pointer advances ──► rbp playhead advances
```

The **first wall is SDMA**: `snd_pcm_hw_params` fails because imx-pcm can't get a
DMA channel. Fix SDMA and the stream can be configured; then the **SSI + a
period-rate IRQ** are what actually make the pointer (and the playhead) move.

### Addresses / IRQs (already in the QEMU header, just not instantiated)
From `include/hw/arm/fsl-imx6.h`:

| Block | Base | Size | IRQ |
|-------|------|------|-----|
| SSI1  | `0x02028000` | `0x4000` | 46 |
| SSI2  | `0x0202C000` | `0x4000` | 47 |
| SSI3  | `0x02030000` | `0x4000` | 48 |
| SDMA  | `0x020EC000` | `0x4000` | 2  |
| AUDMUX| `0x021D8000` | `0x4000` | —  |
| SPDIF | `0x02004000` | `0x4000` | 52 |

(IRQ numbers are GIC SPIs; wire via the A9 `qdev_get_gpio_in`/`sysbus_connect_irq`
pattern already used for the other i.MX6 peripherals in `fsl_imx6_realize`.)

---

## 4. Implementation options (pick in the dedicated session)

### Option A — minimal "real-time null sink" (recommended first step)
Goal: make the **visual playhead advance** with the least modeling; sound optional.
1. **SDMA stub** at `0x020EC000`: enough register surface that the Linux `imx-sdma`
   driver probes without error and `imx-pcm-audio` can "allocate a channel" and
   start it. The i.MX6 SDMA is a *scripted* engine that loads RAM firmware — the
   stub must satisfy the driver's boot/firmware handshake (it reads back a version,
   loads context, etc.). This is the hard part; study `imx-sdma` in the guest kernel
   to see the minimum it checks.
2. **SSI stub** at `0x02028000`+ : accept the TX config, present a drainable FIFO.
3. **Period timer**: a `QEMUTimer` that, once a stream is running, raises the
   SDMA/SSI period IRQ at `rate/period_size` Hz. Each IRQ makes ALSA advance the
   DMA pointer by one period; imx-pcm discards (null) or forwards the data.
4. **AUDMUX**: dumb RAM-backed register file (no behavior needed).

Result: `hw_params`/`prepare`/`start` succeed, period IRQs fire, ALSA pointer
advances, rbp's playhead moves → `REMAIN` counts down. Silent, but "plays".

### Option B — real sound
As A, but route the SSI/SDMA PCM stream to a QEMU `-audiodev` sink
(`AUD_open_out` + `AUD_write`). Format from the codec config (likely S16/S24_LE,
44.1 kHz, stereo). Only worth it after A proves the clock path.

### Option C — faithful SDMA
Fully model the i.MX6 SDMA (scripts + DMA to memory) and SSI FIFO/clock. Correct,
but a large effort; upstream QEMU has **no** i.MX SDMA or SSI-audio model to
borrow from (confirm with a quick check of `hw/dma` and `hw/audio` in the tree).
Not recommended unless A/B prove insufficient.

> **Shortcut to rule out:** swapping in a generic `snd-dummy`/timer card is a
> dead end — rbp hardcodes `hw:ak4387audio` and the card name comes from the
> board ASoC machine driver. Keep the card identity; replace what's *under* it.

---

## 5. Constraints & gotchas

- **Stock firmware is a hard constraint** — prefer QEMU-side changes over
  firmware/rootfs edits (same rule that governed galcore/USB). The fix belongs in
  `hw/arm/fsl-imx6.c` (+ new device files), landed as a patch in `qemu/patches/`.
- **Three linked streams.** rbp opens device 0/1/2 and calls `snd_pcm_link` — they
  share one clock (deck1 / deck2 / master-or-rec). The playhead likely won't move
  until the linked set configures; make sure all the playback streams it opens can
  start. `snd_pcm_readi` in the string list means it also opens a **capture**
  stream (ak5358 ADC — line/phono in / sampler); a playback-only stub may need to
  not break capture `hw_params`.
- **SDMA firmware handshake** is the main unknown — budget time to read the guest's
  `imx-sdma` probe path (what it writes/reads at boot, how it decides the engine is
  ready). The ROM-script vs RAM-script path matters.
- **Don't re-chase the panel.** PLAY/CUE/SYNC are already decoded and delivered;
  this is purely the audio time base. (Aside noted during diagnosis: the deck-button
  runtime handler is **not** `FUN_00222188` — gdb breakpoints there never fire for
  SYNC/TEMPO/PLAY/CUE despite their visible LCD effects; the real handler is TBD but
  is **not** needed for audio work.)

---

## 6. Tooling to reproduce & verify

Launch (already running pattern; `-smp 1`, gdb on `tcp::1234`, monitor + serial as
unix sockets):

```
build/qemu-src/qemu-11.1.0/build/qemu-system-arm -M sabrelite -smp 1 -m 256 \
  -no-reboot -display none \
  -drive if=none,id=usbstick,file=test/usb/rekordbox_new.img,format=raw,snapshot=on \
  -device usb-hub,bus=usb-bus.1,port=1,id=xdjhub \
  -device loader,file=build/qemu/vmlinux.bin,addr=0x10008000,force-raw=on \
  -device loader,file=build/qemu/atags.bin,addr=0x10000100,force-raw=on \
  -device loader,file=build/qemu/tramp.bin,addr=0x10000000,cpu-num=0 \
  -drive if=none,id=xdjsd,file=build/qemu/nand.img,format=raw,snapshot=on \
  -nic user -monitor unix:/tmp/d_mon4.sock,server,nowait \
  -serial unix:/tmp/d_con4.sock,server,nowait -serial null -gdb tcp::1234
```

Build QEMU: `ninja -C build/qemu-src/qemu-11.1.0/build qemu-system-arm`.
QEMU source tree root: `build/qemu-src/qemu-11.1.0/`; machine file
`hw/arm/fsl-imx6.c`; header `include/hw/arm/fsl-imx6.h`.

- **Screendump** (LCD): monitor socket `/tmp/d_mon4.sock`, send `screendump "/path.ppm"`,
  then `sips -s format png`. Helper pattern in
  `scratchpad/*` (see this session's `now.ppm`/`pl_*.ppm` captures).
- **Guest console**: `scratchpad/con4.py '<cmd>'` over `/tmp/d_con4.sock` (single-client;
  if it refuses, the slot is wedged — relaunch or send `\x03\r` to free it). Use it for
  `dmesg`, `/proc/asound`, `/proc/iomem`, `cat /proc/asound/card0/pcm0p/sub0/status`.
- **gdb**: `scratchpad/gdb.py` (RSP on `:1234`) — `G()`, `setbp`/`cont`/`regs`/`rdmem`.
  Breakpoints work (validated by hitting the MENU handler `FUN_0026ae7c`). Single
  one-shot checks are fine; avoid continuous breakpoints during timing-sensitive sweeps.
- **Press a control**: web panel server (`tools/xdj-panel/server.py`, port 8765) —
  `POST /ctrl` with `b 13 40` (PLAY deck1) / `b 23 40` (PLAY deck2), `POST /release`.
  Or write `/tmp/subucom_ctrl` directly (`b`=one-shot, `B`=hold).

### Definition of done
Load a track, `POST /ctrl b 13 40`, screendump twice ~2 s apart → **`REMAIN`
decreases** (and the playhead/waveform marker moves). Option B adds audible output
on a QEMU `-audiodev`.

---

## 7. Related docs / memory
- [subucom-frame-map.md](subucom-frame-map.md) — PLAY/deck decode + the playback section.
- Memory: `deck-playback-audio-gated`, `emulator-roadmap`, `panel-and-usb-bringup`.
- Prior QEMU-device work to mirror in style: galcore GPU stub and the USB wakeup
  patch (`qemu/patches/`) — same "model just enough of the i.MX6 block" approach.

---

## 8. Implemented — results (2026-11-11)

Option A was implemented as
[qemu/patches/11-imx6-audio-sdma-ssi.patch](../qemu/patches/11-imx6-audio-sdma-ssi.patch)
(all in `hw/arm/fsl-imx6.c`; reuses the SSI/SDMA/AUDMUX addresses already in the
header). The firmware/rootfs are untouched, per the hard constraint.

### What was built
- **SDMA engine model** (`imx-sdma`, mainline dmaengine register map at
  `0x020EC000`). The entire probe+playback handshake collapses to **one**
  mechanism: *a channel is started (write `H_START` bit n) → raise its interrupt
  (`H_INTR` bit n + GIC SPI 2)*.
  - **Channel 0** (SDMA command channel — context/script loads during probe and
    channel config) is **one-shot**: fire its completion interrupt once. `H_RESET`
    reads back 0 (reset done); `H_STATSTOP` reads the running set so the ch0
    readiness poll passes.
  - **Channels ≥1** (audio cyclic DMA) run a **period-interrupt train** at the
    real-time rate until stopped (write to `H_STATSTOP`).
- **Per-period work = hand one BD back to the CPU.** The cyclic ISR
  (`sdma_update_channel_loop`) only advances ALSA's pointer for buffer
  descriptors whose `BD_DONE` bit the *hardware* has cleared. So each period
  interrupt walks the channel's CCB→BD ring (base from `H_C0PTR`), clears
  `BD_DONE` on the current BD in guest DRAM, and wraps on `BD_WRAP` — in lockstep
  with the driver's own `buf_tail`. **This was the non-obvious bit**: without it
  the stream reached `RUNNING` but `hw_ptr` stayed 0 and the app got `-EIO`.
- **Real-time pacing.** Each channel's period interval is derived from its BD:
  `period_ns = BD.count(bytes) / byte_rate`, so the playhead tracks wall-clock
  regardless of the app's chosen period/buffer size. `byte_rate` default
  **318000** (env `XDJ_AUDIO_BYTE_RATE`); `XDJ_AUDIO_DEBUG=1` logs per-channel
  pacing. (The ideal 44.1 kHz·S24_LE·stereo rate is 352800 B/s, but the guest
  clocksource runs ~10% slow vs `QEMU_CLOCK_VIRTUAL`, a constant TCG calibration
  gap that 318000 compensates — the drain rate scales linearly with `byte_rate`.)
- **SSI1/2/3** RAM-backed register files (cpu-dai config scratch; `SISR` reports
  TX FIFO empty) and **AUDMUX** pure-RAM region. No behaviour needed — the time
  base is entirely the SDMA period interrupts.

### Verification (direct, at the ALSA layer)
Boot dmesg now shows `imx-sdma: loaded firmware 1.1` / `initialized`, all three
`asoc: … mapping ok`, card `#0: ak4387-audio` — and **no `hw params failed`**.
(The rootfs ships the SDMA firmware; ch0 context loads complete via the stub.)

Playing to the real device and sampling `/proc/asound/card0/pcm0p/sub0/status`:
```
# head -c 3000000 /dev/zero | aplay -t raw -f S24_LE -c 2 -r 44100 -D hw:ak4387audio,0
t=89.32 hw_ptr: 43008     t=91.33 hw_ptr: 131072
t=90.33 hw_ptr: 86016     t=93.34 hw_ptr: 221184
```
→ **44323 frames/s over 4 s (target 44100, ~0.5% fast)**, state `RUNNING`, clean
exit (no under/overrun). Note the codec only accepts **`S24_LE`** (4-byte slots,
8 bytes/frame); `S16_LE` is rejected.

### Option B — real sound (callback-pull), done

A single S32/stereo/44.1 kHz output voice is opened on the host audio backend
(`audio_get_default_audio_be()`, i.e. whatever `-audio`/`-audiodev` provides). The
backend **pulls** (`sdma_audio_cb`) at the real audio rate; we feed it the
sonified channel's PCM — read from the channel's cyclic BD buffer at a
`(son_idx, son_off)` cursor, rescaled from the codec's `S24_LE` 4-byte slot to
`S32` (`<<8`) — in `≤avail` chunks. The frames the backend accepts become **the
clock**: each chunk advances a frame counter, and every running cyclic channel
completes periods (clears `BD_DONE`, raises its IRQ) as that counter crosses its
per-channel period. So one real audio clock drives the whole linked set, the
playhead is slaved to actual output (hardware-accurate), and the Option A
`byte_rate` calibration is no longer used. Knobs: `XDJ_AUDIO_OUT_CH` selects the
sonified channel (default: first stream started); `XDJ_AUDIO_NOSOUND=1` forces the
timer fallback.

The timer (Option A) remains as the fallback when no voice can be opened, so the
playhead still advances even with audio fully disabled.

**Verification (real sound):** launched with `-audio driver=wav,path=out.wav`,
played noise to `hw:ak4387audio,0`, then inspected `out.wav`:
```
RIFF/WAVE  audfmt=1 channels=2 rate=44100 bits=16
data: 710720 bytes  duration=4.029s   (for ~4 s of playback -> real-time)
peak=32767 (100% FS)  nonzero=352788/355360  -> NON-SILENT
```
i.e. the guest's PCM reached the host sink, at the right rate, in real time.
(QEMU's wav backend doesn't backpatch the RIFF/data size words on `quit`, so a
strict parser may balk — the sample bytes are all present.) For live sound on
macOS use `-audio driver=coreaudio`.

### What's NOT yet demonstrated, and why
rbp holds **no** audio device open at idle — it opens `hw:ak4387audio,{0,1,2}`
(+ capture) and links them only when a deck actually **plays a loaded track**.
Showing the on-screen `REMAIN` tick therefore needs BROWSE → navigate → **LOAD**,
which is the **separate, currently-blocked** browse/track-load path
(`iPodMgr` load-sensitive SIGSEGV; the RX nav/LOAD key mapping is still unfound —
see [xdj-rx-control-map.md](xdj-rx-control-map.md) §"Deck transport & jog"). Since
rbp's playhead is slaved to exactly the ALSA clock proven above, the audio time
base is complete; the end-to-end deck demo is unblocked by finishing browse/load.

### Reproduce / tune
- Rebuild: `ninja -C build/qemu-src/qemu-11.1.0/build qemu-system-arm`.
- Headless verify: launch with `-serial unix:/tmp/a_con.sock,server,nowait`
  `-monitor unix:/tmp/a_mon.sock,server,nowait -display none`, log in root/empty
  (send bytes slowly — the emulated UART drops input at speed), run the `aplay`
  one-liner above.
- Real sound: add `-audio driver=coreaudio` (live, macOS) or
  `-audio driver=wav,path=out.wav` (capture to file; inspect it is non-silent).
  `XDJ_AUDIO_OUT_CH=<n>` pins which SDMA channel is sonified; `XDJ_AUDIO_NOSOUND=1`
  forces the timer fallback.
- Fallback tuning: `XDJ_AUDIO_BYTE_RATE=<n>` fine-tunes the timer-mode drain rate
  without a rebuild (only used when no audio backend is active).

---

## 9. Routing the guest's audio to the host

This is the "how does the sound actually come out of my speakers" path. Nothing
in the guest changes — the hookup is entirely a QEMU launch option.

### The chain

```
rbp / aplay (guest userspace, ALSA, hw:ak4387audio)
  └─ writes PCM into the ALSA DMA ring buffer (guest DRAM), S24_LE stereo 44.1k
i.MX6 SDMA (our model, hw/arm/fsl-imx6.c)
  └─ sdma_audio_cb(): on each backend pull, reads the current period from the
     guest DMA buffer, rescales S24_LE slot -> S32, and audio_be_write()s it
QEMU audio subsystem (SWVoiceOut on the default AudioBackend)
  └─ mixes/resamples to the backend's format
Host audio driver  = the "audiodev"  (coreaudio / pipewire / pa / alsa / sdl /
  └─ speakers, or a .wav file, or /dev/null                         wav / none)
```

Our output voice is opened with `audio_get_default_audio_be()`, so it binds to
**the default audiodev** — the one set by the `-audio` option (NOT a named
`-audiodev`, which only devices referencing it by `audiodev=<id>` would use).

### Pick a host backend with `-audio driver=<drv>,<opts>`

| Goal | Flag |
|------|------|
| **Live sound, macOS** | `-audio driver=coreaudio` |
| **Live sound, Linux** | `-audio driver=pipewire` (or `pa`, or `alsa`) |
| **Portable live sound** | `-audio driver=sdl` |
| **Capture to a file** (headless/CI, no speakers) | `-audio driver=wav,path=out.wav` |
| **Silent but still clocks the playhead** | `-audio driver=none` (the implicit default if `-audio` is omitted) |

Add exactly one such flag to the launch line (e.g. in `qemu/watch.sh`, which
currently passes none, so audio falls through to the `none`/default backend —
playhead advances, no sound). Example live launch:

```
qemu-system-arm -M sabrelite ... -audio driver=coreaudio
```

Nothing else is required: the SDMA model lazily opens the voice the first time a
deck starts playing and activates it; it idles the voice when playback stops.

### Latency / tuning
- Buffer/period of the *host* side are backend options, e.g.
  `-audio driver=coreaudio,out.buffer-length=20000` (µs) to trade latency for
  robustness. The guest-side period is fixed by rbp's ALSA config and is
  independent of this.
- `-audio driver=<drv>,out.frequency=48000` forces the backend rate; our voice is
  44100/S32/stereo and QEMU resamples as needed.

### Gotchas
- **`-audio` vs `-audiodev`**: use `-audio driver=...`. A bare
  `-audiodev coreaudio,id=a` is *not* the default backend and our voice won't find
  it (it would need a device `audiodev=a` property, which the SoC doesn't expose).
- **`wav` file size fields**: QEMU's wav backend doesn't backpatch the RIFF/`data`
  length words on `quit`, so strict WAV parsers may reject the file even though the
  samples are all present; read to EOF or fix the two length words if a player
  refuses it.
- **No sound but playhead moves**: that's the `none`/default backend (no `-audio`),
  or `XDJ_AUDIO_NOSOUND=1`, or the host driver failed to open (QEMU prints an audio
  warning and falls back) — pass an explicit working `-audio driver=`.
- **Which stream you hear**: rbp opens deck1/deck2/master as separate SDMA
  channels; we sonify one (`XDJ_AUDIO_OUT_CH`, default first-started). If the wrong
  one is picked you may hear a single deck instead of the master mix — set
  `XDJ_AUDIO_OUT_CH` to the master channel once identified.

## 10. Real-time pacing fix + the remaining silence (2026-11-11, session 2)

End-to-end testing on `-audio coreaudio` revealed TWO separate problems. The first
is **fixed**; the second is isolated and handed off.

### 10a. FIXED — the stream starved (very slow / glitchy / silent): `timer-period`
**Symptom:** with a track playing, the on-screen playhead advanced but audio was
"very slow", then dropped out; the guest console flooded with `ALSA … Resolve xrun`
(~90/s).
**Root cause (measured, not guessed):** the guest CPU was **86% idle**, so it was
*not* CPU-bound. rbp's playback ring is tiny (`period_size=64`, `buffer_size=128`
frames ⇒ **2.9 ms**). QEMU's audio **mixer-timer** polls the output voice (our
`sdma_audio_cb`) on a period that **defaults to 10 ms (100 Hz)** — far too coarse to
wake rbp's audio thread at the ~689 Hz its 64-frame period needs. So rbp refilled at
only **~27% of real time** (11.9k vs 44.1k frames/s, identical idle-vs-playing and
on both the `wav` and `coreaudio` backends → not CPU, not a backend artifact), and
the real-time sink starved.
**Fix (config only, no code/patch change):** set the audio mixer timer period to the
guest's period rate:
```
-audio coreaudio,timer-period=1451      # 1451 µs ≈ 689 Hz = 44100/64
```
**Result:** production **11.9k → 44.2k frames/s (real time)**, xruns **~90/s → 0/s**,
playhead real-time. Now baked into `qemu/watch.sh` (`XDJAUDIO`/`timer-period`);
`coreaudio` on macOS, `pa` elsewhere, `XDJAUDIO=none` to disable.
- `timer-period` is a generic `-audio`/`-audiodev` param (not backend-specific).
  `out.period-length`/`out.buffer-length` are **rejected by coreaudio** (callback
  backend) — use `timer-period`.
- Diagnostics used (all `XDJ_AUDIO_DEBUG=1`, env-gated, added to the *build tree*
  only, not patch 11): per-chunk `peak=`, per-channel `bufpeak=`, and a
  `[sdma-rate] chN produced=… frames/s` meter in `sdma_consume_period`. Re-add if
  you need to re-measure; they're inert without the env var.

### 10b. OPEN (handed off) — decoded audio never enters the mix (true silence)
After the pacing fix the stream is clean (0 xruns) but **every one of the 6 SDMA
output channels still carries pure silence** (`bufpeak=0`) while a track plays. The
mix output rbp writes to ALSA is zero.

**Ruled out — it is NOT the mixer level controls.** The physical conditions for audio
(per the device owner: **master level max, deck TRIM + HI/MID/LOW at half,
crossfader centered**) were set TWO independent ways with no effect:
1. **Debug console** (confirmed working — `button browse` opened the browser):
   `masterv 1.0`, `knob master_att 0`, `knob trim/eqh/eqm/eql 512 {0,1}`,
   `knob xfader 512`, `knob fader 1023 {0,1}`, plus `mixeron`/`route`/`play`.
2. **Direct subucom frame injection** of the analog offsets (below).

So the gate is **upstream of the mixer gains** — in rbp's player→`MixerEngine`
input feed / decode path. rbp *does* open the MP3 fd and advance position, but the
decoded PCM is not reaching the `MixerEngine` input (or the input channel gain /
routing is zero by a path other than the knobs tried).

**Mixer control map recovered this session (for the follow-up):**
- Debug console: **UDP `127.0.0.1:20000`**, text lines; `knob <type> value(0-1023)`,
  `masterv <float>`; see [debug-console-protocol.md](debug-console-protocol.md).
  (`master_att` is *attenuation*: 0 = 0 dB / full, 1023 = full cut.)
- Hardware-test code→label table @ rbp `0x476620`+ (strings @ `0x423450`+):
  `0x4405`=MASTER LEVEL, `0x600f`=CROSS FADER, `0x440c`=BOOTH, `0x4402`=HP MIXING,
  `0x4403`=HP LEVEL, `0x448b`=LEVEL DEPTH; per-channel `0x5007`=TRIM, `0x5008`=EQ HI,
  `0x5009`=EQ MID, `0x500a`=EQ LOW, `0x5011`=CH FADER, `0x508b`=COLOR.
- Subucom analog inject offsets (16-bit LE, `A <off> <val16>`, value/1023), from
  [subucom-frame-map.md](subucom-frame-map.md): EQ HI 0x24/0x34, TRIM 0x26/0x36,
  EQ MID 0x28/0x38, EQ LOW 0x2a/0x3a, COLOR 0x2c/0x3c; BOOTH 0x48, **MASTER 0x4a**,
  LEVEL DEPTH 0x4c, **CROSSFADER 0x4e**, HP LEVEL 0x50, HP MIXING 0x52.
- rbp side: `SubRxFormat::tMixerData()` parses the RX frame → mixer values;
  `mixerengine::{MasterOut,OutputChannel,InputChannel,UsbChannel}` + `DjEngineIF::
  setMasterVolume/setCrossFader`. The owner notes rbp likely holds the loaded track
  **fully in memory** (so the MP3 fd offset won't advance during playback).

**Next step:** find where the player's decoded PCM is (or isn't) handed to the
`MixerEngine` input channel — e.g. break on the master-out mix write and walk back to
the input-channel buffer; confirm whether the decoded buffer is non-zero (→ routing/
input-gain gate) or zero (→ decode/feed not running under emulation).
