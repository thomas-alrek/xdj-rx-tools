---
name: run-xdj-emulator
description: Launch and drive the XDJ-RX firmware emulator - boot the patched QEMU to the live rekordbox UI, capture the screen, mount a rekordbox USB, inject sub-ucom panel controls, read audio routing over the gdbstub, hear deck audio on the host, measure audio stutter, and boot the service modes (test mode, firmware-update mode). Use when you need to run/boot the emulator, reproduce a UI/audio/control behavior, or set up a live test bed.
---

# Running the XDJ-RX emulator

The patched QEMU boots stock firmware to the **live interactive rekordbox UI** (800×480),
with working display, USB mount, sub-ucom panel injection, and real deck audio to the host.

## Launchers
- **`qemu/watch.sh`** — canonical boot: window + serial console on stdio. `XDJDISPLAY=none`
  for headless. Run from repo root.
- **`./run.sh`** — from-scratch: build patched QEMU, decrypt firmware, prep blobs, build SD
  image, boot. Use once / after a clean checkout.
- **Debug launcher** `tools/xdj-panel/debug/relaunch.sh` — single-core, `-display cocoa`,
  **gdbstub `tcp::1234`**, monitor `unix:/tmp/d_mon4.sock`, serial `unix:/tmp/d_con4.sock`,
  `-audio coreaudio`, hub-only USB, `XDJ_AUDIO_DEBUG=1`. This is the live-RE/inject bed.

Launch detached and poll for the home screen (boot is single-core TCG, **~60–90 s**):
```bash
XDJ_AUDIO_DEBUG=1 nohup ./tools/xdj-panel/debug/relaunch.sh > /tmp/xdj_qemu.log 2>&1 &
```
**Always launch as the user, never sudo.** (A failed `sudo`/ptrace attach once *killed* qemu.)

## Control sockets (debug launcher)
- Monitor (HMP): `tools/xdj-panel/debug/mon.py "<cmd>"` → `/tmp/d_mon4.sock`
  (`screendump`, `device_add`, `xp/1wx <addr>`, `info usb`, `cont`).
- Serial console: `tools/xdj-panel/debug/ser.py "<shell cmd>"` → `/tmp/d_con4.sock` (login root).
  Everything the console prints is also logged to **`/tmp/xdj_console.log`** (chardev `logfile=`;
  the panel's View > System Log tails it) — read it instead of competing for the socket.
- gdbstub: `tools/xdj-panel/debug/rsp.py` (raw RSP); `tables.py` dumps the audio routing tables.

## See the screen
```bash
python3 tools/xdj-panel/debug/mon.py "screendump /tmp/s.ppm"
sips -s format png /tmp/s.ppm --out /tmp/s.png   # then Read /tmp/s.png
```
Home screen ≈ DECK1/DECK2 "Not Loaded"; splash = Pioneer DJ/rekordbox logo (still booting).

## Mount a rekordbox USB (full recipe: `docs/usb-mount-howto.md`)
Boot **hub-only** (no `-device usb-storage` at boot — the debug launcher already does this),
wait for the home screen, then:
```
monitor:  device_add usb-storage,bus=usb-bus.1,port=1.1,drive=usbstick,id=xdjusb,removable=on
```
Mounts `/media/usb2/sda1`; USB2 browser shows the library, no modal. Cold-plugging at boot →
"USB Error"; always insert post-boot. **That `device_add` is the whole insert** (patch 10 wakes
HOST1 from the attach).

**Never `touch /tmp/usb2_insert` (or `/tmp/usb_insert`) to insert.** Patch 06's files drive
GPIO7_IO12 / GPIO4_IO30, which rbp reads as **active-low USB fault flags** (gpiodrv ids
`0xcc`/`0x7e`, `UsbStorageManager` listener `0x250020`): LOW on a mounted slot →
`USB Error. Remove the device.` ~20 s (guest) later; `rm` the file → cleared in ~0.5 s.
That was the long-standing "USB Error on a correct insert" (fixed 2026-11-15; the panel
server and xdj-host no longer create the file). Use the files only to inject a USB fault.
**Do NOT retry by `device_del`** —
that tears down the `-drive if=none,id=usbstick` backend (later `device_add`s then
fail `can't find value 'usbstick'`); if a mount won't take, **reboot and insert
once** instead.

## Inject sub-ucom panel controls
Write one spec per line to **`/tmp/subucom_ctrl`** (harness reads it per frame, recomputes CRC):
`A <off> <val16>` = hold 16-bit analog (fader/knob); `a` one-shot; `B <off> <mask>` = hold a
button bitmask; `b` one-shot. Use `tools/xdj-panel/debug/inj.py pulse` to one-shot **without**
clearing the analog-gain holds (a bare write replaces all holds and zeroes the mix). Top-panel
source/DECK keys are GPIO → `/tmp/mainkey` (number=press, remove=release).
Examples: source switch DECK = `B 33 80` + `B 43 80`; gains = `A 2c 3ff`(fader) `A 4a 3ff`(master)
`A 4e 200`(xfader) `A 24/2a/28 200`(EQ) `A 26 3ff`(trim).

**One-shot gotcha:** a `b`/`a` line left in the file *together with holds* re-fires every ~30
frames (only a holds-free file is removed after firing) — rewrite the holds alone ~0.5 s later
(`inj.py pulse` does), else a toggle button flips on/off repeatedly.

## Read panel LEDs / meters
QEMU patch 15 mirrors rbp's LED frame (`SubTxData`): file mode → `cat /tmp/subucom_tx`
(`T <40 hex>`, updated on change); socket mode → the panel server's `GET /leds?once=1`.
`XDJ_TX_LOG=1` logs every changed burst (`SUBUCOM-TX …`) to the qemu stderr. Bit map:
`docs/subucom-led-map.md`; probe new ones with `tools/xdj-panel/debug/ledprobe.py`.

## Hear deck audio
Audio is modelled (SDMA+SSI+AUDMUX, patch 11); the sonifier is pinned to **SDMA ch1 =
MasterOut** (`XDJ_AUDIO_OUT_CH` overrides, `XDJ_AUDIO_DEBUG=1` prints per-channel peaks). For a
loaded deck to be audible you need **routing = DECK** (source switch `B 33 80`/`B 43 80`) **and**
gains up (on-air gate `FUN_00065028`). Load a track via the USB2 browser (track-load works).
**Auto-level (patch 13) now sets routing=DECK + gains automatically ~4 s after rbp starts** — so
a loaded deck just plays. Idle routing is therefore **DECK by default** now (not LINE); disable
with `XDJ_AUTOLEVEL=0` (e.g. to test LINE/PHONO), tune the settle with `XDJ_AUTOLEVEL_DELAY_MS`.

### Audio STUTTERS? (measure, don't guess)
Dropouts are almost always **guest ALSA xruns**, not host underruns: rbp runs a 2×64-frame ring
(2.9 ms), so the guest must see one period complete every 1.45 ms, evenly. Patch 16's pacer
thread does that. To measure: count `ALSA: Resolve xrun` on the guest console (read
`/tmp/d_con4.sock` for 30 s; ~0 is healthy, the pre-patch-16 burst model gave ~12/s), and boot
with `XDJ_AUDIO_STATS=1` for a 5 s line: `underruns`/`gap` (host ran dry), `minfifo`, `guest_late`
(rbp didn't re-arm a period in time = guest CPU too slow), `pace=689/s` (must equal 44100/64),
`resync` (pacer woke >1 period late). More headroom: `XDJ_AUDIO_LATENCY_MS=40` (adds latency).

### Deck loaded but SILENT? (debug recipe)
1. **Stray `/tmp/subucom_ctrl`** is the #1 cause — any hold there (even an unrelated leftover
   like the web panel's browse `A 54 0`) overrides auto-level per-control; if it sets routing or a
   gain it can mute the deck. `rm /tmp/subucom_ctrl` and auto-level resumes. (Merge model means an
   *unrelated* hold no longer silences it — but a stale build predating patch 13's merge can.)
2. **`XDJ_AUDIO_DEBUG=1` peaks** (in the qemu log): master `[sdma-au] peak=0` = genuine internal
   silence (routing/gain/gate), while `peak`≫0 = audio IS produced → the gap is host output.
   All `[sdma-ch] bufpeak=0` incl. deck channels = no PCM reaching the engine (not playing, or the
   decode→mixer path). ~44100 frames/s on every channel = the clock is fine.
3. **`tables.py` routing** — `ch0 routed_src` must be `0` (DECK), not `3` (LINE).
4. Confirm the deck is actually **playing** (REMAIN counting down), not just loaded/cued.

## Read audio routing (verify)
`python3 tools/xdj-panel/debug/tables.py` → source table `0x107ac88`, channel table `0x107acdc`
(`ch_n.routed_src`: deck=n, line=n+3). Idle default = LINE (ch0→3, ch1→4) until the source
switch is held to DECK.

## Service modes (test mode / update mode) — `docs/service-modes.md`
- **Test mode** (8 sub-modes: versions, LED/LCD all off/on, LCD patterns, KEY/LED, SW, VR/fader
  values in hex, rotary counters): hold DECK 1 MEMORY + TEMPO RANGE *from power-on* —
  `printf 'B 13 04\nB 12 08\n' > /tmp/subucom_ctrl` before `relaunch.sh` (file mode), remove the
  file once Mode 1 shows, then turn/push the browse encoder to change mode. rbp only checks the
  **first** frame it parses, so the holds must already be there at boot. Modes 5/6/7 are oracles
  for mapping LEDs, switches and pots. Occasionally comes up in the normal UI or crashes the
  `TestModeView` thread — just reboot.
- **Firmware-update mode**: `XDJ_BOOT_UPDATE=1 ./tools/xdj-panel/debug/relaunch.sh` (QEMU patch
  19) → the initramfs "Connect USB storage device into top USB1 port." screen. Going further needs a
  USB image with `XDJRX.UPD`, and the updater then wants NAND MTDs + the sub-ucom update protocol
  (not emulated). Debug launcher only (`snapshot=on`); never the app image or a real stick.
- **Factory reset**: DELETE + TEMPO RANGE (`B 13 08` + `B 12 08`) — untested.

## Gotchas
- **gdbstub halts the VM on connect** — send `?` first (rsp.py `cmd('?')`); a bare `interrupt()`
  right after connecting gets no reply. Hardware breakpoints (`set_bp(addr, kind=1)`) work from
  power-on (rbp is non-PIE); clear one after it hits or `c` re-triggers it.
- **Host load:** single-core TCG boot starves under load — **close REA/Ghidra before booting**
  (Ghidra + rekordbox-on-host hang the boot at high load). `iPodMgr` has a load-sensitive
  startup SIGSEGV; keep load low (don't leave thousands of `.ppm` files around → Spotlight).
- **Never hammer the gdbstub:** ~25 rapid RSP halts trip rbp's real-time watchdog → rbp crash +
  splash restart (qemu stays up, same PID). Space reads ≥5 s; a single read is fine.
- **RSP EFAULT** reading rbp VA globals unless the CPU halt lands in rbp's context — retry spaced.
- After an inferior call, resume via monitor `cont` (not bare gdb continue).
- Boot occasionally hangs on the splash — if no home screen after ~120 s, kill + relaunch.

## References
`QEMU.md`, `docs/usb-mount-howto.md`, `docs/emulator-status.md`, `docs/subucom-frame-map.md`,
`docs/service-modes.md`, `docs/subucom-led-map.md`.
Tools: `tools/xdj-panel/debug/`. Skills: [qemu-imx6-patches], [map-subucom-button],
[map-subucom-fader], [map-subucom-knob], [map-subucom-rotary], [map-gpio-control], [rbp-binary-internals].
