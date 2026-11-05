# USB test images

Drop a rekordbox-formatted USB disk **image** here as `rekordbox_test.img`
(git-ignored — it's large and your own content). QEMU uses it directly as a raw
`usb-storage` disk, so it must be a whole-disk image (MBR + FAT32/exFAT
partition), not a folder.

## Making one on macOS
A rekordbox "Export to USB" onto a real stick, then:

```sh
# find the stick (e.g. /dev/disk4), unmount, then image the whole disk:
diskutil list
diskutil unmountDisk /dev/diskN
sudo dd if=/dev/rdiskN of=test/usb/rekordbox_test.img bs=4m
```

A `.dmg` that is already a **raw** image (`hdiutil imageinfo` shows
`Class Name: CRawDiskImage`) can just be copied/renamed to `.img` — no
conversion. A compressed/UDIF `.dmg` must first be converted:
`hdiutil convert in.dmg -format UDRW -o rekordbox_test.img`.

## Attaching it
Cold-plug (hotplug does not enumerate on this board):
`XDJUSB=test/usb/rekordbox_test.img ./run.sh`  (wiring: see qemu/watch.sh).

Note: as of now, rbp crashes rendering rekordbox USB content (the galcore/GPU
2D path is stubbed); see docs/emulator-status.md feature D.
