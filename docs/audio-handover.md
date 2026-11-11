# Audio handover — making decks actually play

**Status:** not started. This is the last unimplemented feature on the
[emulator roadmap](../). Everything below is the result of a diagnosis session
(2026-11-11); no audio code has been written yet.

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
