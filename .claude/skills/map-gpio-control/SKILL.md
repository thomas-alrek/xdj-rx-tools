---
name: map-gpio-control
description: Find and drive an XDJ-RX control that rbp reads/writes as a GPIO (via /dev/gpiodrv) - source/input-select switches, line/phono switches, USB fault flags, top-panel source keys, and rbp's output control lines (mux commands). Use when a control is NOT a sub-ucom frame field (no fader/button/knob/rotary match) and you suspect a discrete GPIO level, or when deciding whether a pin is a drivable input vs an rbp-driven output.
---

# Mapping a GPIO control (rbp `/dev/gpiodrv` via `common::GpioManager`)

rbp talks to discrete lines through a char driver `/dev/gpiodrv` wrapped by the C++
class `common::GpioManager`. Every GPIO the app cares about is constructed by one
function; find them all, classify input vs output, trace an input's observer to the
state field it sets, then drive it emulator-side.

## 1. Enumerate every GPIO (firmware, REA)

Open `build/extract/rbp` with REA (`provider_id: "ghidra"`). The ctor is **`FUN_0002be28`**:

```
FUN_0002be28(obj, gpio_num, direction, period, callback, 0x1e)
```
- `gpio_num` = **Linux** gpio number. i.MX6 mapping: bank = `gpio>>5` (0-indexed; bank0=GPIO1),
  pin = `gpio & 31`. e.g. `0x75`=117 → GPIO4_IO21; `0x77`=119 → GPIO4_IO23.
- `direction`: **0 = INPUT** (a switch / detect rbp reads), **1 = OUTPUT** (a line rbp *drives* —
  e.g. an analog-mux command; **driving it emulator-side does nothing to rbp logic**).
- `callback` (param 5): non-zero = an **observer** object; on a level change the reader
  `FUN_0002bd6c` calls `observer->vtable[+8](observer, gpio_num, level)`. Zero = polled / no observer.

Run `procedure_callers(FUN_0002be28)` and read each caller's args. Inventory for rbp sha
`4f62aa71…` (addresses are stable for this binary):

| caller | gpios | dir | meaning |
|---|---|---|---|
| `FUN_00066688` (mixer master) | 117,118 | out | analog-mux command (NOT the switch input) |
| `_INIT_72` | 134,135 | out | global control lines |
| `FUN_0021a4ac` (PanelComController) | 162-165 | out | **source-key LEDs** rekordbox/MIDI/USB1/USB2 (active-high, one-hot; `xdj-board.c` appends them to the LED line as byte `0x14`) |
| `FUN_0024e834` (deck/Player ctor) | 126 / 204 | in | USB fault flag USB1/USB2 (GPIO4_IO30 / GPIO7_IO12, gpiodrv ids `0x7e`/`0xcc`), active-low (LOW on a mounted slot → "USB Error. Remove the device."; NOT insert-detect — insert with `device_add` only, `docs/usb.md`) |
| `FUN_0023b5e8` (ISetting/SwitchTestMode) | 119,120 | in | **EXT TYPE (LINE/PHONO) rear switch** ch0/ch1: `+0xa0/+0xb0 = (level != 0)` (`0x23bd1c`), and SW TEST shows `0`→LINE, `≠0`→PHONO (`0x2409d0`) ⇒ **HIGH = PHONO, LOW = LINE** (static; runtime path is code `0x1013` `LinePhonoSW`, not traced). Not emulated — no external audio. |
| `FUN_00274a38` | 119,120,146,149,158-161 | in | main-ucom top-panel keys (idle HIGH, pressed LOW): rekordbox 159, MIDI 161, USB1 160, USB2 158, DECK 1/2 149, TIME MODE 146 |

GpioManager object layout: `+0x54` gpio num, `+0x58` cached level (byte), `+0x5c` fd,
`+0x60` direction (1=output), `+0x64` observer. Helpers: `FUN_0002c058` = set direction
(writes a byte to the fd), `FUN_0002c0fc` = read level, `FUN_0002bc18` = poll thread,
`FUN_0002bd6c` = reader/dispatcher.

## 2. Trace an input GPIO to the state it sets

For a `dir=0` GPIO with an observer, read the observer's `vtable[+8]` handler (or the
code right after the `FUN_0002c0fc` read in the owner) to see **which state field** the
level writes. Example found this way: in `FUN_0023b5e8`, `FUN_0002c0fc(gpio119)` → writes
the switch-state object `+0xa0` (EXT TYPE ch0); gpio120 → `+0xb0` (ch1). That state object
is the one the SW TEST screen (`FUN_002404ac`) reads as `*(page+0x44)+0xNN`.

If an input GPIO's observer is null and the owner never reads it, the level is sampled
once (e.g. at init) — note that; a live drive then won't re-trigger.

## 3. Drive it emulator-side (`qemu/xdj/xdj-board.c` + `xdj-panel.c`)

`qemu/xdj/xdj-board.c` captures every i.MX6 GPIO bank in `g_xdj_gpio[8]`
(`xdj_imx6_gpio_post_realize`), and pins are driven with:
```c
qemu_set_irq(qdev_get_gpio_in(g_xdj_gpio[gpio>>5], gpio & 31), level); // level 1=HIGH 0=LOW
```
- **Momentary** keys: `echo <gpio> > /tmp/mainkey` (press = LOW; empty/removed = release HIGH),
  or the `xdjkey` chardev in socket mode (hook in `xdj-panel.c`; panel `POST /mainkey`).
- **Reset level** is set per bank via the `psr-reset` property in `xdj_imx6_gpio_pre_realize`
  (so the line is correct from power-on, before rbp samples it) — e.g. the key/fault pull-ups and
  the update-mode strap GPIO3_IO30 (`XDJ_BOOT_UPDATE=1`).
- For a **persistent/held level** (a slide switch) add a hook that drives the pin and does not
  auto-release, modelled on the `/tmp/mainkey` hook.

Edit `qemu/xdj/*.c` in the repo and rebuild with `qemu/build-qemu.sh` (syncs into the build tree +
ninja); never edit the copy under `build/qemu-src/` (skill [qemu-imx6-patches]).

### Mapping an rbp OUTPUT (e.g. an LED) — cheapest method
Read every bank's `DR`/`GDIR` over the monitor (`xp/1wx <base>` / `<base>+4`), perform the action
(press a key, change a mode), read again, and diff the `GDIR`-set bits. That is how 162–165 were
identified as the source-key LEDs. To expose an output to the panel, `qdev_connect_gpio_out()` the
bank's pin to a `qemu_allocate_irq()` handler (see `xdj-board.c`) — the i.MX GPIO model raises it on
every `DR` write while `GDIR` is set.
Static RE without REA: `pip install capstone pyelftools` in a venv and disassemble rbp (ARM mode,
non-PIE); resolve `ldr rX, [pc, #imm]` literals to find string/object references.

## 4. Live-verify (monitor + RSP)

Read the guest i.MX6 GPIO registers over the HMP monitor (`tools/xdj-panel/debug/mon.py`):
bank bases GPIO1..7 = `0x0209C000, 0x020A0000, 0x020A4000, 0x020A8000, 0x020AC000,
0x020B0000, 0x020B4000`; regs `DR +0x00`, `GDIR +0x04` (bit=1 → output), `PSR +0x08` (pad
status). `python3 mon.py "xp/1wx 0x020a8008"`.
- **GDIR gotcha:** if the pin's GDIR bit is 1 (output), PSR follows DR and `qemu_set_irq`
  on the input line is **ignored** — you cannot drive an rbp-output pin as an input.
- Confirm the control→effect link by A/B: boot with the pin at level A vs B (via `psr-reset`
  so it is set before rbp reads it), then read the target state/routing over RSP
  (`tools/xdj-panel/debug/tables.py` for the audio routing table). Flip = confirmed.

## Gotchas
- **Never hammer the RSP port.** ~25 rapid `tables.py`/gdb halts trip rbp's real-time
  watchdog → rbp crash + splash restart. Space reads ≥5 s; a single read is fine.
- RSP reads of rbp VA globals EFAULT unless the CPU halt lands in rbp's context; retry a
  few times spaced out.
- Linux gpio number ≠ a sub-ucom offset. GPIOs are a different plane from the 100-byte frame.

## References
- `docs/subucom-frame-map.md` ("Controls that are not in this frame"), `docs/usb.md`,
  `docs/rbp-and-panel-protocol.md`, `docs/subucom-led-map.md`, `qemu/xdj/xdj-board.c` +
  `qemu/xdj/xdj-panel.c` (the `/tmp/mainkey` hook).
- Tools: `tools/xdj-panel/debug/{mon.py,rsp.py,tables.py}`.
- Sibling skills: [map-subucom-button], [map-subucom-fader], [map-subucom-knob], [map-subucom-rotary], [qemu-imx6-patches].
