# xdj-fw — XDJ-RX firmware tooling (library + `xdj` CLI)

Shared by the desktop app (`app/host`, which links the library) and the command line
(`xdj`). Replaces the Python prep scripts for everything except the dev SD-image builder
and the key fetch.

```
cargo build --release          # -> target/release/xdj
cargo test --release           # codec round trips against ../../build/XDJRX.iso
```

| Module | What |
|---|---|
| `firmware` | `.UPD`/`.zip` read, AES-256-CBC per-sector decrypt/encrypt + footer (`pack`), key lookup, kernel boot blobs (uImage → vmlinux, trampoline, ATAGs), the FAT16 update stick |
| `iso` | ISO9660 + Rock Ridge read (NM/PX/SL/CE) and write (SP/CE→ER, PX/NM/SL, both path tables) |
| `cramfs` | cramfs v2 read/write (sorted dirs, 4 KiB zlib blocks, superblock CRC) — the root fs |
| `tarball` | payload `.tar.gz` list + overlay rewrite keeping untouched headers |
| `tree` | the in-memory tree (POSIX metadata) the codecs share; overlay application |
| `fwbuild` | `unpack`, `build` (base firmware + overlay + release → new image), `diff_unpacked` (edited unpack tree → overlay, for `fw diff`/`fw repack`) |
| `nand` | the emulated NAND image (GPMI/BCH page layout, u-boot env, init / installed? / boot) |

## CLI
```
xdj fw info|decrypt|unpack|diff|repack|build|pack|stick ...
xdj prepare <ISO|UPD> [--out DIR]
xdj nand init|env|info|boot ...
xdj --help ; xdj fw build --help
```

## Custom firmware
Edit in place:
```
xdj fw unpack XDJ-RX_v221.zip build/u ; $EDITOR build/u/rootfs/... ; xdj fw diff XDJ-RX_v221.zip build/u
xdj fw repack XDJ-RX_v221.zip build/u --release 2.31 -o MY.UPD
```
Or a curated overlay (best practices: `docs/custom-firmware.md` §2a):
```
xdj fw unpack XDJ-RX_v221.zip build/unpacked          # browse; copy files to edit
mkdir -p ov/pioneer/pioneer && cp build/unpacked/pioneer/pioneer/apl_start ov/pioneer/pioneer/
$EDITOR ov/pioneer/pioneer/apl_start
xdj fw build XDJ-RX_v221.zip --overlay ov --release 2.30 -o MY.UPD
xdj fw stick MY.UPD stick.img                          # install via firmware update mode
```
Overlay parts: `iso/` (raw image files), `rootfs/` (rootfs.cramfs, mtd5), `pioneer/`
(pioneer.tar.gz, mtd7), `gui/`, `data/`, `user/`. Replace keeps the original mode/owner;
`.xdj-delete` lists removals. The update's `check_sig` is only CRC32 + the `XDJAERO`
model prefix (no signature). End-to-end example: `tools/fw/make-test-upd.sh`.

Parity with the Python dev tools: `qemu/test_rust_parity.sh`. Never commit firmware
contents (Pioneer-owned): build overlays from the unpacked image at build time.
