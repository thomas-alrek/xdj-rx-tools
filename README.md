# Pioneer XDJ-RX firmware — reverse engineering notes

Goal: understand the XDJ-RX firmware structure, architecture, and update
mechanism, with a view to **adding exFAT USB support** and, as a first
milestone, **booting the firmware in QEMU**. Work is done on hardware the owner
possesses, using Pioneer's own publicly-published GPL source, for
interoperability and preservation.

- **Device:** Pioneer XDJ-RX (internal platform name **`XDJAERO`**)
- **Firmware analysed:** `2.21` (built 2017-10-18, the last XDJ-RX release)
- **SoC:** NXP/Freescale **i.MX6Q** (`MCIMX6U6…`), dual Cortex-A9 @ 800 MHz
- **Kernel:** Linux **3.0.35** (Freescale BSP `L3.0.35_4.1.0`), U-Boot 2009.08
- **Storage:** GPMI NAND (layout below), plus the panel microcontroller

---

## 1. The `.UPD` file format (fully reverse-engineered)

`XDJRX.UPD` is an **ISO9660 image encrypted with Linux `cryptoloop`** (the
`losetup -e aes` scheme), followed by a small plaintext footer:

```
+-------------------------------------------------------------+
| encrypted body : N × 512 bytes   (cryptoloop AES-256-CBC)   |
+-------------------------------------------------------------+
| model string   : 12 bytes        "XDJAERO2.21\0"            |
| body CRC32      : 4 bytes, little-endian  = crc32(body)     |
+-------------------------------------------------------------+
```

For 2.21: `48,363,536` bytes = `94,460 × 512` (body) + `16` (footer).

### Crypto parameters (verified against source, not guessed)

| Parameter | Value | Source of truth |
|-----------|-------|-----------------|
| Cipher / mode | `cbc(aes)`, **AES-256** | `linux-3.0.35/drivers/block/cryptoloop.c` (`cms` → `cbc(aes)`) |
| Key | 32 bytes: first line of `aes256.key`, bytes **`[0:31]` + one `0x00`** | `util-linux-ng-2.13/mount/lomount.c` + `include/xstrncpy.h` (`strncpy(dst,src,31); dst[31]=0`), `LO_KEY_SIZE=32` |
| Per-sector IV | **`LE32(sector_index) ‖ 12×0x00`**, incrementing every 512 B from body start | `cryptoloop.c` (`iv[0]=cpu_to_le32(IV)`) + `loop.c` (`IV = offset>>9`, offset 0) |
| Footer CRC | `zlib.crc32(encrypted_body)`, stored little-endian | measured, byte-exact |

The on-device decrypt routine that reveals all of this is
`initramfs/decrypt_update.sh`:

```sh
cat /usr/local/pioneer/aes256.key | losetup -e aes -p 0 $dev $filename
mount $dev /mnt/iso          # inner volume is ISO9660, label "UsbAuto"
./update USB ${language}      # run the updater from inside the image
```

### Where the key comes from (and why this is legitimate)

The XDJ-RX runs embedded Linux, so Pioneer must publish the GPL/LGPL source.
That public release **contains the key**:

> `pioneer_xdj_rx.tar.gz` → `pioneer_xdj_rx/initramfs.tar.gz` →
> `initramfs/usr/local/pioneer/aes256.key`

Download: Pioneer "GNU Open Source License & GPL/LGPL Source Code" page →
**XDJ-RX** (v1.00 / 1.40 / 1.50). The parts are `deflate64` ZIPs wrapping
`pioneer_xdj_rx.tar.gz.00` / `.01`; concatenate then `tar xzf`. The AES key is
unchanged across firmware versions, so the 1.50-era key decrypts 2.21.

**The key is never stored in this repo.** [`fetch_key.py`](fetch_key.py)
resolves it on demand and writes it to `.env` (git-ignored) as
`XDJRX_AES_KEY=<64 hex>`. It uses a local `aes256.key` if present, otherwise
downloads the GPL parts, unwraps the deflate64 ZIPs, streams the two nested
tars, extracts `aes256.key`, and derives the 32-byte key. Both `.env` and
`aes256.key` are listed in `.gitignore`.

---

## 2. Reusable tool — `xdjrx_upd.py`

Pure-Python, one dependency (`pip install pycryptodome`, or `cryptography`).

```bash
# First time only: resolve the AES key into .env (see §1)
python3 fetch_key.py
#   fast if ./aes256.key exists; otherwise downloads ~280 MB of GPL source
#   (that download path also needs: pip install inflate64)

# The .UPD argument may also be the Pioneer-shipped .zip that contains it —
# the tool finds and unwraps the .UPD member automatically:
#   python3 xdjrx_upd.py info  XDJ-RX_v2.21.zip
#   python3 xdjrx_upd.py decrypt XDJ-RX_v2.21.zip -o firmware/XDJRX_2.21.iso

# Inspect (verifies key + footer CRC, finds the ISO9660 magic)
python3 xdjrx_upd.py info XDJRX.UPD

# Decrypt to an ISO you can mount/inspect
python3 xdjrx_upd.py decrypt XDJRX.UPD -o firmware/XDJRX_2.21.iso

# Decrypt AND unpack the ISO tree (uses 7z / bsdtar, or hdiutil on macOS)
python3 xdjrx_upd.py extract XDJRX.UPD -d firmware/contents

# Re-pack a modified ISO into a flashable .UPD (re-encrypt + fix footer CRC)
python3 xdjrx_upd.py pack modified.iso -o XDJRX_custom.UPD --model XDJAERO2.21

# Use your own key file instead of the built-in key
python3 xdjrx_upd.py --key-file aes256.key decrypt XDJRX.UPD
```

`pack` is byte-exact: re-packing the untouched ISO reproduces the original
`.UPD` bit-for-bit — so a modified image will be a valid update file.

> ⚠️ Flashing a custom `.UPD` to real hardware is at your own risk. The device
> has **A/B kernel banks** (see below) which gives some brick resistance, but
> there is no guarantee. Validate in QEMU / on a sacrificial unit first.

---

## 3. What's inside the firmware (`firmware/contents/`)

Decrypted ISO (volume `UsbAuto`) — the updater and everything it flashes:

| Path | What it is |
|------|------------|
| `update` | ARM ELF updater (dynamically linked, not stripped) — the update orchestrator |
| `mot2bin` | Motorola S-record → binary converter (for the panel MCU image) |
| `lib/`, `usr/lib/`, `gui/` | shared libs + GUI assets used by `update` itself |
| `images/uImage` | **Linux kernel** — `Linux-3.0.35-2666-gbdde708`, ARM, uncompressed, load/entry `0x10008000` |
| `images/u-boot.bin.nand` | U-Boot bootloader (NAND build) |
| `images/rootfs.cramfs` | **root filesystem**, CRAMFS, ~17 MB, 1075 files (contains `fuse.ko`, `libfuse`, `fusermount`, udev rules) |
| `images/pioneer.tar.gz` | Pioneer application payload |
| `images/gui.tar.gz` | GUI resources |
| `images/user.tar.gz`, `images/data.tar.gz` | user/data overlays |
| `images/PANELCOM.mot` | **panel microcontroller** firmware (S-record) |
| `images/fw_env.txt` | U-Boot environment (boot flow + NAND partition map) |
| `images/release.txt`, `*.rev` | version `2.21`, SVN build provenance |

### Boot flow & NAND layout (from `fw_env.txt`)

```
bootcmd = run bootcmd_nand
bootcmd_nand = run bootargs_base bootargs_nand bootargs_nand_${kernel_bank}; bootm
  → nand read 0x10800000 0x500000 0x500000   (kernel bank A @ 5 MiB)
bootargs = console=ttymxc0,115200 ... mtdparts=... consoleblank=0 video=mxcfb0 rootwait
```

NAND partitions (`mtdparts`):
`3M(boot) 1M(bb1) 1M(env) 10M(kernel_a) 10M(kernel_b) 30M(rootfs) 10M(data)
8M(pioneer) 5M(bb2) 10M(core) 40M(gui) 122M(quickboot) 5M(bb2) 1M(bbt)`

Notes:
- **Dual kernel banks A/B** → safer updates (fall back if one is bad).
- U-Boot env also defines **MMC** (`rootfstype=ext4`) and **NFS** boot paths —
  the NFS path (`root=/dev/nfs nfsroot=…`) is the most practical for dev/QEMU.
- Console is **`ttymxc0` @ 115200**, 8N1.

---

## 4. exFAT support — feasibility

**USB drives are mounted as `vfat` only.** The updater's mount helper
(`initramfs/mount_usb.sh`) does `mount -t vfat …`; the main app mounts USB media
via a udev rule `12-usb-memory-auto-mount.rules` in the rootfs. The kernel
(3.0.35, 2012) predates mainline exFAT (5.4, 2019) — **no native exFAT** and
`NTFS`/`exFAT` are both off in the config. But:

- `CONFIG_FUSE_FS=m` **and the rootfs already ships FUSE** (`fuse.ko`,
  `libfuse.so`, `fusermount`, `fuse.conf`, `99-fuse.rules`). The device already
  uses FUSE (e.g. `simple-mtpfs`).

**Recommended route: userspace `fuse-exfat` (relan/exfat).**
1. Cross-compile `fuse-exfat` (`mount.exfat-fuse`) for armv7-hf against this
   BSP's toolchain/libfuse.
2. Inject the binary + libs into `rootfs.cramfs` (rebuild the cramfs).
3. Patch the mount path (the udev rule + any `mount -t vfat` call sites) to try
   `exfat` when `vfat` fails — the natural hook is
   `12-usb-memory-auto-mount.rules`.
4. Re-pack with `xdjrx_upd.py pack`.

Alternative: backport a kernel exFAT driver (Samsung `sdFAT` / staging `exfat`)
as a `.ko` for 3.0.35 — faster at runtime, but a bigger porting job and needs a
kernel rebuild. Start with FUSE.

> Open question worth checking early: whether the Pioneer app enumerates the
> USB filesystem itself (and only understands FAT) rather than just relying on
> the mounted path. If so, the app's expectations — not just the mount — may
> need attention. Inspect `pioneer.tar.gz` + the `update`/app binaries next.

---

## 5. QEMU boot — feasibility

**Promising.** The 2.21 kernel is built with `CONFIG_SOC_IMX6Q=y` and
**`CONFIG_MACH_MX6Q_SABRELITE=y`** (among other i.MX6Q boards), and QEMU ships a
matching **`-M sabrelite`** machine. The `uImage` is an uncompressed ARM kernel.

First-milestone attempt (serial console to userspace):

```bash
# extract the raw kernel from the uImage (strip 64-byte U-Boot header)
dd if=firmware/contents/images/uImage of=zImage.raw bs=64 skip=1

qemu-system-arm -M sabrelite -m 1024 -nographic \
  -kernel firmware/contents/images/uImage \
  -append "console=ttymxc0,115200 rootwait" -serial mon:stdio
```

Realistic expectations / caveats:
- Kernel 3.0.35 is **pre-device-tree** for i.MX (board files + ATAGs/machine
  id). The real board is a **custom Pioneer board**, not SABRE Lite, so expect
  to boot the generic SABRE Lite path and hit missing/placeholder peripherals.
- Getting to a **serial console + shell** is a reasonable target. A *full* boot
  (GUI/GPU, panel MCU, audio codecs, jog wheels, deck hardware) is **not**
  achievable in stock QEMU — those peripherals don't exist there.
- For userspace work (e.g. testing the exFAT mount logic) the pragmatic path is
  often **`qemu-arm` user-mode + the extracted rootfs** (chroot-style), rather
  than full-system boot.
- Building a **matching kernel from the GPL source** (we have `linux-3.0.35` +
  `.config`) lets you enable QEMU-friendly options and debug output.

---

## 6. Repro: rebuilding from Pioneer's GPL source

```bash
# 1. Download XDJ-RX GPL parts (deflate64 ZIPs) from Pioneer's GPL/LGPL page.
# 2. Unwrap each ZIP (ditto/7z fail on deflate64; use python 'inflate64'):
pip install inflate64
#    -> pioneer_xdj_rx.tar.gz.00 / .01
cat pioneer_xdj_rx.tar.gz.00 pioneer_xdj_rx.tar.gz.01 > pioneer_xdj_rx.tar.gz
tar xzf pioneer_xdj_rx.tar.gz
tar xzf pioneer_xdj_rx/initramfs.tar.gz      # -> initramfs/usr/local/pioneer/aes256.key
```

Source tree of note:
`BUILD/linux-3.0.35/` (kernel + `.config`), `BUILD/u-boot-2009.08/`,
`BUILD/busybox-1.20.2/`, `tools/` (fuse, simple-mtpfs, util-linux, hfsprogs…),
`config/platform/imx/imx6q.cf`.

---

## Files in this folder

```
XDJRX.UPD              original encrypted firmware (2.21)
xdjrx_upd.py           decrypt / info / extract / pack tool (reads key from .env)
fetch_key.py           resolve the AES key from Pioneer's GPL source -> .env
README.md              this document
.gitignore             excludes .env, aes256.key, firmware/ (Pioneer-owned / large)
.env                   XDJRX_AES_KEY=<hex>   (git-ignored; created by fetch_key.py)
aes256.key             passphrase file from the GPL source (git-ignored; cache)
firmware/
  XDJRX_2.21.iso       decrypted ISO9660 image (mountable)
  contents/            unpacked ISO tree (update, images/uImage, rootfs.cramfs, …)
```

## Status / next steps

- [x] `.UPD` format fully specified; decrypt + byte-exact re-pack working
- [x] Update mechanism, boot flow, NAND layout, component inventory mapped
- [x] exFAT & QEMU feasibility assessed (both viable, caveats above)
- [ ] Unpack `rootfs.cramfs` + `pioneer.tar.gz`; find the app's USB mount path
- [ ] QEMU `sabrelite` boot attempt → serial console
- [ ] Cross-compile `fuse-exfat`; patch mount logic; rebuild cramfs; re-pack
```
```
