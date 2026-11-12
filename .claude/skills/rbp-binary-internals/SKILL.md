---
name: rbp-binary-internals
description: Orientation map of the XDJ-RX main application binary `rbp` - how to analyze it with REA/Ghidra, its key subsystems (sub-ucom panel protocol, GPIO/GpioManager, MixerEngine audio graph + routing, switch-state, settings), the anchor function addresses, the C++/ABI conventions, and the live-debugging (gdbstub/RSP) workflow and its pitfalls. Use when reverse-engineering any rbp behavior, locating a subsystem, or setting up firmware analysis.
---

# `rbp` internals (XDJ-RX main app)

`rbp` is the XDJ-RX application: **ARM32 (EABI5, soft-float), stripped, non-PIE ELF**, loaded
low (`.data`/`.bss` globals at fixed VAs like `0x0107xxxx`). Analyzed copy: `build/extract/rbp`
(sha256 `4f62aa71ef8c7246536e87950939cc081b1850b22502a3780851c20c35b18dd7`). It is a large C++
(GNU libstdc++) program. **Do not patch it** — the project boots stock `rbp` so custom firmware
can be tested.

## Analyze with REA (Ghidra provider)
`open_binary(path=build/extract/rbp, provider_id="ghidra")`. Useful ops: `procedure_pseudo_code`,
`procedure_callers`, `xrefs` / `find_xrefs_to_name`, `search_strings`, `read_bytes`,
`resolve_containing_procedure`, `list_procedures`.
- **File-offset quirk:** a bare hex arg resolves as a **VA** if a procedure is defined there,
  else as a **file offset** (VA = file_off + 0x8000). If `resolve_containing_procedure` says
  "not in procedure" but bytes decode as code, Ghidra just didn't define that function.
- **Huge functions** exceed the result limit and are saved to a `tool-results/*.txt` file;
  extract with `python3 -c "import json; …"` / `jq` and grep the `raw_result.value`.
- Indirect-only (vtable) call targets aren't in the call graph — `procedure_callers` returns
  empty for a virtual method; note "vtable-dispatched" and switch to live tracing.

## Subsystem map (anchor addresses — stable for this binary)

**Sub-ucom panel protocol** (100-byte SPI frame; see the map-subucom-* skills)
- `PanelComController` ctor `FUN_0021a4ac` (vtable `DAT_00420700`); frame decoders `~0x282000-0x28b000`.
- Center/mixer code dispatcher **`FUN_0026ae7c`**; encoder event ctor `FUN_002adfa0`.
- Deck-button code handler is **NOT** `FUN_00222188` (bp there never fires) — still unpinned.
- Analog block decoder `0x282800` (EQ/TRIM/FADER/COLOR), continuous-analog `FUN_002adee8`.

**GPIO / `common::GpioManager`** (`/dev/gpiodrv`; see [map-gpio-control])
- Ctor `FUN_0002be28(obj, gpio, dir, period, callback, 0x1e)` — dir 0=in/1=out, callback=observer.
- Reader/dispatch `FUN_0002bd6c`, poll thread `FUN_0002bc18`, set-direction `FUN_0002c058`,
  read-level `FUN_0002c0fc`. Object: `+0x54` gpio, `+0x58` level, `+0x5c` fd, `+0x60` dir, `+0x64` observer.

**Audio: MixerEngine + playengine** (see [deck-playback-audio-gated] memory, audio docs)
- Engine ctor `FUN_00065350`; master section `FUN_00066688` (owns GpioIn 117/118 = **outputs**).
- Channel strips: input chans `FUN_00084e20(obj, ch, deckSrc=ch, lineSrc=ch+3, graph)`, `FUN_000849e8`, `FUN_00085e40`.
- **Routing tables:** source table `DAT_0107ac88` (7 slots `{buf,idx,flag}`, stride 12), channel
  table `DAT_0107acdc` (6 ch). Router `FUN_00080a74(src_idx, chan)`, graph init `FUN_00080920`
  (seeds routed=7/empty), `setChannelSource FUN_00085d44`, per-frame apply `FUN_00085a10` (routes
  from channel obj `+0x254` requested / `+0x250` applied). Deck→channel = `ch_n→src[n]`, ext/line = `src[n+3]`.
- Setters: `setChannelFader FUN_00083fcc`, `setMasterVolume FUN_00066bb8`, EQ `FUN_000836dc`,
  level `FUN_00083df8`, crossfader `FUN_00066c2c`. **On-air gate `FUN_00065028`** (master>22,
  level, EQ≠0, fader≥6, xfader) — all must pass or output is silent.
- Deck Player render `FUN_00075eb0`; mixdown `FUN_00043a2c` / top render `FUN_000506b0`.

**Hardware switch-state + SW TEST** (config switches)
- SW TEST page `FUN_002404ac` reads switchState `*(page+0x44)`: `+0x9c`/`+0xac` = DECK-SELECT
  (source) ch0/ch1, `+0xa0`/`+0xb0` = EXT TYPE (LINE/PHONO) ch0/ch1, `+0xa4` FADER CURVE, `+0xa8` MIC.
- Config-switch/EXT-TYPE handler `FUN_0023b5e8` (reads GPIO 119/120 → `+0xa0`/`+0xb0`); lives under
  `ISetting::resetValues` (`FUN_00282630`). The DECK-SELECT source is driven by the sub-ucom frame
  bit (rbp[0x33].7 / rbp[0x43].7), applied via the vtable to the channel `+0x254`.

**Misc:** logging/trace `FUN_00042234` (printf-like); `_Znwj` = `operator new`; `__aeabi_atexit`
for static dtors; control-name strings cluster `0x42b3a4-0x42b454`; "DECK SELECT"/"INPUT" UI strings `0x422f40`/`0x3a3620`.

## C++ / ABI
- **Soft-float**: float args/returns are IEEE-754 **bit patterns in core registers**
  (`1.0f`=`0x3f800000`, `0.5f`=`0x3f000000`, `0.4f`=`0x3ecccccd`). No VFP regs in the ABI.
- libstdc++ `std::string` (COW `_Rep`), vectors (grow `n + n/2 + 8 & ~7`). Many controls are
  objects that **track a previous value** (`+0x74`) to compute a delta.

## Live debugging (QEMU gdbstub, raw RSP)
- gdbstub on `tcp::1234`; raw RSP client `tools/xdj-panel/debug/rsp.py` (`read_mem/write_mem`,
  `Z0`/`Z2` bp+watch, `cmd("D")` detach). `tables.py` dumps the routing tables.
- **EFAULT on rbp VA globals** unless the halt lands in rbp's CPU context — retry a few times, spaced.
- **Never hammer the port**: ~25 rapid halts trip rbp's real-time watchdog → crash + splash restart.
- After any inferior call, **resume via the monitor `cont`** (`mon.py`), not a bare gdb continue.
- Breakpoints during early boot/real-time init can wedge rbp (it retries the constructor route in a
  tight loop) — prefer post-boot, spaced, or hardware bp.

## References
`docs/rbp-and-panel-protocol.md`, `docs/subucom-frame-map.md`, `docs/audio-handover.md`,
`docs/audio-silence-handover.md`, `docs/version-display-and-override.md`. Memories:
[deck-playback-audio-gated], [input-source-switch], [deck-line-routing]. Tools: `tools/xdj-panel/debug/`.
Skills: [map-gpio-control], [map-subucom-button], [map-subucom-fader], [map-subucom-knob],
[map-subucom-rotary], [upd-firmware-format], [run-xdj-emulator].
