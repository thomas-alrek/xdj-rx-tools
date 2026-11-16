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
- **`./run.sh`** — from-scratch and idempotent: download the firmware, resolve the key
  (`tools/fetch_key.py`), build the `xdj` CLI, `xdj fw decrypt` → `build/XDJRX.iso`, `xdj prepare`
  (boot blobs in `build/qemu`), build patched QEMU, build the SD image (`qemu/build-nand.sh`), boot
  via `watch.sh`. Use once / after a clean checkout.
- **The desktop app** runs its own NAND unit — skill [build-xdj-app].
- **Debug launcher** `tools/xdj-panel/debug/relaunch.sh` — single-core, `-display cocoa`,
  **gdbstub `tcp::1234`**, monitor `unix:/tmp/d_mon4.sock`, serial `unix:/tmp/d_con4.sock`,
  `-audio coreaudio`, hub-only USB, `XDJ_AUDIO_DEBUG=1`. This is the live-RE/inject bed.

Launch detached and poll for the home screen (boot is single-core TCG, **~60–90 s**):
```bash
XDJ_AUDIO_DEBUG=1 nohup ./tools/xdj-panel/debug/relaunch.sh > /tmp/xdj_qemu.log 2>&1 &
```
**Always launch as the user, never sudo** (a failed `sudo`/ptrace attach can kill qemu).

Debug-launcher options: `XDJ_BOOT_DIR` (boot blobs dir, default `build/qemu`), `XDJ_SD=none`,
`XDJ_SMP` (default 1), `XDJUSB` (image behind the `usbstick` drive), `XDJ_RUNTIME_DIR` (socket-mode
`xdjctrl`/`xdjkey` chardevs instead of the `/tmp` files). Read by the QEMU device models
themselves: `XDJ_BOOT_MEDIA=nand`, `XDJ_NAND_IMG`, `XDJ_PANELCOM_IMG`, `XDJ_UPDATE_TYPE`,
`XDJ_BOOT_UPDATE`, `XDJ_AUTOLEVEL`, `XDJ_AUDIO_*`, `XDJ_TX_LOG`, `XDJ_FRAME_LOG`.
When waiting on firmware progress, never block on one guessed log line: bound the wait and
check the screen (the installer's end is `umount /mnt/usb` after the D007 screen).

### Provision + boot a NAND unit (the real unit's layout; `docs/firmware-update-and-nand.md`)
```bash
X=crates/xdj-fw/target/release/xdj
$X nand init /tmp/u/nand.img --iso build/XDJRX.iso      # erased NAND, factory env from fw_env.txt
$X fw stick build/XDJ-RX_v221.zip /tmp/u/stick.img      # FAT16 stick with XDJRX.UPD
# install = ALL update from USB1 (stick inserted after "### USB update mount."):
XDJ_BOOT_UPDATE=1 XDJ_UPDATE_TYPE=20 XDJ_BOOT_MEDIA=nand XDJ_SD=none \
  XDJ_NAND_IMG=/tmp/u/nand.img XDJ_PANELCOM_IMG=/tmp/u/panelcom.bin XDJUSB=/tmp/u/stick.img \
  ./tools/xdj-panel/debug/relaunch.sh
$X nand info /tmp/u/nand.img                             # installed = env release=
$X nand boot /tmp/u/nand.img /tmp/u/boot                 # u-boot's bootcmd_nand -> blobs
XDJ_BOOT_DIR=/tmp/u/boot XDJ_BOOT_MEDIA=nand XDJ_SD=none XDJ_SMP=2 \
  XDJ_NAND_IMG=/tmp/u/nand.img XDJ_PANELCOM_IMG=/tmp/u/panelcom.bin ./tools/xdj-panel/debug/relaunch.sh
```
Insert the stick on USB1 with `device_add usb-storage,bus=usb-bus.1,port=1.2,drive=usbstick,…`.
A NAND boot **needs ≥ 2 CPUs** (single-core makes `apl_start` run `rbp_solo`, absent on a stock
install). A USER update (type `0x10`) on a blank unit leaves no `data` volume → rbp hangs on the
splash; provision with ALL (`20`).

## Network (eth0 on the LAN)
The FEC (`eth0`) is backed by user-mode NAT unless the optional **socket_vmnet bridge** is installed
(`tools/net/install-vmnet-bridge.sh`, one-time, sudo; README "Networking"). Then the app and
`qemu/watch.sh` attach eth0 to `/var/run/socket_vmnet.bridged.<ifname>` with
`-netdev stream,…,addr.type=unix -net nic,netdev=xdjnet`, and the unit gets a lease from the LAN's
router. QEMU still runs as the user (socket group `staff`); never use QEMU's own `vmnet-*`
backends — they need root. Select: app `XDJ_NET`, `watch.sh` `XDJNIC` = `auto` (default: bridge if
its socket exists, else the Mac-only `/var/run/socket_vmnet`, else NAT) | `user` | `/path/to/socket`.
- **rekordbox on the same Mac (LINK): use `install-vmnet-bridge.sh mac`** — vmnet shared mode, Mac =
  `192.168.105.1` (bridge100), unit leased `192.168.105.x` by macOS bootpd, broadcasts pass, no
  LAN needed (works on Wi-Fi). A LAN bridge never lets the host itself see its guest anyway.
  rekordbox does not link yet (the unit announces on :50000; docs/emulator-status.md "Network").
  The debug launcher stays on NAT.
- Verify: the unit's IP is in the console (`udhcpc … Lease of … obtained`) / the router's lease list.
  MAC is `fec_mac=` from the NAND env's bootargs (stock `00:04:9f:02:68:5e`), so two units bridged
  onto one LAN collide.
- **Wi-Fi bridge: no DHCP lease** (observed): inbound LAN multicast reaches eth0, the DISCOVERs
  leave QEMU, no OFFER returns — with the stock MAC and with vmnet's assigned MAC alike. Bridge a
  wired interface. Capture eth0 without root: QMP `object-add filter-dump,id=d,netdev=xdjnet,file=…`.
- Test without the daemon: any unix stream server speaking 4-byte big-endian length-prefixed
  Ethernet frames stands in for socket_vmnet (point `XDJ_NET` at it; keep the path ≤ 104 chars).

## Control sockets (debug launcher)
- Monitor (HMP): `tools/xdj-panel/debug/mon.py "<cmd>"` → `/tmp/d_mon4.sock`
  (`screendump`, `device_add`, `xp/1wx <addr>`, `info usb`, `cont`).
- Serial console: `tools/xdj-panel/debug/ser.py "<shell cmd>"` → `/tmp/d_con4.sock` (login root).
  **Root shell works only on SD boot** (the SD build clears root's password; [build-xdj-sd-image]).
  A NAND unit keeps the stock rootfs whose root password is unknown — boot SD (the launcher
  default) to poke the running system; it runs the same `rbp`/kernel.
  Everything the console prints is also logged to **`/tmp/xdj_console.log`** (chardev `logfile=`;
  the panel's View > System Log tails it) — read it instead of competing for the socket.
- gdbstub: `tools/xdj-panel/debug/rsp.py` (raw RSP); `tables.py` dumps the audio routing tables.

## See the screen
```bash
python3 tools/xdj-panel/debug/mon.py "screendump /tmp/s.ppm"
sips -s format png /tmp/s.ppm --out /tmp/s.png   # then Read /tmp/s.png
```
Home screen ≈ DECK1/DECK2 "Not Loaded"; splash = Pioneer DJ/rekordbox logo (still booting).

## Mount a rekordbox USB (full recipe + slot topology: `docs/usb.md`)
Boot **hub-only** (no `-device usb-storage` at boot — the debug launcher already does this),
wait for the home screen, then:
```
monitor:  device_add usb-storage,bus=usb-bus.1,port=1.1,drive=usbstick,id=xdjusb,removable=on
```
Hub port **1.1 = USB2**, **1.2 = USB1**. Port 1.1 mounts `/media/usb2/sda1`. Cold-plugging at
boot → "USB Error"; always insert post-boot. **That `device_add` is the whole insert**
(`xdj-usbnc.c` wakes HOST1 from the attach). The BROWSE screen lists **USB1**, so use port 1.2 to
browse a library there; MASTER REC records to the USB2 stick.

**Never `touch /tmp/usb2_insert` (or `/tmp/usb_insert`) to insert.** `xdj-panel.c`'s files drive
GPIO7_IO12 / GPIO4_IO30, which rbp reads as **active-low USB fault flags** (gpiodrv ids
`0xcc`/`0x7e`, `UsbStorageManager` listener `0x250020`): LOW on a mounted slot →
`USB Error. Remove the device.` ~20 s (guest) later; `rm` the file → cleared in ~0.5 s.
Use the files only to inject a USB fault.
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
`A 4e 200`(xfader) `A 24/2a/28 200`(EQ) `A 26 3ff`(trim). POWER `0x5d.0` is active-low (the
harness inverts it: `B 5d 01` = pressed); releasing it while rbp runs starts the standby handshake
(rbp answers TX byte `0x11 = 'S'`; `docs/service-modes.md`).

**One-shot gotcha:** a `b`/`a` line left in the file *together with holds* re-fires every ~30
frames (only a holds-free file is removed after firing) — rewrite the holds alone ~0.5 s later
(`inj.py pulse` does), else a toggle button flips on/off repeatedly.

## Read panel LEDs / meters
`xdj-panel.c` mirrors rbp's LED frame (`SubTxData`): file mode → `cat /tmp/subucom_tx`
(`T <40 hex>`, updated on change); socket mode → the panel server's `GET /leds?once=1`.
`XDJ_TX_LOG=1` logs every changed burst (`SUBUCOM-TX …`) to the qemu stderr. Bit map:
`docs/subucom-led-map.md`; probe new ones with `tools/xdj-panel/debug/ledprobe.py`.

## Hear deck audio
Audio is modelled (SDMA+SSI+AUDMUX, `xdj-audio.c`); the sonifier is pinned to **SDMA ch1 =
MasterOut** (`XDJ_AUDIO_OUT_CH` overrides, `XDJ_AUDIO_DEBUG=1` prints per-channel peaks). For a
loaded deck to be audible you need **routing = DECK** (source switch `B 33 80`/`B 43 80`) **and**
gains up (on-air gate `FUN_00065028`). Load a track from the USB browser.
**Auto-level (`xdj-panel.c`) sets routing=DECK + gains automatically ~4 s after rbp starts**, so
a loaded deck just plays and idle routing is DECK. Disable with `XDJ_AUTOLEVEL=0` (e.g. to test
LINE/PHONO), tune the settle with `XDJ_AUTOLEVEL_DELAY_MS`.

### Audio STUTTERS? (measure, don't guess)
Dropouts are almost always **guest ALSA xruns**, not host underruns: rbp runs a 2×64-frame ring
(2.9 ms), so the guest must see one period complete every 1.45 ms, evenly. `xdj-audio.c`'s pacer
thread does that. To measure: count `ALSA: Resolve xrun` on the guest console (read
`/tmp/d_con4.sock` for 30 s; ~0 is healthy), and boot
with `XDJ_AUDIO_STATS=1` for a 5 s line: `underruns`/`gap` (host ran dry), `minfifo`, `guest_late`
(rbp didn't re-arm a period in time = guest CPU too slow), `pace=689/s` (must equal 44100/64),
`resync` (pacer woke >1 period late). More headroom: `XDJ_AUDIO_LATENCY_MS=40` (adds latency).

### Track loads slow? (measure)
- The pad LEDs A→D are the load progress bar: time it from `/tmp/subucom_tx` byte `0x02` bits 7..4
  (deck 2: byte `0x06`) after `B 5d 40`. Reference: a 60 MB WAV loads in ~7.4 s.
- Raw read speed: `sync; echo 3 > /proc/sys/vm/drop_caches; dd if=<wav> of=/dev/null bs=256k count=80`
  on the guest (`ser.py`; long output → read `/tmp/xdj_console.log`). Expect ~5.6 MB/s; much less →
  check `04-usb-chipidea-hub` / `05-macos-submillisecond-poll` are in the build.
- Link speed: `cat /sys/bus/usb/devices/2-1*/speed` must be **480**. The firmware itself sets
  `max_sectors_kb=8` (`/etc/udev/rules.d/usb-iosched.sh`), so per-request latency rules throughput —
  don't change that (firmware), fix latency QEMU-side.
- Threads: rbp pins everything to **CPU 0** except the audio path (`JuceALSA`, `FGPR:1/2` on CPU 1)
  — same as the real **dual-core** unit (firmware `thread_smp_affinity` folds mask 3 → 1). `top -H -p
  $(pidof rbp)` in the guest; the loader threads are `PcmReader:N`, `Tot_LoadTASK`, `PageBufferedInput`.
- Guest kernel busy-loop? `cat /proc/<pid>/stat` state R + high stime, then sample PCs with HMP
  `info registers -a` and resolve kernel addresses against the guest's `/proc/kallsyms`.

### Deck loaded but SILENT? (debug recipe)
1. **Stray `/tmp/subucom_ctrl`** is the #1 cause — any hold there (even an unrelated leftover
   like the web panel's browse `A 54 0`) overrides auto-level for that control; if it sets routing
   or a gain it can mute the deck. `rm /tmp/subucom_ctrl` and auto-level resumes.
2. **`XDJ_AUDIO_DEBUG=1` peaks** (in the qemu log): master `[sdma-au] peak=0` = genuine internal
   silence (routing/gain/gate), while `peak`≫0 = audio IS produced → the gap is host output.
   All `[sdma-ch] bufpeak=0` incl. deck channels = no PCM reaching the engine (not playing, or the
   decode→mixer path). ~44100 frames/s on every channel = the clock is fine.
3. **`tables.py` routing** — `ch0 routed_src` must be `0` (DECK), not `3` (LINE).
4. Confirm the deck is actually **playing** (REMAIN counting down), not just loaded/cued.

## Read audio routing (verify)
`python3 tools/xdj-panel/debug/tables.py` → source table `0x107ac88`, channel table `0x107acdc`
(`ch_n.routed_src`: deck=n, line=n+3). With `XDJ_AUTOLEVEL=0` the idle default is LINE (ch0→3,
ch1→4) until the source switch is held to DECK.

## Regression test — `docs/testing.md`
`tools/test/regress.py` boots a throwaway unit end to end (stages `provision,boot,panel,usb,
standby,testmode,custom`; `--only`, `--unit <installed fw dir>` to skip provisioning, `--keep`) with
private ports/runtime dir; `--app dist/XDJ-RX.app` tests the packaged bundle. Run it before merging
emulator/host/panel/firmware changes. Panel facts it encodes: MENU must be **held** (~2 s) to open
UTILITY; the browser shows **USB1**.

## Service modes (test mode / update mode) — `docs/service-modes.md`
- **Test mode** (8 sub-modes: versions, LED/LCD all off/on, LCD patterns, KEY/LED, SW, VR/fader
  values in hex, rotary counters): hold DECK 1 MEMORY + TEMPO RANGE *from power-on* —
  `printf 'B 13 04\nB 12 08\n' > /tmp/subucom_ctrl` before `relaunch.sh` (file mode), remove the
  file once Mode 1 shows, then turn/push the browse encoder to change mode. rbp only checks the
  **first** frame it parses, so the holds must already be there at boot. Modes 5/6/7 are oracles
  for mapping LEDs, switches and pots. In the desktop app: **File > Power On in Test Mode** (panel
  `POST /power on test`; holds, cold-boots, releases after `ALSA: Resolve xrun`). The control
  chardevs bind at realize, so boot-time holds are in rbp's first frame. **Known flake:** ~1 boot
  in 8 still comes up in the normal UI (rbp-side; `regress.py` retries once). `XDJ_FRAME_LOG=1`
  prints the first 12 frames' `[12]/[13]` bytes to check what rbp saw.
- **Firmware-update mode**: `XDJ_BOOT_UPDATE=1 ./tools/xdj-panel/debug/relaunch.sh`
  (GPIO3_IO30 strap in `xdj-board.c`) → the initramfs "Connect USB storage device into top USB1
  port." screen. Going further needs a USB image with `XDJRX.UPD` (`xdj fw stick`); with
  `XDJ_NAND_IMG` + `XDJ_PANELCOM_IMG` the full update completes (NAND recipe above). The desktop
  app provisions and boots its NAND unit this way by itself (`docs/standalone-app.md` §4a).
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
`docs/usb.md`, `docs/firmware-update-and-nand.md`, `docs/emulator-status.md`,
`docs/subucom-frame-map.md`, `docs/service-modes.md`, `docs/subucom-led-map.md`, `docs/testing.md`.
Tools: `tools/xdj-panel/debug/`. Skills: [qemu-imx6-patches], [map-subucom-button],
[map-subucom-fader], [map-subucom-knob], [map-subucom-rotary], [map-gpio-control], [rbp-binary-internals].
