# Testing: what checks exist and when to run them

| check | covers | run | time |
|---|---|---|---|
| `cd crates/xdj-fw && cargo test --release` | the firmware codecs against the real 2.21 image: ISO / cramfs / tar round trips, a no-change build reproducing every file, overlay build, unpack → edit → repack | after touching `crates/xdj-fw` | ~5 s |
| `tools/xdj-panel/test_panel_parity.py` | the in-process Rust panel (`app/host/src/panel.rs`) vs `server.py`: every control snapshot, HTTP reply, LED and log line for one scripted session | after touching either panel server | ~30 s |
| **`tools/test/regress.py`** | the whole thing, live, on a throwaway unit (private `$HOME`, runtime dir, ports) | before merging anything that touches QEMU, the host, the panel or firmware handling | ~8 min |

## `tools/test/regress.py`
`--app dist/XDJ-RX.app` tests the **packaged** app (its host, relocated QEMU and panel files) instead of
the build tree. Stages (`--only` picks a subset; `--unit DIR` clones an installed firmware dir instead of
provisioning; `--keep` keeps the work dir; on failure it is kept anyway, with `host.log`,
`run/console.log`, `run/qemu.log` and every screendump):
- **prep** — `xdj-host prep` of the firmware zip.
- **provision** — a blank unit installs itself (ALL update): env `release=2.21`, the "update
  complete" screen.
- **boot** — normal NAND boot reaches the home screen.
- **panel** — MENU *held* 2 s opens UTILITY, a short MENU tap leaves it (a short tap on the home
  screen does nothing — not a bug); rbp's LED stream is live.
- **usb** — BROWSE, then a panel USB LOAD of `test/usb/*.img` on **USB1** (the browser shows USB1)
  → the stick's rekordbox library replaces "NO USB".
- **standby** — POWER off while rbp runs → the standby handshake (rbp answers 'S', power cut < 10 s).
- **testmode** — File > Power On in Test Mode → VERSION INFORMATION (one retry for the known ~1-in-8
  rbp-side flake; the result notes it).
- **custom** — `tools/fw/make-test-upd.sh` builds TEST.UPD one release above what's installed,
  installs it with File > Install Firmware Update (`POST /install-update`) → env release, USER
  UPDATE + signature lines, the complete screen, both console markers, test mode right after.

Screens are classified from pixel statistics (`stats()`: black/white/colour/blue/red fractions,
printed with every result) — no reference screenshots, since the UI is Pioneer's. Run it with
nothing else heavy on the machine (single-core TCG boots are load-sensitive).
