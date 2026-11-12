---
name: qemu-imx6-patches
description: Understand and extend the XDJ-RX QEMU patch set - the i.MX6/SABRE-SD machine changes that let stock firmware boot to the live UI (GPU/IPU stubs, SD boot, USB bring-up, main-ucom GPIO keys, sub-ucom panel inject harness, audio SDMA/SSI). Use when modifying qemu-side emulation, adding a control-injection hook, debugging a boot/USB/audio gap, or building the patched qemu-system-arm.
---

# XDJ-RX QEMU patch architecture

All device behavior lives **qemu-side**; the firmware (kernel + `rbp`) is loaded byte-for-byte
and **never patched**. Base: **QEMU 11.1.0**, machine **`-M sabrelite`** (i.MX6Q, Cortex-A9),
but booted as the real board (**SABRE-SD, MACH_TYPE 3980, `ATAG_REVISION=0x500`**) via a loader
trampoline + hand-built ATAGs (`qemu_prepare.py` builds `tramp.bin`/`atags.bin`/`vmlinux.bin`).

## Build
- `qemu/build-qemu.sh` applies `qemu/patches/*.patch` to the QEMU 11.1.0 source and builds into
  `build/qemu-src/qemu-11.1.0/build/qemu-system-arm`.
- **Incremental rebuild** after editing a source file (e.g. `hw/arm/fsl-imx6.c`):
  `ninja -C build/qemu-src/qemu-11.1.0/build qemu-system-arm` (~30 s). The build tree is
  git-ignored — edit the **patch** for the record *and* the build-tree source to test, or
  regenerate the patch from the diff.

## The patches (`qemu/patches/`)
| # | file | what it does |
|---|---|---|
| 01 | `imx6_src-gpu-reset` | SRC GPU reset bits self-clear so galcore's `ResetGPU` poll completes |
| 02 | `fsl-imx6-gpu-stub` | the big machine patch: **GPU register stub** (galcore probes then fails gracefully `-22`), **IPU1 vsync interrupt stub** (BG ch23 EOF/NFACK → fast, tear-free display), **fb scanout device** (mirrors `/dev/fb0` @ `0x18000000`, 800×480), and the base **sub-ucom inject harness** (reads `/tmp/subucom_ctrl`, rewrites the 100-byte frame, recomputes CRC16/0x8408) |
| 03 | `sabrelite-sabresd-sd` | SABRE-SD board: SD rootfs on **uSDHC3** (`-drive if=none,id=xdjsd`), console on `ttymxc0` |
| 04 | `imx6-usb-detect-gpio` | `psr-reset` for USB-detect pad-status pins (idle HIGH; avoids false "USB present") |
| 05 | `imx6-dvfs-gpt-kick` | keeps the GPT/clock path moving (DVFS) so boot doesn't stall |
| 06 | `imx6-usb-insert-hook` | detect-GPIO insert hooks: `touch /tmp/usb_insert` / `/tmp/usb2_insert` asserts a slot's insert line |
| 07 | `imx6-multifield-holds` | up to MAXHOLD **persistent** A/B inject holds parsed from all ctrl-file lines; supports complementary two-field faders (tempo = `0x04` neg + `0x0a` pos) |
| 08 | `usb-highspeed-hub` | a **USB-2.0** hub (stock `usb-hub` is USB-1.1 and HOST1 rejects it) |
| 09 | `imx6-mainucom-keys` | drives i.MX6 GPIO lines rbp reads via `/dev/gpiodrv` (top-panel source/DECK keys); `g_xdj_gpio[8]` + `/tmp/mainkey` (momentary) + `psr-reset` idle levels |
| 10 | `imx6-usbnc-wakeup` | USBNC OWIR wakeup so a **post-boot** USB insert resumes a suspended HOST1 and enumerates |
| 11 | `imx6-audio-sdma-ssi` | minimal **SDMA** engine + RAM-backed **SSI1/2/3 + AUDMUX** so imx-sdma/ASoC/ALSA run; a host **sonifier** pinned to SDMA **ch1=MasterOut** (`XDJ_AUDIO_OUT_CH`/`XDJ_AUDIO_DEBUG`) emits real audio to the host |

## Key machine file
`build/qemu-src/qemu-11.1.0/hw/arm/fsl-imx6.c` holds most hooks: the per-frame sub-ucom hook
`xdj_subucom_frame_start` (inject harness, `/tmp/mainkey` key drive, USB insert, any new control
hook), the GPIO bank capture `g_xdj_gpio[]`, and the `psr-reset` setup per bank at realize.

## Extending it (adding a control hook)
- A **sub-ucom** control (fader/button/knob/rotary) needs **no new patch** — inject it through
  the existing `/tmp/subucom_ctrl` harness (see the map-subucom-* skills).
- A **GPIO** control: drive the i.MX6 line via `g_xdj_gpio[bank]` + `qemu_set_irq`; a momentary
  key via `/tmp/mainkey`; a held level via `psr-reset` (reset default) + a file hook. **A pin
  whose GDIR is output can't be driven as an input** (PSR follows DR) — check `GDIR` first.
- Writing rbp **VA** memory from a hook works only when the CPU is in rbp's context
  (`cpu_memory_rw_debug`); prefer driving a real input over poking memory.

## Gotchas
- Don't drive IPU **alpha/FG** channel (bit 27) — panics the kernel; only BG ch23.
- The sub-ucom inject harness has a ~150-inject quiescence limit; continuous streams (encoder)
  are hard to sustain.
- Keep the **firmware untouched** — if a fix seems to need an rbp/kernel edit, find the qemu-side
  cause first (that's the standing constraint; custom firmware is tested by booting it).

## References
`QEMU.md`, `qemu/build-qemu.sh`, `qemu/watch.sh`, `qemu/build-nand.sh`, `qemu_prepare.py`,
`docs/usb-*.md`, `docs/audio-handover.md`. Skills: [run-xdj-emulator], [rbp-binary-internals],
[map-gpio-control], [map-subucom-button].
