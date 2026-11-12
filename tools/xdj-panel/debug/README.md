# XDJ-RX emulator debug tooling

Raw helpers for poking the running emulator over its control sockets and the QEMU gdbstub,
**without a host gdb** (macOS has no ARM gdb). Built while tracing the DECK/LINE routing — see
`docs/deck-line-switch-handover.md` and `docs/subucom-frame-map.md`.

Endpoints the debug launcher exposes: monitor `/tmp/d_mon4.sock`, guest serial `/tmp/d_con4.sock`,
gdb RSP `tcp::1234`. Audio debug (`XDJ_AUDIO_DEBUG=1`) prints `[sdma-au] master=.. peak=..` to the
launcher's stdout — `peak>0` means the master bus has signal.

## Files
- **`relaunch.sh`** — launch the emulator in debug mode (single-core, cocoa window, the three sockets,
  audio-debug). Hub-only USB (do the post-boot `device_add` + `touch /tmp/usb2_insert`, per
  `docs/usb-mount-howto.md`). Boots in ~30–60s; occasionally hangs — relaunch if no `[sdma-au]` after ~90s.
- **`rsp.py`** — minimal GDB-RSP client (ARM LE). Library + CLI:
  - `RSP().read_mem/write_mem/rd32/wr32` — guest memory R/W (virtual addresses; works while rbp is the
    current MMU context, which it is at any halt).
  - `read_reg(n)` (r0–r15), `set_bp/clear_bp` (Z0), `set_watch/clear_watch` (Z2), `cont_wait(timeout)`,
    `cmd("D")` to detach+resume, `cmd("?")` status, single-step via `send("s")`.
  - CLI: `rsp.py words <addr_hex> <count>`, `rsp.py wr32 <addr> <val>`, `rsp.py mem <addr> <len>`.
  - NB: **connecting halts the CPU**; always `cmd("D")` (or let a script's finally do it) to resume.
- **`tables.py`** — dump the audio **source table** (`0x0107ac88`) and **channel table** (`0x0107acdc`)
  then detach. The deck-channel rows (`ch[0]`/`ch[1]`) show `routed=` 0/1 for DECK, 3/4 for LINE.
- **`inj.py`** — sub-ucom injects that **preserve the analog-gain holds** in `/tmp/subucom_ctrl`:
  - `inj.py base` — assert the gain holds (CH1 fader/master/xfader/EQ/trim up).
  - `inj.py pulse 'b 5d 10' [dwell]` — one-shot on top of the holds (e.g. browse PUSH `b 5d 10`,
    LOAD1 `b 5d 40`, PLAY `b 13 40`).
  - `inj.py mainkey 158 [dwell]` — momentary MAIN-UCOM GPIO (158=USB2 src, 160=USB1, 159=rekordbox,
    161=MIDI). **Momentary only** — not usable for the DECK/LINE *slide* switch (needs a held level).
- **`mon.py`** — one HMP monitor command (`python3 mon.py "info usb"`; `device_add …`; `screendump …`).
- **`ser.py`** — guest console: logs in as root, runs a command, returns output (serial is lossy under
  TCG; the helper types slowly and is still occasionally garbled — prefer gdb/monitor where possible).

## Typical routing check (DECK/LINE work)
```
./relaunch.sh &                      # wait for [sdma-au] in its stdout
python3 tables.py                    # baseline: ch0.routed=3 (LINE)
# ... drive the switch / load a deck ...
python3 tables.py                    # ch0.routed=0 (DECK) == audible when gains are up
```
