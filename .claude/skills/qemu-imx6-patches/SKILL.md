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
- `qemu/build-qemu.sh` applies `qemu/patches/*.patch` (recorded in `<src>/.xdj-patches`, so a re-run on an existing tree is idempotent — the old reverse-dry-run check re-applied patch 02 once later patches changed its context) to the QEMU 11.1.0 source and builds into
  `build/qemu-src/qemu-11.1.0/build/qemu-system-arm`.
- **Incremental rebuild** after editing a source file (e.g. `hw/arm/fsl-imx6.c`):
  `ninja -C build/qemu-src/qemu-11.1.0/build qemu-system-arm` (~30 s). The build tree is
  git-ignored — edit the **patch** for the record *and* the build-tree source to test, or
  regenerate the patch from the diff (see **Regenerating & verifying a patch**).

## Regenerating & verifying a patch (don't hand-edit hunk counts)
The only `patch` on this Mac is **`/usr/bin/patch` (Apple 2.0)**, which `build-qemu.sh`
uses — it is **strict**: a wrong `@@ -a,b +c,d @@` line count is a hard `malformed patch
at line N` error (not a fuzzy apply), which **silently breaks every later patch and the
from-scratch build**. So never hand-edit hunks or their counts — regenerate with `diff -u`
(it computes counts correctly). To regenerate patch *N* from the live build tree:
- **after** = the live tree with patch *N* applied. If later patches touch the same file,
  recover this by **reverse-applying the later patches** to the live source
  (`patch -R <file> < patches/M.patch` for M>N) — not by hand.
- **before** = pristine QEMU + patches `01..N-1`: `tar xf build/qemu-src/qemu-11.1.0.tar.xz`
  to a temp dir, apply `01..N-1`, grab the file.
- body = `diff -u before after` with the paths rewritten to `a/…` `b/…`; prepend the patch's
  existing header prose (everything before the first `--- a/`).

**Verify before committing (all three):**
1. **Round-trip:** `before + newpatch` == `after` (byte-identical).
2. **Full series:** fresh tarball + `for p in qemu/patches/*.patch; do /usr/bin/patch -p1 <$p; done`
   applies clean, and the result == the live build tree byte-for-byte (**drift check** — the
   git-ignored build tree can diverge from committed patches; e.g. patch 11's `XDJ_AUDIO_DEBUG`
   meters were once live-only).
3. **From scratch:** that fresh tree `configure`s + `ninja qemu-system-arm` with no errors.
Regenerate after *any* build-tree edit so the committed patches reproduce the running binary.

## The patches (`qemu/patches/`)
| # | file | what it does |
|---|---|---|
| 01 | `imx6_src-gpu-reset` | SRC GPU reset bits self-clear so galcore's `ResetGPU` poll completes |
| 02 | `fsl-imx6-gpu-stub` | the big machine patch: **GPU register stub** (galcore probes then fails gracefully `-22`), **IPU1 vsync interrupt stub** (BG ch23 EOF/NFACK → fast, tear-free display), **fb scanout device** (mirrors `/dev/fb0` @ `0x18000000`, 800×480), and the base **sub-ucom inject harness** (reads `/tmp/subucom_ctrl`, rewrites the 100-byte frame, recomputes CRC16/0x8408) |
| 03 | `sabrelite-sabresd-sd` | SABRE-SD board: SD rootfs on **uSDHC3** (`-drive if=none,id=xdjsd`), console on `ttymxc0` |
| 04 | `imx6-usb-detect-gpio` | `psr-reset` for USB-detect pad-status pins (idle HIGH; avoids false "USB present") |
| 05 | `imx6-dvfs-gpt-kick` | keeps the GPT/clock path moving (DVFS) so boot doesn't stall |
| 06 | `imx6-usb-insert-hook` | USB **fault-flag** injection: `touch /tmp/usb_insert` / `/tmp/usb2_insert` drives GPIO4_IO30 / GPIO7_IO12 LOW = rbp's active-low USB fault → "USB Error". NOT an insert line (misnamed); normal inserts = `device_add` only |
| 07 | `imx6-multifield-holds` | up to MAXHOLD **persistent** A/B inject holds parsed from all ctrl lines (complementary two-field faders, tempo = `0x04` neg + `0x0a` pos); **Phase 1 control socket**: reads the snapshot from chardev `id=xdjctrl` (NUL-framed) when present, else file `XDJ_CTRL_PATH`/`/tmp/subucom_ctrl`. Shared `xdj_parse_ctrl` + chardev receive (also used by 09); BQL-serialized, no locks |
| 08 | `usb-highspeed-hub` | a **USB-2.0** hub (stock `usb-hub` is USB-1.1 and HOST1 rejects it) |
| 09 | `imx6-mainucom-keys` | drives i.MX6 GPIO lines rbp reads via `/dev/gpiodrv` (top-panel source/DECK keys); `g_xdj_gpio[8]` + `psr-reset` idle levels; key int from chardev `id=xdjkey` (Phase 1) when present, else `XDJ_KEY_PATH`/`/tmp/mainkey` (momentary) |
| 10 | `imx6-usbnc-wakeup` | USBNC OWIR wakeup so a **post-boot** USB insert resumes a suspended HOST1 and enumerates |
| 11 | `imx6-audio-sdma-ssi` | minimal **SDMA** engine + RAM-backed **SSI1/2/3 + AUDMUX** so imx-sdma/ASoC/ALSA run; a host **sonifier** pinned to SDMA **ch1=MasterOut** (`XDJ_AUDIO_OUT_CH`/`XDJ_AUDIO_DEBUG` peak/rate meters) emits real audio to the host |
| 12 | `imx6-otg-vbus-valid` | chipidea **OTGSC** (0x1A4) reports a settled host VBUS/session so the OTG driver skips its multi-second VBUS wait (`wait otg vbus change timeout!`) at the splash |
| 13 | `imx6-subucom-autolevel` | **auto-levels** the mixer on boot (both ch → DECK + unity-ish gains) so a loaded deck is audible with no manual inject; MERGES per-control with the ctrl snapshot (chardev `xdjctrl` or `/tmp/subucom_ctrl`; panel still overrides); `XDJ_AUTOLEVEL=0`, `XDJ_AUTOLEVEL_DELAY_MS` |
| 14 | `imx6-subucom-frame-hook` | calls `xdj_subucom_frame_start()` from the ECSPI burst start (`imx_spi_flush_txfifo`) — **without it no panel control reaches rbp** (all-zero frames); MAXHOLD 64; auto-level both channels |
| 15 | `imx6-subucom-led-backchannel` | captures rbp's **MOSI `SubTxData`** (LEDs / meters / jog ring), CRC-gates the 20-byte payload, publishes changes as `T <hex>` up `xdjctrl` (socket) or `/tmp/subucom_tx` (`XDJ_TX_PATH`); `XDJ_TX_LOG=1`. Map: `docs/subucom-led-map.md` |
| 16 | `imx6-audio-paced-sdma` | **fixes audio stutter**: SDMA periods were completed in bursts from the host audio callback (~2 per call = rbp's whole 2×64-frame ALSA ring → constant `ALSA: Resolve xrun`). A **pacer thread** (own thread: macOS main-loop timers can't tick every 1.45 ms) completes one period per channel per tick, copies the master period into a PCM FIFO the host callback drains, and trims its rate ±8% by FIFO level (locks the playhead to the host audio clock; late wake-ups resync, never burst). `XDJ_AUDIO_LATENCY_MS` (FIFO target, default 20), `XDJ_AUDIO_STATS=1` (5 s stats). |
| 17 | `imx6-source-key-leds` | rekordbox/MIDI/USB1/USB2 key LEDs = rbp GPIO outputs **162–165** (GPIO6_IO02..05, active-high, one-hot). Connects those output lines (`qdev_connect_gpio_out` → `xdj_srcled_set`), mirrors a bitmask and appends it to the patch-15 LED line as a 21st byte (`T <40 hex> <2 hex>`), re-publishing on each change. Panel byte `0x14`. |
| 18 | `imx6-auth-chip-i2c4` | **fixes E-7206 AUTH CHIP ERROR**: adds the missing **I2C4** controller (`0x021F8000`, SPI 35 = guest `/dev/i2c-3`, mapped over the catch-all) + `xdj-mfi-cp` I2C slave at `0x10` answering the MFi coprocessor identity registers (reg0 `0x05`, reg1 `0x01`, protocol 2.0, device ID `0x200`; no signing/cert). Also puts the **sub-ucom version word** in frame bytes 2–3 (`0x0035` = "0.35", `XDJ_SUBUCOM_VER`). |
| 19 | `imx6-update-mode-strap` | `XDJ_BOOT_UPDATE=1` sets **GPIO3_IO30** (sub-ucom update request) HIGH via `psr-reset` → initramfs USB firmware-update mode (D001 screen). |

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
