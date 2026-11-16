# USB test images

Put rekordbox-formatted USB disk **images** here (`*.img`, git-ignored —
they are your own content). QEMU uses an image directly as a raw `usb-storage` disk, so it must be a
whole-disk image (MBR + FAT32/exFAT partition), not a folder. `tools/xdj-panel/debug/relaunch.sh`
and `tools/test/regress.py` use the first `test/usb/*.img` (regress.py loads a copy).

## Making one
- From a rekordbox collection, without a stick: `tools/build-rb-usb.sh` (see its header).
- From a stick you exported to with rekordbox (macOS):
  ```sh
  diskutil list                      # find the stick, e.g. /dev/disk4
  diskutil unmountDisk /dev/diskN
  sudo dd if=/dev/rdiskN of=test/usb/my-stick.img bs=4m
  ```
- A raw `.dmg` (`hdiutil imageinfo` shows `Class Name: CRawDiskImage`) can be renamed to `.img`;
  a compressed/UDIF one must be converted first: `hdiutil convert in.dmg -format UDRW -o my-stick`.

## Using it
- Desktop app: *File → Load USB1/USB2 Image…* (USB1 is what the browser shows; MASTER REC records
  to USB2). Slots are read/write: writes, including recordings, land in the image.
- `rekordbox_new_exfat.img`: the same content on an exFAT partition, for the exFAT mod (stock
  firmware only mounts FAT32/HFS+).
- `wav_test.img`: `rekordbox_new.img` + a folder of test tones in WAV formats stock firmware does and
  doesn't play (`tools/fw/mods/wav/make-wav-test-image.sh`; browse with [FOLDER]).
- Any files on a blank FAT32 stick, no library: `tools/make-fat32-image.sh OUT.img FILE...`.
- Dev: see [docs/usb.md](../../docs/usb.md) (insert after rbp is polling).
