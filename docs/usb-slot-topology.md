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

## RESOLVED (2026-11-09)
With the drive correctly at `2-1.1` + slot mount **and a genuine `baken` `export.pdb`**,
rbp **accepts** the device — no "USB Error" modal, and it opens and parses the library.
Verified live against the guest serial console (`/tmp/d_con4.sock`):

- `screendump` shows the normal deck view (DECK 1/2, "Not Loaded."), **no error modal**.
- rbp's open fds include `…/PIONEER/rekordbox/export.pdb` (read to offset 126976 of
  217088) and `…/PIONEER/USBANLZ/USBMNG.DAT` — it registered the USB as a browsable
  rekordbox source.

The **full-speed** enumeration was a red herring: the kernel logs `2-1.1: not running at
top speed` but rbp accepts the device anyway. The fix was purely topology + a real
library (patch 08 hub → `2-1.1` → slot mount `/media/usb2/sda1`). rbp was **not** patched.

Good image: `test/usb/rekordbox_new.img`. **Next gate:** open the media browser to
browse/LOAD a track — see docs/xdj-rx-control-map.md "Browser page gate". See
[[usb-error-investigation]].
