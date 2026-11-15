# rbp debug console protocol (DebugSub / DebugMain)

rbp has a built-in text command console. It can drive most device functions, including
simulating panel controls. Its command vocabulary is also the naming source for the panel event
codes: [subucom-frame-map.md](subucom-frame-map.md) names each frame bit with the code its
`button`/`knob`/`rotary` handlers use.

The emulator does not use this console for input. Panel input goes through real SPI frames
([rbp-and-panel-protocol.md](rbp-and-panel-protocol.md)). Whether the console's injectors cause
the same visible UI reactions as real frames has not been verified.

All addresses are rbp VAs (non-PIE, base `0x8000`).

## Transport

- `allinone_debug::DebugSub` (thread `DebugSub`, run loop `FUN_0002b260`) binds a **UDP socket
  on `127.0.0.1:20000`** (the port comes from the DebugManager config; 20000 is the default) and
  loops on `recvfrom`. It is live from app start. rbp's UDP listeners on `0.0.0.0:50000/50001/50002`
  are separate servers.
- Each received **text line** (`\r` or `\n` terminated) is wrapped in a `common::CommonMessage`
  (type `0x1234`, tag `0x74583`, parser `FUN_0002b0e8`) and posted to
  `allinone_debug::DebugMain` (dispatcher `FUN_0002ad84`). The command's text reply goes back
  over UDP to the sender.
- **Reply and timeout:** DebugSub waits up to **10 s** for a reply (`FUN_0003febc`, 10000 ms).
  Commands that print nothing return a 1-byte `'1'` ack, which can take the full 10 s. The action
  itself runs immediately in DebugMain's thread; the wait only delays the ack and stops DebugSub
  from reading the next datagram. Single commands are responsive, but rapid streams are
  throttled by this wait.

### Sending commands

The socket is bound to the guest's loopback, so commands are sent from a guest shell (the serial
console, see the `run-xdj-emulator` skill). busybox `nc` is TCP-only and the rootfs has no
socat or python, but `/bin/bash` supports `/dev/udp`:

```sh
# one line = one command; fire-and-forget
bash -c 'exec 3<>/dev/udp/127.0.0.1/20000; printf "%s\n" "$*" >&3' _ button play 0

# with the reply
bash -c 'exec 3<>/dev/udp/127.0.0.1/20000; printf "%s\n" "$0" >&3; cat <&3 >/tmp/r & p=$!; sleep 11; kill $p' "button"
```

The emulator has no host → guest forward for this port.

## Command format

`verb arg arg …`, separated by spaces or tabs, with quote-aware tokenizing (tokenizer
`FUN_002e8d0c`). `verb` is matched against the command table at **`0x461d70`**: 71
`{name, handler}` pairs, null-terminated. An unknown verb gives a no-op ack.

## Commands

Arguments come from each handler's `usage:` string. `?` means the usage has not been extracted.

### Panel-control injectors
| command | args | effect |
|---|---|---|
| `button` | `<type> [l] [playerNo 0-3]` | full press + release of a control (down, hold, up); `l` = long push. Types are listed below. |
| `knob` | `<type> value(0-1023) [playerNo]` | set a continuous control (EQ, trim, fader, …) to an absolute value |
| `rotary` | `<type> diff` | send a relative delta to a rotary encoder |
| `fader` | `?` | fader position (handler not decoded) |

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
| `typech` / `chtype` | `playerChNo mixerChNo` |
| `route` / `setroute` | `player -> mixer` |
| `routch` | `playerChNo mixerChNo` |
| `eqchange`, `mixeron`, `cfset`, `setMT`, `curve` | `?` |
| `bfbeat` | `chNo beat` (beat FX) |
| `bfdepth` | `playerNo value` |
| `filter` / `crush` / `gate` / `noise` / `effect` | `?` (sound-colour FX; these are also `button` types) |

### Browser / navigation
These need a mounted USB.

| command | meaning |
|---|---|
| `brws` | browse |
| `lup` / `ldown` | list scroll up / down |
| `ep` / `bk` | enter / back |
| `epl` / `bkl` | enter-long / back-long |
| `lpoi` / `rpoi` / `callfwd` / `callrev` | pointer / call navigation |
| `trfwd` / `trrev` / `srfwd` / `srrev` | track / search forward / reverse |

### Engine / codec / debug
`decode, simd, buffer, bufsleep, analyze_aac, hz, latency, get_bpm, bpm_test_on,
bpm_test_off, DebugLog, orb, xml, dummy, memory, delete, addmem, delmem, time,
true, usbstop, umount, shutdown, help, led_enable true/false`

### Network / NFS / Wi-Fi
`nfsmount, nfsumount ipaddr [force], nfsexport, nfsget drv:/file [unit],
nfsdir drv:, nfssd, nfsana, wlan_onoff, wlan_mode, ap_info, ap_other,
ap_ip ipaddr netmask, ap_get, ap_select ssid encryption password, wlantest [ssid],
wpatalk …`

## `button <type>`: the control vocabulary

Handler `FUN_0001ec50`. It gets the panel event object from `FUN_002482d0()` and calls its
dispatch method with `(code, 0 = down, playerNo)`, then `usleep`, then `(code, 2 = up)`. The
codes and the down/up state values are the same ones the SPI frame decoders emit (observed at the
panel dispatcher `0x2a47c0`, where state 0 = press and 2 = release). So this table also names the
frame's control codes.

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

Global (no playerNo): `midi` 0x201, `rekord` 0x202, `usb` 0x203, `info` 0x204, `browse` 0x208,
`taglist` 0x209, `menu` 0x20a, `mark` 0x409.

`button` with no type makes the device print the full list.

### `knob` / `rotary` types

Handlers `FUN_0001e69c` (knob) and `FUN_0001e49c` (rotary). Their code tables have not been
fully decoded. The type names, from the adjacent string block:
`eqh, eqm, eql, color, fader, trim, xfader, curve, gain, master_att, miceq, miceqh, miceql,
booth, depth, hpmix, hplevel, mic, touch, time, rotary`.
