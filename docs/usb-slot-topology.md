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

## Still open (the last mile)
With the drive correctly at `2-1.1` + slot mount, rbp **still** errors before the
library scanner (`FUN_0024cb38` @ `0x24cf14` never hits). Leading suspect: the
device enumerates at **full speed** behind the hub (dmesg: `2-1.1: not running at
top speed; connect to a high speed hub`) — QEMU's chipidea-EHCI reports the port
full-speed even though it accepted the hub as high-speed. Real slots carry
high-speed sticks; rbp's `UsbStorageManager` may reject a full-speed slot device.
Next: get a high-speed enumeration on chipidea, or gdb-trace the exact
`UsbStorageManager` check that routes to the Caution/"USB Error" state. See
[[usb-error-investigation]].
