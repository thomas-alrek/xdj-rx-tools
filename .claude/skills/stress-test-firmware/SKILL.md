---
name: stress-test-firmware
description: Stress test an XDJ-RX custom firmware (3.00 and later) in the emulator for hours - tools/test/stress.py drives every feature at random (browse/load, play, jog, hot cues A-H, beat jump, UTILITY settings, stick swaps while playing, [FOLDER]/Contents, a 10,000-track library, power cycles, safe starts), records each crash with console + screenshot + last actions, and keeps going until a deadline. Also the generated test sticks (tools/test/make-test-sticks.sh) and how to triage a recorded failure. Use when asked to stress/soak test firmware, before a release, to (re)build the test USB images, or to explain a build/stress/crashes entry.
---

# Stress testing custom firmware

Reference: `docs/testing.md` (§ test sticks, § stress.py). Mod test loop and private host:
skill `firmware-mods`.

## Setup
1. Sticks (once; git-ignored, synthetic audio only): `tools/test/make-test-sticks.sh --big`
   → `test/usb/gen_lib_exfat.img`, `gen_lib_fat32.img`, `gen_flac.img`, `gen_wav.img`,
   `gen_big10k.img`. Needs ffmpeg, flac, baken (macOS). ~1 min; ~4 min with `--big`.
   A missing image is just skipped by the stress test (no `--big` → no 10k scroll action).
2. A **private host** on a throwaway unit with the firmware installed (never the app's unit):
   `XDJ_FW_DIR=build/T/fw XDJ_RUNTIME_DIR=build/T/run XDJ_PANEL_PORT=18765
   XDJ_VNC_WS_PORT=16080 app/host/target/release/xdj-host`, install the `.UPD`
   (`POST :18765/install-update`), power-cycle, see `### xdjmod: loaded into rbp`.
   Opt-in features worth covering (BEAT JUMP, HOT CUE E-H) are switched on by the run itself
   (the `setting` action changes UTILITY rows at random).
3. Close REA/Ghidra and other heavy jobs: TCG is load-sensitive.

## Run
```
python3 tools/test/stress.py --until 08:00 --unit build/T/fw [--port 18765] [--seed N]   # background it
python3 tools/test/stress.py --safe-starts 5 --unit build/T/fw                          # safe start only
```
Run it as a background task (it blocks for hours); read `build/stress/report.md` for status
(actions per kind, boots, safe starts, crashes, xruns) instead of the raw log. Python needs
Pillow. `--out DIR` for a separate record (default `build/stress`, appended across runs:
clear it for a fresh report).

## Triage a recorded failure (`build/stress/crashes/NNN/`)
- `reason.txt` - what tripped (console crash text / rbp not running / power off / no frames
  45 s / console restarted / safe-start marker missing / mods loaded on a safe start) and the
  last 60 actions. `console.log` - the boot's whole console. `screen.png` - the screen then.
- Decide **firmware vs emulator vs harness** before calling it a bug:
  - crash text from rbp (`Segmentation fault`, `relocation error`, `terminate called`,
    `edbmalloc NG`) right after an action → firmware/mod; find the mod from the action and
    the `### xdjmod:` lines; reproduce with the same `--seed`.
  - boot steps before `#### Application Start!! ###` (stock `apl_start`) → not the mods.
  - Known emulator effects: the unmodelled USB hub (I2C `0x2c`) can stall a boot for minutes
    on a busy host (`docs/emulator-status.md`); ALSA xruns are normal (counted, not failures);
    test-mode flake ~1 in 8 (unrelated to stress).
- Reproduce before reporting: time the step (`curl -X POST -d on :18765/power` + watch the
  console), or re-run the action.

## Rules
- Keep testing after a failure (the script power-cycles and continues); report every entry with
  its cause and whether it needs a firmware fix.
- Never on the user's app unit; nothing on real hardware without an emulator run first.
- The sticks are regenerated, never committed (`test/usb/*.img` is ignored); keep fixtures free
  of personal or third-party audio.
