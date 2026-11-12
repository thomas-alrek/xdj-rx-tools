---
name: run-xdj-emulator
description: Launch and drive the XDJ-RX firmware emulator - boot the patched QEMU to the live rekordbox UI, capture the screen, mount a rekordbox USB, inject sub-ucom panel controls, read audio routing over the gdbstub, and hear deck audio on the host. Use when you need to run/boot the emulator, reproduce a UI/audio/control behavior, or set up a live test bed.
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
host:     touch /tmp/usb2_insert
```
Mounts `/media/usb2/sda1`. Cold-plugging at boot → "USB Error" modal; always insert post-boot.

## Inject sub-ucom panel controls
Write one spec per line to **`/tmp/subucom_ctrl`** (harness reads it per frame, recomputes CRC):
`A <off> <val16>` = hold 16-bit analog (fader/knob); `a` one-shot; `B <off> <mask>` = hold a
button bitmask; `b` one-shot. Use `tools/xdj-panel/debug/inj.py pulse` to one-shot **without**
clearing the analog-gain holds (a bare write replaces all holds and zeroes the mix). Top-panel
source/DECK keys are GPIO → `/tmp/mainkey` (number=press, remove=release).
Examples: source switch DECK = `B 33 80` + `B 43 80`; gains = `A 2c 3ff`(fader) `A 4a 3ff`(master)
`A 4e 200`(xfader) `A 24/2a/28 200`(EQ) `A 26 3ff`(trim).

## Hear deck audio
Audio is modelled (SDMA+SSI+AUDMUX, patch 11); the sonifier is pinned to **SDMA ch1 =
MasterOut** (`XDJ_AUDIO_OUT_CH` overrides, `XDJ_AUDIO_DEBUG=1` prints per-channel peaks). For a
loaded deck to be audible you need **routing = DECK** (source switch `B 33 80`/`B 43 80`) **and**
gains up (on-air gate `FUN_00065028`). Load a track via the USB2 browser (track-load works).

## Read audio routing (verify)
`python3 tools/xdj-panel/debug/tables.py` → source table `0x107ac88`, channel table `0x107acdc`
(`ch_n.routed_src`: deck=n, line=n+3). Idle default = LINE (ch0→3, ch1→4) until the source
switch is held to DECK.

## Gotchas
- **Host load:** single-core TCG boot starves under load — **close REA/Ghidra before booting**
  (Ghidra + rekordbox-on-host hang the boot at high load). `iPodMgr` has a load-sensitive
  startup SIGSEGV; keep load low (don't leave thousands of `.ppm` files around → Spotlight).
- **Never hammer the gdbstub:** ~25 rapid RSP halts trip rbp's real-time watchdog → rbp crash +
  splash restart (qemu stays up, same PID). Space reads ≥5 s; a single read is fine.
- **RSP EFAULT** reading rbp VA globals unless the CPU halt lands in rbp's context — retry spaced.
- After an inferior call, resume via monitor `cont` (not bare gdb continue).
- Boot occasionally hangs on the splash — if no home screen after ~120 s, kill + relaunch.

## References
`QEMU.md`, `docs/usb-mount-howto.md`, `docs/emulator-status.md`, `docs/subucom-frame-map.md`.
Tools: `tools/xdj-panel/debug/`. Skills: [qemu-imx6-patches], [map-subucom-button],
[map-subucom-fader], [map-subucom-knob], [map-subucom-rotary], [map-gpio-control], [rbp-binary-internals].
