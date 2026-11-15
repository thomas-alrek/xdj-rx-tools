# UTILITY → VERSION No. shows `Ver --.--` — where it comes from, and the fix

## Symptom

In the on-device menu **UTILITY → VERSION No.**, the SD-boot emulator (the
standalone app before it moved to NAND, and `qemu/watch.sh`) showed:

```
Ver --.--
```

A real XDJ-RX on firmware 2.21 shows `Ver 2.21`.

## Cause: no `release` in the u-boot env

The version is **not** reported by rbp or the panel MCU. It comes from the u-boot
env, which the **firmware updater** writes. The chain:

1. **apl_start** (rootfs init script) at boot:
   ```sh
   release=`fw_printenv -n release`
   if [ "$release" = "" ]; then release="--.--"; fi
   ...
   echo "$release" > /tmp/smdj.rev
   echo "$release [$rev_string]" > /tmp/smdj2.rev
   ```
   On an SD boot, `$rev_string` is `<rbp.rev LCR>:<system.rev LCR>:<sub rev>`
   ("Last Changed Rev" from `/root/pioneer/rbp.rev` and `/etc/system.rev`; the
   sub rev from `fw_printenv -nn rev_panelcom`/`rev_dispcom`). On a NAND boot it is
   built from the env `rev_*` variables instead.
2. **rbp** reads `/tmp/smdj2.rev` when the VERSION screen opens
   (`FUN_00223140` case 5; `/tmp/smdj2.rev` literal @ `0x418dbc`) and shows the
   part before `[`. rbp only reads these files and never writes them.
3. **`fw_printenv`** picks its env device from **SRC_SBMR1** (`0x020D8004`)
   BOOT_CFG1[7:4]: ≥ 8 → `NAND /dev/mtd2 0x0` (size `0x20000`), otherwise
   `SD /dev/mmcblk0 0xC0000` (size `0x2000`) (`/etc/fw_env.config`, `get_boot()`
   in fw_printenv). QEMU sets the straps from `XDJ_BOOT_MEDIA` (`xdj-board.c`,
   `xdj_src_sbmr1`).
4. The SD image's env (`qemu/build-nand.py`) originally held only the SD-boot
   selectors (`rootfs=sd`, `mmcdev`, …). With no `release`, the script fell back to
   `--.--`. The rendezvous file held `--.-- [4206:4195:]`: rbp.rev LCR 4206,
   system.rev LCR 4195, and an empty sub rev.

### Who writes `release` on a real unit: the updater's `finEnv()`

The updater (`/update` in the UPD) runs `getRevision(sys, apl, sub)` then
`finEnv(release, sys, apl, sub, …)`:

| env var | value | source in the UPD |
|---|---|---|
| `release` | `2.21` | `head -n 1 images/release.txt` |
| `rev_boot`, `rev_kernel`, `rev_rootfs`, `rev_data`, `rev_user` | `4195` | `images/system.rev` "Last Changed Rev" |
| `rev_pioneer` | `4206` | `images/rbp.rev` "Last Changed Rev" |
| `rev_panelcom` (ALL) / `rev_subucom` (user update) | `0.35` | `images/subucom.rev`, newlines stripped |

- **ALL update** (storage arg = NAND): `cp images/fw_env.txt /tmp/fw_env.txt`,
  appends `echo <k>=<v>` lines, then `fw_initenv /tmp/fw_env.txt NAND`. This path
  names the sub version **`rev_panelcom`**.
- **User update**: `Shell::setEnv` (fw_setenv) for each variable. This path names
  the sub version **`rev_subucom`**, which is what the updater's "current SUB
  version" (`????` when unset) reads.

`getRevision()` falls back to `????` for a missing or unparsable file.

## Fix (emulator side, no rbp/rootfs patch)

- **NAND boot** (the standalone app, and `qemu/xdj_nand.py` provisioning +
  `XDJ_BOOT_MEDIA=nand`): the real updater's `finEnv` writes these variables into
  mtd2 during the ALL update that provisions the unit (`standalone-app.md` §4a).
  Nothing extra is needed.
- **SD test bed** (dev only: `qemu/watch.sh`, `relaunch.sh`; the app no longer
  uses it): `qemu/build-nand.py` (`rev_env_entries`) derives the same variables
  an ALL update writes from the ISO's `images/{release.txt,system.rev,rbp.rev,
  subucom.rev}` and adds them to the SD env at `0xC0000`. Expected
  `/tmp/smdj2.rev`: `2.21 [4206:4195:0.35]`, screen `Ver 2.21`.

## Overriding the displayed version (custom firmware)

Set `release` in the u-boot env: `fw_setenv release X.YY` on a running guest, or
edit `rev_env_entries` / the NAND env. It takes effect on the next boot, because
apl_start writes `/tmp/smdj2.rev` once. For a one-off without rebooting, rewrite
`/tmp/smdj2.rev`; rbp reads it each time the screen opens.

The sub version shown in **test mode** ("SUB Ver.") is separate. It comes from
frame bytes 2–3, which the harness parses from the emulated MCU flash
(`XDJ_PANELCOM_IMG`); see `update-path-plan.md`.

## History

An earlier version of this note blamed the missing sub-ucom StaticInfomation
handshake and described the `[4206:4195:]` numbers as panel-MCU model/ID. Both
were wrong: the handshake object is only a lazy cache, and those numbers are the
svn "Last Changed Rev" values of rbp and the system.
