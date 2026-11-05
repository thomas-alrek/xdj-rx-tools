# XDJ-RX debug console protocol (DebugSub / DebugMain)

rbp (the firmware app) ships a built-in **debug command console** that can drive
almost every function of the device — including simulating **any panel control**.
This is the mechanism we use to inject input into the emulator (automated testing,
and a quick interactive path that bypasses the gated SPI panel).

> Reverse-engineered from `rbp` (ARM, stripped) with REA/Ghidra + a QEMU gdbstub
> watchpoint trace. All addresses are rbp's fixed load VAs (non-PIE, base 0x8000).

## Transport

- `allinone_debug::DebugSub` (thread `DebugSub`, run loop `FUN_0002b260`) binds a
  **UDP socket on `127.0.0.1:20000`** and loops on `recvfrom`. It is live from
  app start. (Config via "DebugManager"; default port 20000. Also listening:
  UDP `0.0.0.0:50000/50001/50002` — separate servers, not this console.)
- Each received **text line** (`\r` or `\n` terminated) is wrapped into a
  `common::CommonMessage` (type `0x1234`, parser `FUN_0002b0e8`) and dispatched to
  `allinone_debug::DebugMain` (dispatcher `FUN_0002ad84`). The command's text reply
  is sent back over UDP to the sender.
- **Reply / timeout:** DebugSub waits up to **10 s** (`FUN_0003febc`, 10000 ms) for
  a reply before returning. Commands with no text output return a 1-byte `'1'` ack,
  and that ack can take the full 10 s. **The action itself runs immediately** (in
  DebugMain's thread, the moment the message is posted) — the 10 s only delays the
  ack and blocks DebugSub from reading the *next* datagram. So: single actions are
  responsive; rapid/continuous streams are throttled by this serial reply-wait.

### Sending commands (from inside the guest)

busybox `nc` is TCP-only and there's no socat/python, but the rootfs ships
`/bin/bash`, whose `/dev/udp` builtin does UDP. `qemu/build-nand.sh` installs a
helper `/usr/bin/subcmd` (bake, don't hand-hack):

```sh
# one line == one command; fire-and-forget (ignore the ack to stay responsive)
bash -c 'exec 3<>/dev/udp/127.0.0.1/20000; printf "%s\n" "$*" >&3' _ button play 0
```

Host → guest: DebugSub binds `127.0.0.1` (guest-internal), so a host sender needs a
guest-side TCP↔UDP bridge reachable via QEMU `hostfwd` (planned; baked into
build-nand.sh). For automated tests, run the sender in the guest.

## Command format

`verb arg arg …` — whitespace/tab delimited, quote-aware (tokenizer
`FUN_002e8d0c`). `verb` is matched against the command table at **`0x461d70`**
(`{name, handler}` pairs, 71 entries, null-terminated). Unknown verb → no-op ack.

## Command table (71 commands)

Args are from each handler's `usage:` string where present (`?` = not yet extracted).

### Panel-control injectors (the important ones)
| command | args | effect |
|---|---|---|
| `button` | `<type> [l] [playerNo 0-3]` | full press+release of a control (down, hold, up). `l` = long push. See the **button types** table below. |
| `knob`   | `<type> value(0-1023) [playerNo]` | set a continuous control (EQ, trim, fader…) to an absolute value |
| `rotary` | `<type> diff` | send a relative delta to a rotary encoder |
| `fader`  | `?` | fader position (handler not yet decoded) |

### Playback / deck
| command | args |
|---|---|
| `play` | `PlayerNo [Filename]` |
| `pause` | `PlayerNo` |
| `acue` | `playerNo` (auto-cue) |
| `jump` | `playerNo jumpPosition[msec]` |
| `load` | `PlayerNo Filename` |
| `tap`, `jog`, `quant`, `mark`, `tagtrack` | `?` |

### Mixer / FX
| command | args |
|---|---|
| `masterv` | `volume` |
| `master_att` | `value` |
| `gain` | `mixerChNo` |
| `miceq` | `value` |
| `type` | `mixer` |
| `typech`/`chtype` | `playerChNo mixerChNo` |
| `route`/`setroute` | `player -> mixer` |
| `routch` | `playerChNo mixerChNo` |
| `eqchange`, `mixeron`, `cfset`, `setMT`, `curve` | `?` |
| `bfbeat` | `chNo beat` (beat-FX) |
| `bfdepth` | `playerNo value` |
| `filter`/`crush`/`gate`/`noise`/`effect` | `?` (release-FX; also button types) |

### Browser / navigation  (needs a mounted USB — blocked by the 2D-GPU work)
| command | meaning |
|---|---|
| `brws` | browse |
| `lup` / `ldown` | list scroll up / down |
| `ep` / `bk` | enter / back |
| `epl` / `bkl` | enter-long / back-long |
| `lpoi` / `rpoi` / `callfwd` / `callrev` | pointer/call nav |
| `trfwd`/`trrev`/`srfwd`/`srrev` | track/search fwd/rev |

### Engine / codec / debug
`decode, simd, buffer, bufsleep, analyze_aac, hz, latency, get_bpm, bpm_test_on,
bpm_test_off, DebugLog, orb, xml, dummy, memory, delete, addmem, delmem, time,
true, usbstop, umount, shutdown, help, led_enable true/false`

### Network / NFS / Wi-Fi
`nfsmount, nfsumount ipaddr [force], nfsexport, nfsget drv:/file [unit],
nfsdir drv:, nfssd, nfsana, nfsmount, wlan_onoff, wlan_mode, ap_info, ap_other,
ap_ip ipaddr netmask, ap_get, ap_select ssid encryption password, wlantest [ssid],
wpatalk …`

## `button <type>` — the control vocabulary (handler `FUN_0001ec50`)

Each `button <type>` dispatches an event **code** via the same internal panel
dispatcher the real SPI panel uses (`FUN_002482d0()->dispatch(code, 0=down,
playerNo)` → `usleep` → `dispatch(code, 2=up)`). **These codes are identical to the
ones the SPI `SubRxData` receiver decodes** (`FUN_0028cea4`), so this table doubles
as the SPI control map.

Deck controls take a `playerNo` (0-3); global controls ignore it.

| type | code | | type | code | | type | code |
|---|---|---|---|---|---|---|---|
| `play` | 0x4101 | | `slip` | 0x4116 | | `trfwd` | 0x421b |
| `cue` | 0x4102 | | `pad1` | 0x4117 | | `trrev` | 0x421c |
| `shift` | 0x4103 | | `pad2` | 0x4118 | | `time` | 0x430c |
| `vinyl` | 0x4104 | | `pad3` | 0x4119 | | `jog` | 0x430e |
| `mt` | 0x4109 | | `pad4` | 0x411a | | `callfwd` | 0x431e |
| `sync` | 0x410b | | `srfwd` | 0x411b | | `callrev` | 0x431f |
| `loopin` | 0x410d | | `srrev` | 0x411c | | `tap` | 0x4486 |
| `loopout` | 0x410f | | `delete` | 0x4120 | | `effect` | 0x4487 |
| `reloop` | 0x4110 | | `memory` | 0x4121 | | `quant` | 0x4488 |
| `rev` | 0x4111 | | `rotary` | 0x4205 | | `filter` | 0x5095 |
| `master` | 0x4112 | | `back` | 0x4206 | | `crush` | 0x5096 |
| `hotcue` | 0x4113 | | `tagtrack` | 0x420b | | `gate` | 0x5097 |
| `aloop` | 0x4114 | | `decksel` | 0x420c | | `noise` | 0x5098 |
| `loopsplit` | 0x4115 | | `load` | 0x420d | | `usbstop` | 0x8002 |

Global (no playerNo): `midi` 0x201, `rekord` 0x202, `usb` 0x203, `info` 0x204,
`browse` 0x208, `taglist` 0x209, **`menu` 0x20a**, `mark` 0x409.

Send `button` with no type to get the device to print the full list.

### `knob` / `rotary` `<type>` (continuous controls)
Continuous-control types (from the adjacent string block; exact code tables in
handlers `FUN_0001e69c` (knob) / `FUN_0001e49c` (rotary), decode on demand):
`eqh, eqm, eql, color, fader, trim, xfader, curve, gain, master_att, miceq,
miceqh, miceql, booth, depth, hpmix, hplevel, mic, touch, time, rotary`.

## Relationship to the SPI panel path

The UDP **text** format is unrelated to SPI. But `button/knob/rotary` feed the
**same internal dispatcher** (`FUN_002482d0`→`FUN_00222188`) with the **same event
codes** that the SPI `SubRxData` frames carry. So:
- For **automated testing**: drive features via this console (scriptable, repeatable).
- For the **responsive final product**: the SPI path is still the faithful one
  (all controls incl. continuous jog/fader at hardware rates, no 10 s serial
  reply-wait). This table gives the SPI event codes per control for that work.
