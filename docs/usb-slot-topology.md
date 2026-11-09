# XDJ-RX USB slot topology — why a mounted rekordbox USB still says "USB Error"

## The trap
A rekordbox USB drive attached to the emulator mounts cleanly (vfat, read+write,
export.pdb readable) yet rbp shows the modal **"USB Error. Remove the device."**
and never scans the library. The same error appears for *any* library format
(rbxport or a genuine `baken` export), so it is **not** the rekordbox DB.

## Root cause: the slot-connect signal is topology-gated
rbp does not decide "a USB slot has a device" from the mount. It blocks reading a
kernel event channel — **`/proc/udev_usbctn1`** / **`/proc/udev_usbctn2`** (one per
front slot; siblings `udev_usb{,hid,mtp}{1,2}` exist too). A Pioneer driver in the
firmware kernel posts a "connect" on that channel **only for a USB device whose
devpath is exactly `2-1.1` (USB1) or `2-1.2` (USB2)** — those literal strings are
in the kernel image. That means:

- **Guest USB bus 2 = `fsl-ehci.1` = USB HOST1** (bus 1 = `fsl-ehci.0` = USB OTG).
- Both front slots sit **behind a hub at `2-1`** (bus 2, root port 1), at hub
  ports `.1` and `.2`.
- A device anywhere else (e.g. `1-1` on OTG, or `2-1` directly with no hub) mounts
  via the generic `sd*` rule (at `/media/sdaN`) but the slot-connect never fires,
  so rbp waits/errors.

A correctly-placed device at `2-1.1` gets the **slot-specific mount**
(`/media/usb1/sda1` or `/media/usb2/sda1`) instead of the generic `/media/sdaN`.

## QEMU mapping and the launch config
QEMU's i.MX6 has 4 `usb-chipidea` controllers created in order OTG, HOST1, HOST2,
HOST3 → `usb-bus.0 / .1 / .2 / .3`. So **guest bus 2 (HOST1) = `usb-bus.1`**.

HOST1 is high-speed-only (EHCI); QEMU's stock `usb-hub` is full-speed (USB 1.1) and
is rejected. **Patch 08** makes `usb-hub` a USB 2.0 dual-speed hub so it attaches.
Place the drive behind it at `2-1.1`:

```
-device usb-hub,bus=usb-bus.1,port=1,id=xdjhub
-device usb-storage,bus=usb-bus.1,port=1.1,drive=usbstick,id=xdjusb,removable=on
```

The detect GPIOs still gate the firmware's controller resume: `touch /tmp/usb_insert`
drives USB1 (GPIO4_IO30), `/tmp/usb2_insert` drives USB2 (GPIO7_IO12) (patch 06).
The device's hub port must match the GPIO's slot. Observed: hub port 1 (`2-1.1`)
mounts at `/media/usb2` (→ drive `/tmp/usb2_insert`).

## Status: SOLVED (2026-11-09) — QEMU patch 10, both slots, stock firmware
The old "flaky" status was a **cold-plug timing race**, now understood and fixed. Full RE
write-up in `docs/usb-accept-handover.md` §0b/§Fix B; one-paragraph version:

- rbp's **`UsbMountManager`** thread (`FUN_0024cb38`, via `read_sf_rbp`/`FUN_00296628`) accepts
  the library only when it reads a **`"mount"`** text line on **`/proc/udev_usb2`** *while
  polling*. (The earlier `UsbCautionManager`/`udev_usbctn2` lead was wrong: that is the
  persistent `connect` STATE file — re-read every poll, which is why rbp always knew a device
  was present and showed "USB Error" instead of "NO USB".)
- The `"mount"` line is written by the stock udev rule `12-usb-memory-auto-mount.rules` as a
  **blocking rendezvous write** (`echo -n mount … > /proc/udev_usb2`). On cold-plug the device
  enumerates at t≈0, so udev runs that echo before rbp polls; udev's worker-timeout kills the
  blocked echo and the event is lost (fs stays mounted) ⇒ "USB Error". The rare good boot = rbp
  polling in time (hence "load-sensitive").
- **Fix = insert the stick AFTER rbp is polling, exactly like real hardware.** That required
  QEMU to resume a bus-suspended Host1, which it could not (`_is_usbh1_wakeup` reads the
  unmapped USBNC OWIR bit → "invalid wake up"). **`patches/10-imx6-usbnc-wakeup.patch`** models
  USBNC at USBOH3+0x800 and derives OWIR from the EHCI root port's `PORTSC_FPRES`, so post-boot
  insertion resumes Host1 and enumerates normally.

### Reproduction (deterministic, both slots)
1. Boot **hub-only** — keep the hub, do NOT cold-plug the storage; keep the drive as
   `-drive if=none,id=usbstick,file=test/usb/rekordbox_new.img,format=raw,snapshot=on`.
2. Wait until rbp's `UsbMountManager` thread wchan == `poll_schedule_timeout`
   (`/proc/$(pidof rbp)/task/<tid>/wchan`).
3. Insert via the monitor + detect GPIO:
   - **USB2** (slot 2): `device_add usb-storage,bus=usb-bus.1,port=1.1,drive=usbstick,id=xdjusb,removable=on`
     then `touch /tmp/usb2_insert`.
   - **USB1** (slot 1): `drive_add 0 if=none,id=usbstick2,file=<ABS>/test/usb/rekordbox_new.img,format=raw,snapshot=on`,
     `device_add usb-storage,bus=usb-bus.1,port=1.2,drive=usbstick2,id=xdjusb1,removable=on`,
     then `touch /tmp/usb_insert`.
   Hub port `1.1`→devpath `2-1.1`→`/media/usb2` (detect `/tmp/usb2_insert`, GPIO7_IO12);
   hub port `1.2`→`2-1.2`→`/media/usb1` (detect `/tmp/usb_insert`, GPIO4_IO30).
4. Result: Host1 resumes (`ehci_fsl_bus_resume`, 0 "invalid wake up"), device enumerates, udev
   auto-mounts the slot path, rbp opens `export.pdb`, no "USB Error", the slot's browser shows
   the library. Both slots verified simultaneously; stable (`disc=0`, one suspend/resume).

Notes: full-speed enumeration and the MAIN-UCOM key work (patch 09) were both red herrings;
`usbh1_platform_rh_suspend_swfix "big error"` is benign (unmodeled PHY line-state poll). Do NOT
cycle `device_del`/`device_add` in place (tears down the `if=none` drive backend) — reproduce
from a fresh boot. See [[usb-error-investigation]] memory for the full RE trail.
