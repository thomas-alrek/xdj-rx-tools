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

## Status: library-accept is FLAKY (not reliably resolved) — updated 2026-11-09 late
With the drive at `2-1.1` + slot mount + a genuine `baken` `export.pdb`, rbp **sometimes**
accepts the device and parses the library (one boot — QEMU 87844 — opened `export.pdb` to
offset 126976/217088, no error modal). But **most fresh boots FAIL**: rbp shows
`USB Error. Remove the device.` / the browse screen reads `NO USB`, and `export.pdb` is
never opened. It is a **load-sensitive race**, not a deterministic fix.

Key facts (all verified live):
- The device is correctly placed every time: slot `2-1.1`, mounted `/media/usb2/sda1`
  (slot-specific, so the kernel's Pioneer slot-connect DID fire). Kernel mounts it fine
  (`sda1`, SCSI attached). So the rejection is **100% rbp-side**.
- rbp rejects **pre-scan**: the library scanner access check (`FUN_0024cb38` @ `0x24cf14`)
  **never fires** on insert — the "USB Error" is raised before any library read.
- **Not caused by the MAIN-UCOM key work (patch 09):** a clean 01-08 build (no patch 09)
  reproduces the failure identically.
- `full-speed` enumeration is NOT the trigger (87844 loaded while full-speed too).

Localized to **`UsbCautionManager` (`FUN_00292d6c`)** — it opens `/proc/udev_usbctn1|2`
(via `FUN_002af4d0`) and owns the `{fd1,fd2}` at `+0x18`; its main vtable is `@0x428598`
(methods at VAs `0x292c6c/0x292d50/0x292924/0x292818`). **But those vtable methods never
fire on insert** — the event is read in a separate thread / task-run not yet found. A
coordinated fresh-boot trace (bps set before the first insert) caught nothing in them.

**Next:** find the `udev_usbctn2` read loop (NOT the vtable methods) — trace the
`pthread_create` the caution manager spawns, or breakpoint the blocking `read()` on that
fd — and read the event bytes to see if the kernel posts a "caution" event or rbp branches
on a timing/state condition. See [[usb-error-investigation]] memory for addresses + the
REA bare-hex=file-offset (+0x8000) quirk.
