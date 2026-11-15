# HOW-TO: bring up a rekordbox USB in the emulator (step by step)

The one rule that makes this work: **boot with the hub but NO storage, then insert the
storage AFTER rbp is polling** — exactly like plugging a stick into real hardware. Cold-
plugging a stick that is already present at boot hits a mount-rendezvous race and rbp throws
the modal **"USB Error. Remove the device."** (the *why* is in `docs/usb-slot-topology.md`;
this page is just the recipe.)

> **2026-11-15 — the "USB Error" on a correct insert is FIXED (it was us, not rbp).** The
> `/tmp/usb_insert` / `/tmp/usb2_insert` files (`xdj-panel.c` (was patch 06)) do **not** drive an insert-detect
> line: GPIO4_IO30 / GPIO7_IO12 are rbp's **active-low USB fault flags** (gpiodrv ids
> `0x7e`/`0xcc`, listener `0x250020` in `UsbStorageManager`). Holding one LOW after the slot
> mounts makes rbp show **"USB Error. Remove the device."**; HIGH clears it. A normal insert is
> **`device_add` only** — `xdj-usbnc.c` (was patch 10) wakes HOST1 from the attach itself. **Never `touch` the
> files to insert**; they're now only for deliberately injecting a USB fault. (Verified live:
> device_add alone → mount, `export.pdb` open, USB2 browser shows the library, no modal.)

## 0. Prerequisites (check these first — most "it won't work" is here)
- **QEMU built with patches 06, 08, 10.** `08` = USB-2.0 hub (stock `usb-hub` is USB-1.1 and
  HOST1 rejects it). `10` = USBNC wakeup (lets QEMU resume a suspended HOST1 so a *post-boot*
  insert enumerates; without it you get `_is_usbh1_wakeup … invalid wake up` and nothing
  mounts). `06` = USB **fault-flag** injection (`/tmp/usb2_insert` etc.) — *not* needed to insert.
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

## 3. Insert the stick (monitor device_add only)
For **USB2 / slot 2** (the usual one):

```
monitor:  device_add usb-storage,bus=usb-bus.1,port=1.1,drive=usbstick,id=xdjusb,removable=on
```

That's it: the attach behind the hub at devpath `2-1.1` raises the root port's FPRES, `xdj-usbnc.c` (was patch 10)
turns that into the USBNC wakeup, HOST1 resumes and enumerates. **Do not `touch
/tmp/usb2_insert`** — that asserts the slot's USB *fault* flag and produces the "USB Error"
modal (see the banner at the top).

Sending the monitor line programmatically (socketed launch):
```
printf 'device_add usb-storage,bus=usb-bus.1,port=1.1,drive=usbstick,id=xdjusb,removable=on\n' \
  | nc -U /tmp/d_mon4.sock        # or a tiny python AF_UNIX client
```

For **USB1 / slot 1** instead, use a second drive + hub port `1.2` + the other GPIO:
```
monitor:  drive_add 0 if=none,id=usbstick2,file=<ABS_PATH>/test/usb/rekordbox_new.img,format=raw,snapshot=on
monitor:  device_add usb-storage,bus=usb-bus.1,port=1.2,drive=usbstick2,id=xdjusb1,removable=on
```
Port→slot map: hub port `1.1` → devpath `2-1.1` → `/media/usb2`; hub port `1.2` → `2-1.2` →
`/media/usb1`. (Fault flags, for fault injection only: USB2 = `/tmp/usb2_insert` /
GPIO7_IO12, USB1 = `/tmp/usb_insert` / GPIO4_IO30.)

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
- **"USB Error. Remove the device." modal ~20 s (guest time) after a good mount** → a
  `/tmp/usb*_insert` fault file is present (the old recipe touched it) → `rm` it; the modal
  clears within ~0.5 s and the library stays loaded.
- **"USB Error" at boot / no library** → cold-plugged, before rbp was polling → reboot
  hub-only, wait for the home screen, insert in step 3.
- **Mounts at `/media/sda1`, not `/media/usb2`** → device isn't behind the hub at `2-1.1`
  (wrong `bus=`/`port=`, or no hub) → use `bus=usb-bus.1,port=1.1` with the hub present.
- **Nothing enumerates; log shows `invalid wake up` / no resume** → `xdj-usbnc.c` (was patch 10) not in the
  running binary → rebuild qemu (`qemu/build.sh` or ninja) and relaunch.
- **`device_add` says the hub/port is busy or the hub was rejected at boot** → `04-usb-chipidea-hub` (was patch 08) not
  applied (hub stayed USB-1.1 and HOST1 refused it) → rebuild with `04-usb-chipidea-hub` (was patch 08).
- **`device_add` "Property 'drive' … can't find'/duplicate id"** → the `drive=` id must match
  the `-drive if=none,id=…` from launch, and `id=xdjusb` must be unique (don't reuse after a
  failed `device_del`; reboot).
- **Flaky across boots** → you're cold-plugging. Re-check step 1 has NO `-device usb-storage`.

## One-liner summary
Boot hub-only → wait for the home screen → `device_add usb-storage,bus=usb-bus.1,port=1.1,
drive=usbstick,id=xdjusb,removable=on` → mounts at `/media/usb2/sda1` (no `touch`). Deep mechanism: `docs/usb-slot-topology.md`, `docs/usb-accept-handover.md`.
