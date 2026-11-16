# USB in the emulator

How the XDJ-RX's two front USB slots are modelled, how to insert a rekordbox stick (desktop app
and dev launcher), and what goes wrong when the rules below are broken.

**The one rule:** boot with the hub but **no storage**, then insert the stick **after `rbp` is
polling** — exactly like plugging a stick into the real unit. Insert with QEMU `device_add`
only; never touch the USB fault-flag files.

## 1. Slot topology

| front slot | QEMU attach point | guest devpath | slot mount |
|---|---|---|---|
| **USB1** | `bus=usb-bus.1,port=1.2` | `2-1.2` | `/media/usb1/sda1` |
| **USB2** | `bus=usb-bus.1,port=1.1` | `2-1.1` | `/media/usb2/sda1` |

- QEMU's i.MX6 creates four `usb-chipidea` controllers in the order OTG, HOST1, HOST2, HOST3 →
  `usb-bus.0 … .3`. Guest bus 1 = `fsl-ehci.0` (OTG), guest bus 2 = `fsl-ehci.1` = **HOST1** =
  `usb-bus.1`.
- Both front slots sit **behind a USB 2.0 hub on HOST1 root port 1** (`2-1`), at hub ports `.1`
  and `.2`. The launch line always carries the hub:
  `-device usb-hub,bus=usb-bus.1,port=1,id=xdjhub`.
- The firmware kernel only reports a *slot* connect for the literal devpaths `2-1.1` / `2-1.2`
  (the strings are in the kernel image). A device anywhere else (`1-1` on OTG, or `2-1` with no
  hub) still mounts through the generic `sd*` udev rule at `/media/sdaN`, but `rbp` never sees a
  slot connect and shows **"USB Error. Remove the device."**
- HOST1 is high-speed-only EHCI. QEMU's stock `usb-hub` is full-speed (USB 1.1) and is rejected;
  `qemu/patches/04-usb-chipidea-hub.patch` gives it a USB 2.0 descriptor and reports high-speed
  ports.

### Speed: 480 Mbit/s
`04-usb-chipidea-hub.patch` also implements the ChipIdea `PORTSC[27:26]` PSPD port-speed field
(without it Linux enumerates everything at 12 Mbit/s), `USBSTS[7]` SRI (the i.MX6 BSP busy-waits
on it for high-speed links), hub high-speed port status, and per-microframe async-schedule
re-polling. On macOS, `05-macos-submillisecond-poll.patch` is also needed (QEMU's timers
otherwise poll at ~1 ms). The firmware caps USB requests at 8 KB itself, so per-request latency is
what matters: front-slot reads go from 1.3 to ~5.5 MB/s and a track load from ~10.6 s to ~7.4 s.

### Post-boot insert: USBNC wakeup
After boot, `rbp` lets HOST1 suspend. A later attach must resume it: the guest's
`_is_usbh1_wakeup` checks the USBNC `OWIR` (wakeup-interrupt-request) bit. `qemu/xdj/xdj-usbnc.c`
models the USBNC block at USBOH3+0x800 and derives `OWIR` live from the EHCI root port's
`PORTSC_FPRES`, so the attach behind the hub wakes HOST1 and it enumerates normally. Without it
the console shows `_is_usbh1_wakeup … invalid wake up` and nothing mounts.
`usbh1_platform_rh_suspend_swfix "big error"` on the console is benign (an unmodelled PHY
line-state poll).

## 2. Why the insert must come after `rbp` is polling

`rbp` accepts a slot's library only when its **`UsbMountManager`** thread (`FUN_0024cb38`, read
via `FUN_00296628`) reads a `"mount"` text line on **`/proc/udev_usb1`** / **`/proc/udev_usb2`**
*while it is polling*. That line comes from the stock udev rule
`12-usb-memory-auto-mount.rules`, as a **blocking rendezvous write**
(`echo -n mount … > /proc/udev_usb2`).

- **Cold-plug** (the stick present at boot): the device enumerates at t≈0, udev runs the echo
  before `rbp` polls, udev's worker timeout kills the blocked echo and the event is lost. The
  filesystem stays mounted, but `rbp` shows "USB Error" and never scans the library.
- `/proc/udev_usbctn1` / `/proc/udev_usbctn2` are the persistent *connect state*, re-read every
  poll — which is why `rbp` knows a device is present and says "USB Error" rather than "NO USB".
  (Siblings `udev_usb{,hid,mtp}{1,2}` exist too.)
- **Post-boot insert** (what the app and the recipe below do) delivers the `mount` line while
  `rbp` is waiting for it. Both slots work, simultaneously, on stock firmware.

## 3. The USB fault flags — leave them alone

**GPIO4_IO30** (USB1, gpiodrv id `0x7e`) and **GPIO7_IO12** (USB2, id `0xcc`) are `rbp`'s
**active-low USB fault flags**, not insert-detect lines. `UsbStorageManager` listens on them
(GPIO listener `0x250020`, dispatch `0x2500cc`; the warning's hide timer `CautionTimer` at
`mgr+0x3e4`, fired from `0x251e4c`). Held LOW once the slot has mounted, `rbp` shows **"USB Error.
Remove the device."**; back HIGH, the modal clears within ~0.5 s and the library stays loaded.

`qemu/xdj/xdj-panel.c` exposes them for deliberate fault injection only:

| file | effect |
|---|---|
| `touch /tmp/usb_insert` / `rm` | USB1 fault asserted (GPIO4_IO30 LOW) / cleared |
| `touch /tmp/usb2_insert` / `rm` | USB2 fault asserted (GPIO7_IO12 LOW) / cleared |

A normal insert never creates these files. The app's host and panel delete them on every insert
and eject, so a stale file can't leave a slot faulted.

## 4. Desktop app

- **USB1 / USB2 LOAD** (panel buttons / the native menu) → `POST /usb <usb1|usb2> <path>` →
  `app/host/src/panel.rs` stages a space-free symlink (`/tmp/xdjpanel_<slot>.img`; HMP
  `drive_add` doesn't quote paths), then over QMP runs
  `drive_add 0 if=none,…,format=raw` + `device_add usb-storage,bus=usb-bus.1,
  port=<1.2|1.1>,…,removable=on`. A disk image or a host `/dev/diskN` both work.
- **Re-load ejects first**: USB STOP on the unit only unmounts in the firmware; the QEMU device
  stays plugged, so a new LOAD `device_del`s the previous one. Loading the image/stick that is in
  the *other* slot moves it (ejects it there first): its drive holds the image's write lock.
  The new backing is checked before anything is ejected, so a failed load keeps the slot's stick,
  and a load whose `device_add` fails removes its drive again (no orphan holding the lock).
  Loads run one at a time. A failed load shows a native error dialog in the app (queued loads:
  via the `alerts` list in `GET /power`).
- **Opened in macOS**: an image attached in macOS (opened in Finder) is ejected there first
  (`hdiutil info` → `diskutil eject`); if a volume on it is busy, the load fails and says so.
- **Read/write**: every insert (panel, MCP, boot-time, update stick) writes straight to the image or
  device — MASTER REC recordings, rekordbox data and the updater's writes stay on it. Before the
  attach the host (`prepare_usb_backing`) unmounts a `/dev/diskN` on the host (the guest must be
  its only writer; a busy volume fails the load) and checks the image or device opens read/write
  (macOS makes a removable disk's node the console user's — no root needed). Right after the
  unmount, disk arbitration holds the device briefly, so `attach_usb_drive` retries a
  `drive_add` that fails with "Resource busy". Eject the slot (or power off) before using the stick on the host again.
- **Loaded while powered off**: the load is **queued** (`GET /power` shows it under `pending`)
  and inserted once the guest is ready — `Resolve xrun` on the console + 6 s for a normal boot, or
  the update initramfs' `USB update mount` + 2 s in firmware update mode.
- **Boot-time insert (CLI use)**: `xdj-host` reads `XDJ_USB=<image|/dev/diskN>`,
  `XDJ_USB_SLOT=usb1|usb2` (default `usb2`), `XDJ_USB_DELAY=<s>` (settle after the
  `Resolve xrun` marker, default 6).
- Firmware provisioning and *Install Firmware Update* put their update stick on **USB1** — the
  slot the update screen asks for ([firmware-update-and-nand.md](firmware-update-and-nand.md)).

What the firmware does with the slots:
- The **browser** (BROWSE / SOURCE) shows **USB1**; a stick on USB1 replaces "NO USB" with its
  rekordbox library.
- **MASTER REC** (`0x5e.2`, code `0x401`) records the master output to the **USB2** stick
  (`/PIONEER REC/REC%03d.WAV`; [subucom-frame-map.md](subucom-frame-map.md)).

## 5. Dev recipe (debug launcher)

`tools/xdj-panel/debug/relaunch.sh` boots hub-only: it defines the stick backend but does not
attach it —

```
-drive if=none,id=usbstick,file=test/usb/<image>.img,format=raw   # read/write; XDJUSB overrides; default: the first test/usb/*.img
-device usb-hub,bus=usb-bus.1,port=1,id=xdjhub
```

with the HMP monitor on `/tmp/d_mon4.sock` (`debug/mon.py '<cmd>'`) and the serial console on
`/tmp/d_con4.sock` (`debug/ser.py '<shell cmd>'`). **Never add `-device usb-storage` to the launch
line** — that is a cold-plug.

1. **Wait until `rbp` is polling.** The home screen plus a couple of seconds is usually enough.
   Precise check on the guest console — a thread of `rbp` parked in poll:
   ```
   for t in /proc/$(pidof rbp_solo rbp)/task/*; do cat $t/wchan; echo; done | grep -c poll_schedule_timeout
   ```
2. **Insert** (HMP monitor):
   ```
   # USB2 — the launch-time backend
   device_add usb-storage,bus=usb-bus.1,port=1.1,drive=usbstick,id=xdjusb,removable=on
   # USB1 — add a second backend first (absolute path)
   drive_add 0 if=none,id=usbstick2,file=/abs/path/test/usb/<image>.img,format=raw
   device_add usb-storage,bus=usb-bus.1,port=1.2,drive=usbstick2,id=xdjusb1,removable=on
   ```
   Over QMP the same lines go through `human-monitor-command` (what the app does). Note that HMP
   reports its errors as text inside a successful QMP `return`.
3. **Verify:**
   ```
   monitor:  info usb                 # a "QEMU USB MSD" entry with your id
   guest:    grep usb /proc/mounts    # /dev/sda1 /media/usb2/sda1 vfat ...
             ls /media/usb2/sda1      # Contents  PIONEER  PIONEER REC
             dmesg | tail             # "sda: sda1", "Attached SCSI removable disk"
   ```
   On the LCD: no "USB Error" modal, and the slot's library shows in the browser.

An insert that went wrong can't be retried in place: `device_del` tears down the `if=none` drive
backend. Reproduce from a fresh boot.

`qemu/watch.sh`'s `XDJUSB=` attaches the image as a cold-plugged `usb-storage` on `usb-bus.0`
(OTG) — it mounts at `/media/sdaN` but is not a front slot. Use `relaunch.sh` or the app for slot
work.

## 6. A rekordbox test image

`rbp` reads a genuine rekordbox device export (`PIONEER/rekordbox/export.pdb` + the `USBANLZ`
analysis files) on a FAT filesystem. `tools/build-rb-usb.sh` builds one as a raw FAT32/MBR disk
image from a rekordbox *Export Collection* XML, using `baken expressport` — no rekordbox needed to
write the stick:

```sh
brew install M-Igashi/tap/baken
tools/build-rb-usb.sh ~/Music/rekordbox/collection.xml "Sets/Test" test/usb/rekordbox_new.img 2g
# extra baken flags: BAKEN_FLAGS="--cdjsafe --generate-analysis" tools/build-rb-usb.sh ...
```

`relaunch.sh` and the regression suite use the first `test/usb/*.img`
([testing.md](testing.md)).

## 7. Troubleshooting

| symptom | cause | fix |
|---|---|---|
| "USB Error. Remove the device." ~20 s (guest time) after a good mount | a `/tmp/usb*_insert` fault file exists | `rm` it; the modal clears in ~0.5 s |
| "USB Error" at boot, no library | cold-plugged, or inserted before `rbp` polled | reboot hub-only, insert after the home screen |
| mounts at `/media/sda1`, not `/media/usbN` | not behind the hub at `2-1.1`/`2-1.2` | `bus=usb-bus.1,port=1.1` (or `1.2`) with the hub present |
| nothing enumerates; `invalid wake up` on the console | `xdj-usbnc.c` not in the running QEMU | rebuild QEMU (`qemu/build-qemu.sh`) |
| hub rejected at boot / `device_add` says the port is busy | QEMU built without `04-usb-chipidea-hub.patch` | rebuild with the patch set |
| devices enumerate at 12 Mbit/s, track loads slow | same patch missing (PSPD), or `05-macos-submillisecond-poll.patch` on macOS | rebuild |
| `device_add`: can't find drive / duplicate id | `drive=` must match the `-drive if=none,id=…`; ids must be unique | use fresh ids; after a failed `device_del`, reboot |
