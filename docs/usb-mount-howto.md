# HOW-TO: bring up a rekordbox USB in the emulator (step by step)

The one rule that makes this work: **boot with the hub but NO storage, then insert the
storage AFTER rbp is polling** — exactly like plugging a stick into real hardware. Cold-
plugging a stick that is already present at boot hits a mount-rendezvous race and rbp throws
the modal **"USB Error. Remove the device."** (the *why* is in `docs/usb-slot-topology.md`;
this page is just the recipe.)

## 0. Prerequisites (check these first — most "it won't work" is here)
- **QEMU built with patches 06, 08, 10.** `08` = USB-2.0 hub (stock `usb-hub` is USB-1.1 and
  HOST1 rejects it). `10` = USBNC wakeup (lets QEMU resume a suspended HOST1 so a *post-boot*
  insert enumerates; without it you get `_is_usbh1_wakeup … invalid wake up` and nothing
  mounts). `06` = the detect-GPIO insert hooks (`/tmp/usb2_insert` etc.).
  Verify: `grep -l "usbnc\|OWIR" qemu/patches/10-*.patch` and that `build/qemu-src/.../build/
  qemu-system-arm` was built after those patches.
- **A rekordbox-formatted raw image**, e.g. `test/usb/rekordbox_new.img` (FAT/vfat with a
  `PIONEER/rekordbox/export.pdb`). `~/Downloads/rekordbox_test.dmg` is another.
- Know your **control sockets** for this launch. The socketed debug launcher uses
  `-monitor unix:/tmp/d_mon4.sock` and `-serial unix:/tmp/d_con4.sock`. `qemu/watch.sh` puts
  the HMP monitor on the terminal (type the `device_add` line there) and the serial on stdio.
  Below, "**monitor:**" = send to the HMP monitor; "**host:**" = run on the Mac.

## 1. Launch HUB-ONLY (do NOT attach usb-storage at boot)
Keep the drive backend defined but unattached, and attach only the hub on `usb-bus.1`
(= guest bus 2 = HOST1, where the front slots live):

```
-drive if=none,id=usbstick,file=test/usb/rekordbox_new.img,format=raw,snapshot=on
-device usb-hub,bus=usb-bus.1,port=1,id=xdjhub
```

That is exactly what `relaunch.sh` / the debug launcher already do. **There is no
`-device usb-storage` on the command line** — adding one here is the #1 mistake (it cold-
plugs → "USB Error").

## 2. Wait until rbp is actually up and polling
Let the UI finish booting (home screen). rbp's `UsbMountManager` thread must be in its poll
before you insert. Quick-and-dirty: wait until the LCD is at the home screen plus a couple
seconds. Precise check over the serial console:

```
# guest (serial): find rbp and confirm a thread is parked in poll
pidof rbp_solo rbp
for t in /proc/$(pidof rbp_solo rbp)/task/*; do cat $t/wchan; echo; done | grep -c poll_schedule_timeout
```

If you insert too early, you get "USB Error" — there is no recovery in place; **reboot and
insert later** (do not `device_del`/`device_add` to retry — that tears down the `if=none`
backend; always reproduce from a fresh boot).

## 3. Insert the stick (monitor device_add + host detect-GPIO)
For **USB2 / slot 2** (the usual one):

```
monitor:  device_add usb-storage,bus=usb-bus.1,port=1.1,drive=usbstick,id=xdjusb,removable=on
host:     touch /tmp/usb2_insert
```

Order: `device_add` first (plugs the device behind the hub at devpath `2-1.1`), then the
`touch` (asserts the slot's insert GPIO so the firmware resumes/enumerates HOST1).

Sending the monitor line programmatically (socketed launch):
```
printf 'device_add usb-storage,bus=usb-bus.1,port=1.1,drive=usbstick,id=xdjusb,removable=on\n' \
  | nc -U /tmp/d_mon4.sock        # or a tiny python AF_UNIX client
```

For **USB1 / slot 1** instead, use a second drive + hub port `1.2` + the other GPIO:
```
monitor:  drive_add 0 if=none,id=usbstick2,file=<ABS_PATH>/test/usb/rekordbox_new.img,format=raw,snapshot=on
monitor:  device_add usb-storage,bus=usb-bus.1,port=1.2,drive=usbstick2,id=xdjusb1,removable=on
host:     touch /tmp/usb_insert
```
Port→slot map: hub port `1.1` → devpath `2-1.1` → `/media/usb2` (GPIO `/tmp/usb2_insert`);
hub port `1.2` → `2-1.2` → `/media/usb1` (GPIO `/tmp/usb_insert`). **The hub port and the
GPIO file must name the same slot** or the connect never fires.

## 4. Verify it mounted (and rbp accepted it)
```
# host: QEMU sees the device behind the hub
monitor:  info usb         # expect a "QEMU USB MSD ... ID: xdjusb" entry

# guest (serial): the SLOT path is mounted (not the generic /media/sdaN)
grep usb2 /proc/mounts                       # -> /dev/sda1 /media/usb2/sda1 vfat ...
ls /media/usb2/sda1                          # -> Contents  PIONEER  PIONEER REC
dmesg | tail                                 # -> "sda: sda1", "Attached SCSI removable disk"
```
On the LCD: no "USB Error" modal; SOURCE → USB2 shows the library.

## Troubleshooting (symptom → cause → fix)
- **"USB Error. Remove the device." modal** → inserted while/before cold-plug, before rbp was
  polling → reboot hub-only, wait for the home screen, insert in step 3. (Inherent race; the
  patch-10 fix only makes *post-boot* insertion reliable, not cold-plug.)
- **Mounts at `/media/sda1`, not `/media/usb2`** → device isn't behind the hub at `2-1.1`
  (wrong `bus=`/`port=`, or no hub) → use `bus=usb-bus.1,port=1.1` with the hub present.
- **Nothing enumerates; log shows `invalid wake up` / no resume** → patch 10 not in the
  running binary → rebuild qemu (`qemu/build.sh` or ninja) and relaunch.
- **`device_add` says the hub/port is busy or the hub was rejected at boot** → patch 08 not
  applied (hub stayed USB-1.1 and HOST1 refused it) → rebuild with patch 08.
- **`device_add` "Property 'drive' … can't find'/duplicate id"** → the `drive=` id must match
  the `-drive if=none,id=…` from launch, and `id=xdjusb` must be unique (don't reuse after a
  failed `device_del`; reboot).
- **`touch` has no effect** → wrong GPIO file for the slot, or the `device_add` didn't land
  first → do `device_add` then `touch`, and match the slot (usb2_insert ↔ port 1.1).
- **Flaky across boots** → you're cold-plugging. Re-check step 1 has NO `-device usb-storage`.

## One-liner summary
Boot hub-only → wait for the home screen → `device_add usb-storage,bus=usb-bus.1,port=1.1,
drive=usbstick,id=xdjusb,removable=on` → `touch /tmp/usb2_insert` → mounts at
`/media/usb2/sda1`. Deep mechanism: `docs/usb-slot-topology.md`, `docs/usb-accept-handover.md`.
