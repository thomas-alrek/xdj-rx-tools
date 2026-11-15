# UTILITY → VERSION No.: where it comes from and how to override it

The menu **UTILITY → VERSION No.** shows the installed firmware release (`Ver 2.21` on a unit
running 2.21). It is not reported by rbp or the panel MCU: it is the u-boot env variable
**`release`**, written by the firmware updater.

## The chain
1. **apl_start** (rootfs init script) at boot:
   ```sh
   release=`fw_printenv -n release`
   if [ "$release" = "" ]; then release="--.--"; fi
   ...
   echo "$release" > /tmp/smdj.rev
   echo "$release [$rev_string]" > /tmp/smdj2.rev
   ```
   On a NAND boot `$rev_string` is built from the env `rev_*` variables. On an SD boot it is
   `<rbp.rev LCR>:<system.rev LCR>:<sub rev>` — the svn "Last Changed Rev" of
   `/root/pioneer/rbp.rev` and `/etc/system.rev`, and the sub rev from
   `fw_printenv -nn rev_panelcom`/`rev_dispcom`.
2. **rbp** reads `/tmp/smdj2.rev` each time the VERSION screen opens (`FUN_00223140` case 5;
   literal `/tmp/smdj2.rev` @ `0x418dbc`) and shows the part before `[`. rbp never writes these
   files.
3. **`fw_printenv`** picks its env device from **SRC_SBMR1** (`0x020D8004`) BOOT_CFG1[7:4]: ≥ 8 →
   `NAND /dev/mtd2 0x0` (size `0x20000`), otherwise `SD /dev/mmcblk0 0xC0000` (size `0x2000`)
   (`/etc/fw_env.config`, `get_boot()`). QEMU sets the straps from `XDJ_BOOT_MEDIA`
   (`xdj_src_sbmr1()` in `qemu/xdj/xdj-board.c`).

## Who writes `release`: the updater's `finEnv()`
The updater (`/update` in the UPD) runs `getRevision(sys, apl, sub)` then
`finEnv(release, sys, apl, sub, …)`:

| env var | value (2.21) | source in the UPD |
|---|---|---|
| `release` | `2.21` | `head -n 1 images/release.txt` |
| `rev_boot`, `rev_kernel`, `rev_rootfs`, `rev_data`, `rev_user` | `4195` | `images/system.rev` "Last Changed Rev" |
| `rev_pioneer` | `4206` | `images/rbp.rev` "Last Changed Rev" |
| `rev_panelcom` (ALL) / `rev_subucom` (user update) | `0.35` | `images/subucom.rev`, newlines stripped |

- **ALL update** (storage arg = NAND): copies `images/fw_env.txt` to `/tmp/fw_env.txt`, appends
  `<k>=<v>` lines, then `fw_initenv /tmp/fw_env.txt NAND`. Names the sub version `rev_panelcom`.
- **User update**: `Shell::setEnv` (fw_setenv) per variable. Names the sub version `rev_subucom`,
  which is what the updater reads as the "current SUB version" (`????` when unset).
- `getRevision()` falls back to `????` for a missing or unparsable file.

## In the emulator
- **NAND unit** (the desktop app; `xdj nand init` + ALL update + `XDJ_BOOT_MEDIA=nand`): the real
  updater's `finEnv` writes these variables into mtd2 while provisioning the unit, so the screen
  shows `Ver 2.21` with nothing extra
  ([firmware-update-and-nand.md](firmware-update-and-nand.md), [standalone-app.md](standalone-app.md)).
  `xdj nand env <img>` prints the env.
- **SD test bed** (dev only: `qemu/watch.sh`, `relaunch.sh`): `qemu/build-nand.py`
  (`rev_env_entries`) derives the same variables from the ISO's
  `images/{release.txt,system.rev,rbp.rev,subucom.rev}` and adds them to the SD env at `0xC0000`.
  Expected `/tmp/smdj2.rev`: `2.21 [4206:4195:0.35]`. Without a `release` the screen shows `--.--`.

## Overriding the version
- **Custom firmware** (the normal way): `xdj fw build <base> -o OUT.UPD --release X.YY` sets
  `images/release.txt`; installing it makes the updater write `release=X.YY`, and VERSION No. (and
  test mode's SYSTEM Ver.) show it after the next boot — [custom-firmware.md](custom-firmware.md).
  The updater compares this release with the installed one.
- **On a running guest**: `fw_setenv release X.YY` takes effect on the next boot (apl_start writes
  `/tmp/smdj2.rev` once). For a one-off without rebooting, rewrite `/tmp/smdj2.rev`; rbp re-reads
  it whenever the screen opens.

## SUB version (test mode)
The "SUB Ver." in test mode Mode 1 is separate: it comes from sub-ucom frame bytes 2–3, which the
harness (`qemu/xdj/xdj-panel.c`) parses from the firmware in the emulated MCU flash
(`XDJ_PANELCOM_IMG`; `0.00` with no firmware; `XDJ_SUBUCOM_VER=<hex>` overrides). See
[service-modes.md](service-modes.md#test-mode).
