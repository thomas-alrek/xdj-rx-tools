#!/usr/bin/env python3
"""
Local bridge for the virtual XDJ-RX panel.

Serves index.html and turns panel actions into the emulator's control-injection
protocol, and streams the QEMU framebuffer to the browser as raw RGB (drawn on a
<canvas>) — no per-frame image encoding, over a persistent monitor connection:

  POST /ctrl     body="A 2c 3ff" / "B 12 08" / "b 5e 01" / "S 5a e0 20"
                 -> set analog / hold button / one-shot / set switch field
  POST /release  body="B 12 08" (or empty = all)          -> let go of held button(s)
  POST /jog      body="1 move <deg> <sec>" | "1 touch 1"  -> jog wheel (counter/period/flags)
  GET  /raw                      -> latest framebuffer as raw RGB
                                    (headers X-Width / X-Height; body w*h*3 bytes)
  GET  /logs.html?src=subucom|console -> log window page; GET /log/<src> = its SSE stream
  POST /power  on|off|toggle [update|test] -> power the emulator (xdj-host); GET /power = state
                 (test = boot into the service manual's test mode, see _test_mode_boot)
  POST /log/note                 -> append a panel note to the sub-ucom log
  GET  /leds                     -> Server-Sent Events: one "data: <40|42 hex>" per change of
                                    rbp's 20-byte LED/meter payload (SubTxData, xdj-panel.c (was patch 15);
                                    layout docs/subucom-led-map.md). /leds?once=1 = current JSON.

A QEMU `screendump` of the 800x480 surface takes ~18 ms, so /raw back-to-back gives
20-40 fps on localhost. The QEMU xdj-subucom harness reads /tmp/subucom_ctrl each
frame: b/a = one-shot press+release ; B/A = hold while the file exists (release on rm).

Control transport (standalone-app Phase 1): by default the control state is written to
the /tmp files (/tmp/subucom_ctrl, /tmp/mainkey) that the QEMU harness polls. Pass
--ctrl-sock / --key-sock to instead push it to the per-instance QEMU chardev sockets
(id=xdjctrl / id=xdjkey) as NUL-terminated snapshots -- launch QEMU with XDJ_RUNTIME_DIR
set (watch.sh / relaunch.sh) so those sockets exist. This is the reference client for the
Phase 2 Rust host.

Usage:  python3 tools/xdj-panel/server.py [--port 8765] [--mon /tmp/d_mon4.sock]
                [--ctrl-sock <dir>/ctrl.sock] [--key-sock <dir>/key.sock]
Then open http://127.0.0.1:8765
"""
import argparse, http.server, socketserver, socket, time, os, threading, json, subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, "assets")
CAL = os.path.join(ASSETS, "calibration.json")
# Vendored noVNC (served at /novnc/* so panel.html's screen pane can use RFB-over-
# WebSocket when launched with ?vnc=ws://... -- the standalone-app Phase 2 path).
# XDJ_NOVNC_DIR overrides the dev repo path (the packaged .app sets it; Phase 5).
NOVNC = os.environ.get("XDJ_NOVNC_DIR") or os.path.normpath(
    os.path.join(HERE, "..", "..", "app", "ui", "novnc"))
# The Knobs library (npm: audio-knobs), served at /knobs/* for the panel's knobs/faders.
# Dev: the npm install under app/ui/node_modules; the packaged .app sets XDJ_KNOBS_DIR.
KNOBS = os.environ.get("XDJ_KNOBS_DIR") or os.path.normpath(
    os.path.join(HERE, "..", "..", "app", "ui", "node_modules", "audio-knobs", "dist"))
CTRL = "/tmp/subucom_ctrl"
MAINKEY = "/tmp/mainkey"        # MAIN-UCOM direct-key GPIO (USB1/USB2/rekordbox/MIDI)
MON = "/tmp/d_mon4.sock"
QMP = None                      # QEMU QMP unix socket (the .app passes this; used for USB mount)
PPM = "/tmp/xdjpanel_fb.ppm"

# Phase 1 control transport. When --ctrl-sock / --key-sock are given, control state
# is pushed to the QEMU chardev sockets (id=xdjctrl / id=xdjkey) as NUL-terminated
# snapshots instead of the /tmp files -- QEMU is the server, we connect as a client.
# Each record is the ENTIRE desired state (one snapshot); an empty record releases.
# When unset we keep writing the /tmp files exactly as before (the env-path fallback
# in the QEMU harness still lets those be per-instance). This is the reference client
# for the Phase 2 Rust host.
CTRL_SOCK = None
KEY_SOCK = None
_sock_lock = threading.Lock()
_conn = {"ctrl": None, "key": None}
_last_ctrl = ""                 # last ctrl snapshot sent (read-back in socket mode)

_CTYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
           ".webp": "image/webp", ".json": "application/json; charset=utf-8",
           ".html": "text/html; charset=utf-8", ".css": "text/css",
           ".js": "application/javascript", ".svg": "image/svg+xml"}

_grab_lock = threading.Lock()

# LED back-channel (xdj-panel.c (was patch 15)). rbp streams its LED / level-meter state to the
# panel MCU as a SubTxData frame; the harness forwards each change as "T <40 hex>\n" --
# back up the xdjctrl socket in socket mode, or into TX_PATH in file mode. We keep the
# latest payload here and fan it out to every /leds SSE client.
TX_PATH = os.environ.get("XDJ_TX_PATH") or "/tmp/subucom_tx"
_leds = {"hex": "", "seq": 0, "t": 0.0}
_leds_cv = threading.Condition()


# ---- log windows (View > Sub uCom log / System log) -------------------------------------
# Two line rings served as SSE at GET /log/<src>:
#   subucom -- panel -> emulator control traffic (what each POST asked for) and rbp's LED
#              changes (non-meter / non-jog bytes), plus panel notes (POST /log/note);
#   console -- the guest Linux console, tailed from --console (the app's runtime-dir
#              console.log; the debug launcher's /tmp/xdj_console.log).
LOG_MAX = 5000
_logs = {"subucom": [], "console": []}       # [(index, line)]
_log_next = {"subucom": 0, "console": 0}
_log_cv = threading.Condition()
CONSOLE = None


def _logline(src, text):
    with _log_cv:
        buf = _logs[src]
        for ln in str(text).splitlines() or [""]:
            if src != "console":
                ln = time.strftime("%H:%M:%S") + "  " + ln
            buf.append((_log_next[src], ln))
            _log_next[src] += 1
        if len(buf) > LOG_MAX:
            del buf[: len(buf) - LOG_MAX]
        _log_cv.notify_all()


_LED_SKIP = {0x00, 0x04, 0x08, 0x09, 0x0C, 0x0D, 0x0E, 0x0F}   # jog rings + level meters


_led_prev = {}      # byte -> (value before its last change, time of that change)


def _log_led_diff(old, new):
    # rbp blinks LEDs by toggling bits ~3x/s; a byte flipping straight back to its previous
    # value within 1 s is a blink, not a state change, so it isn't logged.
    try:
        a, b = bytes.fromhex(old), bytes.fromhex(new)
    except ValueError:
        return
    now, ch = time.time(), []
    for i in range(min(len(a), len(b))):
        if a[i] == b[i] or i in _LED_SKIP:
            continue
        pv, t = _led_prev.get(i, (None, 0.0))
        _led_prev[i] = (a[i], now)
        if pv == b[i] and now - t < 1.0:
            continue
        ch.append("%02x:%02x>%02x" % (i, a[i], b[i]))
    if len(b) > len(a):
        ch.append("%02x:->%02x" % (len(a), b[len(a)]))
    if ch:
        _logline("subucom", "<- LED  " + "  ".join(ch))


def _console_loop():
    # Tail CONSOLE (it may appear later, or be truncated by a reboot).
    pos, ino, part = 0, None, b""
    while True:
        try:
            st = os.stat(CONSOLE)
            if ino != st.st_ino or st.st_size < pos:
                ino, pos, part = st.st_ino, max(0, st.st_size - 200_000), b""
            if st.st_size > pos:
                with open(CONSOLE, "rb") as f:
                    f.seek(pos)
                    data = f.read(1_000_000)
                pos += len(data)
                data = part + data
                lines = data.split(b"\n")
                part = lines.pop()
                if lines:
                    _logline("console", b"\n".join(l.rstrip(b"\r") for l in lines)
                             .decode("utf-8", "replace"))
        except FileNotFoundError:
            pass
        time.sleep(0.25)


# ---- power (desktop app: xdj-host owns QEMU; we forward the panel's power button) ----
# xdj-host listens on <runtime>/power.sock: "on" | "on update" | "off" | "status" -> one
# line "on" / "on update" / "off". Without it (dev/debug launcher) power is unavailable.
POWER_SOCK = None
_pending_usb = {}             # slot -> image path, inserted once the guest is ready
_power_lock = threading.Lock()


def _power_cmd(cmd):
    if not POWER_SOCK or not os.path.exists(POWER_SOCK):
        return None
    try:
        with socket.socket(socket.AF_UNIX) as c:
            c.settimeout(5.0)
            c.connect(POWER_SOCK)
            c.sendall((cmd + "\n").encode())
            return c.makefile().readline().strip() or None
    except Exception:
        return None


def _leds_off():
    # Powered off: tell the panel to darken every LED / meter / jog ring.
    with _leds_cv:
        _leds["hex"] = "off"
        _leds["seq"] += 1
        _leds_cv.notify_all()


# Test mode (service manual 6.1; docs/service-modes.md): rbp's boot-key handler looks at
# the FIRST sub-ucom frame only -- DECK 1 MEMORY [0x13]&04 + TEMPO RANGE [0x12]&08 held
# then = TestMode. The holds go into _held before power on, so the ctrl-socket connect to
# the fresh QEMU (_repush) delivers them before rbp's first read.
TEST_MODE_KEYS = ("B 12 08", "B 13 04")


def _test_mode_boot(t0):
    """Let go of the test-mode keys once rbp is certainly past its first-frame check.
    Started AFTER the power on. Neither rbp's startup line nor its first LED frame is
    late enough: rbp starts streaming SubTxData before its panel thread parses the
    first input frame, and releasing on those raced (boots came up normal about half
    the time). "ALSA: Resolve xrun" only prints once rbp is fully up (in test mode
    and normal boot alike), long after the boot-key check; holding the keys until
    then is harmless (test mode 1 ignores them). Fallback: release after 3 min, or
    once powered off."""
    while time.time() - t0 < 180:
        if time.time() - t0 > 5 and _power_cmd("status") in (None, "off"):
            break
        if _console_has("Resolve xrun"):
            time.sleep(2)
            break
        time.sleep(0.5)
    with _browse_lock:
        for ln in TEST_MODE_KEYS:
            _, off, mask = ln.split()
            _held.pop((int(off, 16), int(mask, 16)), None)
        _push()
    _logline("subucom", "·  test mode: released MEMORY + TEMPO RANGE")


def _console_has(marker):
    try:
        with open(CONSOLE, "rb") as f:
            return marker.encode() in f.read()
    except Exception:
        return False


def _insert_pending(update):
    # Mirror xdj-host's maybe_insert_usb choreography: insert only once the guest is
    # ready for it -- rbp polling its USB manager (normal boot: "Resolve xrun" + settle),
    # or the initramfs waiting for the update stick ("USB update mount").
    marker, settle = ("USB update mount", 2) if update else ("Resolve xrun", 6)
    t0 = time.time()
    while time.time() - t0 < 300:
        if _power_cmd("status") in (None, "off"):
            return
        if _console_has(marker):
            break
        time.sleep(1.0)
    else:
        _logline("subucom", "·  USB insert: gave up waiting for the guest")
        return
    time.sleep(settle)
    with _power_lock:
        items = list(_pending_usb.items())
        _pending_usb.clear()
    for slot, img in items:
        try:
            _logline("subucom", "·  inserting queued %s: %s" % (slot, mount_usb(slot, img)))
        except Exception as e:
            _logline("subucom", "·  queued %s insert failed: %s" % (slot, e))


def _leds_update(hexstr):
    # "<40 hex>" = SubTxData; QEMU (xdj-board.c (was patch 17)) appends " <2 hex>" = source-key LEDs
    # (GPIO 162..165: bit0 rekordbox, bit1 MIDI, bit2 USB1, bit3 USB2). Served as one
    # string: byte 0x14 of the panel's LED array.
    hexstr = hexstr.strip().lower().replace(" ", "")
    if len(hexstr) not in (40, 42) or any(c not in "0123456789abcdef" for c in hexstr):
        return
    with _leds_cv:
        if hexstr != _leds["hex"]:
            if _leds["hex"]:
                _log_led_diff(_leds["hex"], hexstr)
            _leds["hex"] = hexstr
            _leds["seq"] += 1
            _leds["t"] = time.time()
            _leds_cv.notify_all()


def _leds_file_loop():
    """File mode: poll the harness's TX file (atomically replaced on each change)."""
    last = None
    while True:
        try:
            st = os.stat(TX_PATH)
            key = (st.st_mtime_ns, st.st_size, st.st_ino)
            if key != last:
                last = key
                with open(TX_PATH) as f:
                    ln = f.read().strip()
                if ln.startswith("T "):
                    _leds_update(ln[2:])
        except Exception:
            pass
        time.sleep(0.03)


def _repush():
    time.sleep(0.3)
    try:
        with _browse_lock:
            _push()
    except Exception:
        pass


def _leds_sock_loop():
    """Socket mode: read "T <hex>" lines QEMU writes back on the xdjctrl connection.
    Shares the persistent connection _sock_send uses (one client per chardev)."""
    buf = b""
    while True:
        with _sock_lock:
            conn = _conn["ctrl"]
            if conn is None:
                try:
                    conn = socket.socket(socket.AF_UNIX)
                    conn.settimeout(2.0)
                    conn.connect(CTRL_SOCK)
                    _conn["ctrl"] = conn
                    buf = b""
                    # A (re)started QEMU has no panel state yet: send the full snapshot.
                    threading.Thread(target=_repush, daemon=True).start()
                except Exception:
                    conn = None
        if conn is None:
            time.sleep(0.5)
            continue
        try:
            data = conn.recv(4096)
        except socket.timeout:
            continue
        except Exception:
            data = b""
        if not data:                      # peer closed / error: drop and reconnect
            with _sock_lock:
                if _conn["ctrl"] is conn:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    _conn["ctrl"] = None
            time.sleep(0.5)
            continue
        buf += data
        while b"\n" in buf:
            ln, buf = buf.split(b"\n", 1)
            if ln.startswith(b"T "):
                _leds_update(ln[2:].decode("ascii", "replace"))

# Browse ROTATE state: the encoder is a free-running 16-bit counter at the
# subucom frame bytes rbp[0x54]/[0x55] (LE). The firmware reads delta=new-old,
# so +1 per polled frame = one detent down, -1 = one detent up. We keep the
# counter here and ramp it one step at a time with a poll-safe dwell.
_browse_counter = 0
_browse_active = False          # True once the encoder has been turned this session
_browse_lock = threading.Lock()

# INPUT SELECT (per-channel source switch): the sub-ucom frame bits
# rbp[0x33].7 (ch0/deck1) / rbp[0x43].7 (ch1/deck2). Bit SET = DECK (channel plays
# its deck, src n); bit CLEAR = LINE/PHONO (src n+3, silent in the emulator). Held
# as persistent B-masks so DECK survives every other control write; default DECK so
# a loaded deck is audible out of the box. (LINE = omit the hold -> bit stays 0.)
_INPUT_BIT = {0: (0x33, 0x80), 1: (0x43, 0x80)}
_input_src = {0: 1, 1: 1}       # 1 = DECK (default), 0 = LINE
_input_lock = threading.Lock()


def _input_hold_lines():
    """Hold lines for channels currently set to DECK (LINE channels omit the bit)."""
    out = []
    with _input_lock:
        for ch in sorted(_input_src):
            if _input_src[ch]:
                off, mask = _INPUT_BIT[ch]
                out.append("B %02x %02x" % (off, mask))
    return out


def _write_ctrl_atomic(data):
    with open(CTRL + ".tmp", "w") as f:
        f.write(data)
    os.replace(CTRL + ".tmp", CTRL)


def _sock_send(which, data):
    """Send a NUL-terminated snapshot to the ctrl/key unix socket.

    Keeps a persistent connection and reconnects once on failure (QEMU restarts,
    or wasn't up yet when the panel loaded). Returns True on success.
    """
    path = CTRL_SOCK if which == "ctrl" else KEY_SOCK
    payload = data.encode("utf-8", "replace") + b"\0"
    with _sock_lock:
        for _ in range(2):
            conn = _conn[which]
            if conn is None:
                try:
                    conn = socket.socket(socket.AF_UNIX)
                    conn.settimeout(2.0)
                    conn.connect(path)
                    _conn[which] = conn
                except Exception:
                    _conn[which] = None
                    return False
            try:
                conn.sendall(payload)
                return True
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass
                _conn[which] = None      # drop + retry a fresh connection once
    return False


def _write_ctrl(data):
    """Push the full ctrl snapshot: socket (NUL-framed) or /tmp file (atomic)."""
    global _last_ctrl
    _last_ctrl = data
    if CTRL_SOCK:
        _sock_send("ctrl", data)
    elif data.strip():
        _write_ctrl_atomic(data)
    else:
        try:
            os.remove(CTRL)             # empty snapshot == release (file removed)
        except FileNotFoundError:
            pass


def _read_ctrl():
    """Current ctrl snapshot text for read-back (browse resync / input preserve)."""
    if CTRL_SOCK:
        return _last_ctrl
    try:
        with open(CTRL) as f:
            return f.read()
    except Exception:
        return ""


def _write_key(data):
    """Push the mainkey GPIO int: socket (NUL-framed) or /tmp file. Empty = release."""
    if KEY_SOCK:
        _sock_send("key", data)
    elif data.strip():
        with open(MAINKEY, "w") as f:
            f.write(data + "\n")
    else:
        try:
            os.remove(MAINKEY)
        except FileNotFoundError:
            pass


# ---- full panel state -------------------------------------------------------
# The QEMU harness takes the WHOLE desired frame state per snapshot (a missing line
# = that field falls back to the auto-level default / 0). So the server keeps the
# panel's complete state and every write re-sends all of it:
#   _analog  {off: val16}        every fader/knob the operator has moved (persistent)
#   _held    {(off, mask): n}    buttons currently held down (any number at once,
#                                e.g. SHIFT + a pad), OR-merged per byte on output
#   _switch  {(off, mask): bits} multi-position switches packed into a byte field
#                                (FX SELECT, CH SELECT, XF CURVE, MIC) -- persistent
#   _jog     per-deck jog counter / period / direction / touch (see /jog)
# plus the browse counter and INPUT-SELECT holds kept by the routes below.
_analog = {}
_held = {}
_switch = {}                            # {(off, mask): bits}  multi-position switches
_JOG_BASE = {1: 0x00, 2: 0x10}          # deck2 = deck1 + 0x10
# deck-relative: period @0x0c, counter @0x0e, flags @0x10 (bit0 touch, bit1 reverse)
_jog = {d: {"cnt": 0, "per": 0, "rev": 0, "touch": 0, "active": False, "t": 0.0}
        for d in _JOG_BASE}
JOG_TICKS_PER_REV = 1024                # counter ticks per platter turn (model)
JOG_SPEED_K = 416.67                    # rbp: speed = 416.67 / period  (FUN_00283dc4)
JOG_NORMAL_RPS = 100.0 / 3 / 60         # 33 1/3 rpm -> firmware speed 1.0
JOG_IDLE_S = 0.12                       # no motion for this long -> period 0 (stopped)


def _jog_lines():
    out = []
    for d, j in _jog.items():
        if not j["active"]:
            continue
        b = _JOG_BASE[d]
        out.append("A %02x %x" % (b + 0x0c, j["per"] & 0xffff))
        out.append("A %02x %x" % (b + 0x0e, j["cnt"] & 0xffff))
        flags = (0x01 if j["touch"] else 0) | (0x02 if j["rev"] else 0)
        if flags:
            out.append("B %02x %02x" % (b + 0x10, flags))
    return out


def _compose_ctrl(lines=()):
    """Full snapshot = persistent state + any one-shot lines passed in.

    Persistent state (analog values, held buttons, jog, browse counter, input
    select) is re-sent on EVERY write: the harness treats the snapshot as the whole
    truth, so anything omitted would snap back. Once the browse encoder has moved,
    its 16-bit counter at 0x54/0x55 MUST be in every frame (a drop reads as a huge
    rotate delta). One-shot 'b'/'a' lines ride on top (the harness fires them once)."""
    out = ["A %02x %x" % (off, val & 0xffff) for off, val in sorted(_analog.items())]
    merged = {}
    for (off, mask) in _held:
        merged[off] = merged.get(off, 0) | mask
    for (off, mask), bits in _switch.items():
        if bits & mask:
            merged[off] = merged.get(off, 0) | (bits & mask)
    out += ["B %02x %02x" % (off, m) for off, m in sorted(merged.items())]
    out += _jog_lines()
    if _browse_active:
        out.append("A 54 %x" % _browse_counter)
    out += _input_hold_lines()
    out += [ln.strip() for ln in lines if ln.strip() and ln.strip()[0] in "ab"]
    return "\n".join(out) + "\n"


def _apply_lines(lines):
    """Fold A/B spec lines into the persistent state; return the one-shot lines."""
    shots = []
    for ln in lines:
        parts = ln.split()
        if len(parts) == 4 and parts[0] == "S":      # S <off> <mask> <bits>
            try:
                _switch[(int(parts[1], 16), int(parts[2], 16))] = int(parts[3], 16)
            except ValueError:
                pass
            continue
        if len(parts) != 3 or parts[0] not in "ABab":
            continue
        try:
            off, val = int(parts[1], 16), int(parts[2], 16)
        except ValueError:
            continue
        if parts[0] == "A":
            if off == 0x54:
                continue                    # the browse counter is owned by /browse
            _analog[off] = val
        elif parts[0] == "B":
            _held[(off, val)] = 1
        else:
            shots.append(ln)
    return shots


def _push():
    _write_ctrl(_compose_ctrl())


def _jog_idle_loop():
    """A real jog reports period 0 once the platter stops; model that by zeroing the
    period JOG_IDLE_S after the last move (the counter keeps its position)."""
    while True:
        time.sleep(0.04)
        now = time.time()
        with _browse_lock:
            dirty = False
            for j in _jog.values():
                if j["per"] and now - j["t"] > JOG_IDLE_S:
                    j["per"] = 0
                    dirty = True
            if dirty:
                try:
                    _push()
                except Exception:
                    pass


def _parse_ppm(data):
    """Return (w, h, rgb_bytes) from a binary P6 PPM, or None."""
    if not data.startswith(b"P6"):
        return None
    i = 2
    vals = []
    while len(vals) < 3 and i < len(data):
        while i < len(data) and data[i:i+1].isspace():
            i += 1
        if i < len(data) and data[i:i+1] == b"#":           # comment line
            while i < len(data) and data[i:i+1] != b"\n":
                i += 1
            continue
        j = i
        while j < len(data) and not data[j:j+1].isspace():
            j += 1
        try:
            vals.append(int(data[i:j]))
        except ValueError:
            return None
        i = j
    if len(vals) < 3:
        return None
    w, h, _maxv = vals
    i += 1                                                  # single whitespace after maxval
    rgb = data[i:i + w * h * 3]
    if len(rgb) < w * h * 3:
        return None
    return w, h, rgb


def grab_raw():
    """Screendump with a FRESH monitor connection each call; return (w, h, rgb) or None.

    A fresh connection (unix connect is sub-millisecond; the screendump itself is the
    ~18ms cost) avoids the stale-frame trap of a persistent connection: if QEMU was
    restarted, a held connection can keep returning the last PPM on disk. We delete the
    PPM first and only read it back if the screendump actually re-creates it, so a dead
    emulator yields None (the page shows "no QEMU") instead of a frozen old frame.
    """
    with _grab_lock:
        try:
            os.remove(PPM)
        except FileNotFoundError:
            pass
        try:
            s = socket.socket(socket.AF_UNIX)
            s.settimeout(2.0)
            s.connect(MON)
            time.sleep(0.02)
            s.setblocking(False)
            try:
                while True:
                    if not s.recv(65536):
                        break
            except Exception:
                pass
            s.setblocking(True)
            s.settimeout(2.0)
            s.sendall(('screendump "%s"\n' % PPM).encode())
            t0 = time.time()
            sz = -1
            while time.time() - t0 < 2.0:
                if os.path.exists(PPM):
                    s2 = os.path.getsize(PPM)
                    if s2 > 1000 and s2 == sz:
                        break
                    sz = s2
                time.sleep(0.003)
            s.close()
        except Exception:
            return None
        if not os.path.exists(PPM):
            return None
        try:
            with open(PPM, "rb") as f:
                return _parse_ppm(f.read())
        except Exception:
            return None


# ---- USB mount (drive_add/device_add on the hub; the attach itself wakes HOST1) ----
# Mirrors the app/host `maybe_insert_usb` choreography so a panel LOAD button can mount
# a disk image (or physical device) on the fly. Over QMP in the .app (--qmp), or the raw
# HMP monitor in dev (--mon). docs/usb-mount-howto.md / usb-slot-topology.md.
def _qmp_hmp(sock_path, cmds):
    """Run HMP command-lines via QMP human-monitor-command on a fresh connection."""
    s = socket.socket(socket.AF_UNIX); s.settimeout(5.0); s.connect(sock_path)
    f = s.makefile("rwb")
    f.readline()                                   # QMP greeting
    f.write(b'{"execute":"qmp_capabilities"}\n'); f.flush(); f.readline()
    outs = []
    for c in cmds:
        f.write((json.dumps({"execute": "human-monitor-command",
                             "arguments": {"command-line": c}}) + "\n").encode()); f.flush()
        while True:
            line = f.readline()
            if not line:
                break
            obj = json.loads(line.decode())
            if "return" in obj or "error" in obj:
                outs.append(obj); break
    s.close()
    return outs


def _hmp_raw(cmds):
    """Send raw HMP command lines over the --mon monitor socket (dev)."""
    s = socket.socket(socket.AF_UNIX); s.settimeout(5.0); s.connect(MON)
    time.sleep(0.02)
    s.setblocking(False)
    try:
        while s.recv(65536):
            pass
    except Exception:
        pass
    s.setblocking(True); s.settimeout(5.0)
    out = []
    for c in cmds:
        s.sendall((c + "\n").encode()); time.sleep(0.08)
        try:
            out.append(s.recv(65536).decode("utf-8", "replace"))
        except Exception:
            pass
    s.close()
    return out


def _mon(cmds):
    """Run HMP command-lines over QMP (the .app) or the raw HMP monitor (dev)."""
    return _qmp_hmp(QMP, cmds) if QMP else _hmp_raw(cmds)


def _mon_quiet(cmds):
    """Best-effort monitor commands; ignore any failure (e.g. device not present)."""
    try:
        _mon(cmds)
    except Exception:
        pass


# Current panel-mounted drive/device id per slot, so a re-LOAD can eject it first.
_usb_cur = {"usb1": None, "usb2": None}


def eject_usb(slot):
    """Detach whatever the panel (or the boot auto-insert) put on this slot."""
    fault = "/tmp/usb_insert" if slot == "usb1" else "/tmp/usb2_insert"
    cur = _usb_cur.get(slot)
    # Candidates: our previous panel insertion + the host's boot auto-insert id.
    pairs = []
    if cur:
        pairs.append(cur)
    pairs.append(("xdjusb_%s" % slot, "xdjusb_%s_dev" % slot))
    for _drive, dev in pairs:
        _mon_quiet(["device_del %s" % dev])      # async unplug; frees the hub port
    time.sleep(0.4)
    for drive, _dev in pairs:
        _mon_quiet(["drive_del %s" % drive])
    try:
        os.remove(fault)                         # never leave the USB fault flag asserted
    except FileNotFoundError:
        pass
    _usb_cur[slot] = None
    time.sleep(0.15)


def mount_usb(slot, imgpath, write=False):
    """Insert `imgpath` (file or /dev device) on the given front slot (usb1|usb2),
    ejecting any previous insertion first so re-LOAD works without a duplicate-ID."""
    port, fault = ("1.2", "/tmp/usb_insert") if slot == "usb1" else ("1.1", "/tmp/usb2_insert")
    real = os.path.realpath(imgpath)
    # A physical disk must be released by the host OS first (macOS/Linux, best-effort).
    if imgpath.startswith("/dev/"):
        for cmd in (["diskutil", "unmountDisk", imgpath], ["umount", imgpath]):
            try:
                subprocess.run(cmd, capture_output=True, timeout=15); break
            except Exception:
                continue
    # Eject anything already on this slot (previous LOAD or the boot auto-insert).
    eject_usb(slot)
    # HMP drive_add doesn't quote paths; symlink to a space-free path.
    link = "/tmp/xdjpanel_%s.img" % slot
    try:
        os.remove(link)
    except FileNotFoundError:
        pass
    os.symlink(real, link)
    # Unique ids per insertion so a slow async unplug can't collide with the new one.
    did = "xdjpanel_%s_%d" % (slot, int(time.time() * 1000))
    dev = did + "_dev"
    snap = "" if write else ",snapshot=on"
    cmds = [
        "drive_add 0 if=none,id=%s,file=%s,format=raw%s" % (did, link, snap),
        "device_add usb-storage,bus=usb-bus.1,port=%s,drive=%s,id=%s,removable=on" % (port, did, dev),
    ]
    outs = _mon(cmds)
    blob = json.dumps(outs).lower()
    if '"error"' in blob or "failed" in blob or "duplicate" in blob or "in use" in blob:
        raise RuntimeError("monitor rejected insert: %s" % outs)
    _usb_cur[slot] = (did, dev)
    # Do NOT create the xdj-panel.c (was patch 06) file (/tmp/usb*_insert) here. Those GPIOs
    # (GPIO4_IO30 / GPIO7_IO12) are rbp's active-low USB *fault* flags, not
    # insert-detect lines; holding one low raises "USB Error. Remove the device."
    # once the slot mounts. Host1 resume on attach comes from xdj-usbnc.c (was patch 10) (USBNC OWIR).
    # Clear a stale fault file left by an older build/tool so the flag stays OK.
    try:
        os.remove(fault)
    except FileNotFoundError:
        pass
    return "mounted %s on %s%s" % (os.path.basename(real), slot.upper(),
                                   "" if write else " (read-only)")


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, ctype, body, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, relpath, base=HERE):
        fp = os.path.normpath(os.path.join(base, relpath.lstrip("/")))
        if not fp.startswith(os.path.normpath(base) + os.sep) and fp != os.path.normpath(base):
            self._send(403, "text/plain", b"forbidden")
            return
        if not os.path.isfile(fp):
            self._send(404, "text/plain", b"not found")
            return
        ctype = _CTYPES.get(os.path.splitext(fp)[1].lower(), "application/octet-stream")
        try:
            with open(fp, "rb") as f:
                self._send(200, ctype, f.read())
        except Exception as e:
            self._send(500, "text/plain", str(e).encode())

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/panel.html"):
            self._send_file("panel.html")
        elif path in ("/grid", "/index.html"):
            self._send_file("index.html")
        elif path.startswith("/assets/"):
            self._send_file(path[len("/assets/"):], base=ASSETS)
        elif path.startswith("/novnc/"):
            self._send_file(path[len("/novnc/"):], base=NOVNC)
        elif path.startswith("/knobs/"):
            self._send_file(path[len("/knobs/"):], base=KNOBS)
        elif path == "/cal-ref":
            # Dev-only calibration overlay image (never bundled/committed).
            # Set XDJ_CAL_REF=/path/to/hardware-photo.png to enable.
            ref = os.environ.get("XDJ_CAL_REF", "")
            if ref and os.path.isfile(ref):
                try:
                    with open(ref, "rb") as f:
                        self._send(200, "image/png", f.read())
                except Exception as e:
                    self._send(500, "text/plain", str(e).encode())
            else:
                self._send(404, "text/plain", b"no XDJ_CAL_REF")
        elif path == "/cal":
            if os.path.isfile(CAL):
                self._send_file("calibration.json", base=ASSETS)
            else:
                self._send(404, "application/json", b"{}")
        elif path == "/power":
            st = _power_cmd("status")
            with _power_lock:
                pend = dict(_pending_usb)
            body = {"available": st is not None, "state": (st or "off").split()[0],
                    "update": bool(st and st.endswith("update")), "pending": pend}
            self._send(200, "application/json", json.dumps(body).encode())
        elif path == "/logs.html":
            self._send_file("logs.html")
        elif path in ("/log/subucom", "/log/console"):
            src = path.rsplit("/", 1)[1]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            if src == "console" and not CONSOLE:
                self.wfile.write(b"data: (no console log: start the panel server with --console)\n\n")
            nxt = 0
            try:
                while True:
                    with _log_cv:
                        if _log_next[src] == nxt:
                            _log_cv.wait(15.0)
                        out = [ln for i, ln in _logs[src] if i >= nxt]
                        nxt = _log_next[src]
                    if out:
                        self.wfile.write("".join("data: %s\n\n" % ln.replace("\r", "")
                                                 for ln in out[-LOG_MAX:]).encode())
                    else:
                        self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
            except Exception:
                pass
        elif path == "/leds":
            if "once=1" in self.path:
                with _leds_cv:
                    body = json.dumps(_leds).encode()
                self._send(200, "application/json", body)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            seen = -1
            try:
                while True:
                    with _leds_cv:
                        if _leds["seq"] == seen:
                            _leds_cv.wait(15.0)
                        seq, hx = _leds["seq"], _leds["hex"]
                    if seq != seen and hx:
                        self.wfile.write(("data: %s\n\n" % hx).encode())
                    else:
                        self.wfile.write(b": ping\n\n")      # keep-alive
                    self.wfile.flush()
                    seen = seq
            except Exception:
                pass                       # client went away
        elif path == "/raw":
            fb = grab_raw()
            if fb:
                w, h, rgb = fb
                self._send(200, "application/octet-stream", rgb,
                           {"X-Width": str(w), "X-Height": str(h)})
            else:
                self._send(503, "text/plain", b"no framebuffer (is QEMU on the monitor socket?)")
        else:
            self._send(404, "text/plain", b"not found")

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(n).decode("utf-8", "replace") if n else ""
        path = self.path.split("?", 1)[0]
        if path == "/log/note":
            _logline("subucom", "·  " + body.strip()[:500])
            self._send(200, "text/plain", b"ok"); return
        if path in ("/ctrl", "/release", "/input", "/mainkey", "/mainkey_release", "/browse", "/usb"):
            _logline("subucom", "-> %-16s %s" % (path[1:], " | ".join(
                ln.strip() for ln in body.strip().split("\n") if ln.strip()) or "(all)"))
        elif path == "/jog" and "touch" in body:
            _logline("subucom", "-> jog              " + body.strip())
        if path == "/power":
            # Body: "on" | "on update" | "on test" | "off" | "toggle" | "toggle update".
            w = body.split()
            cur = _power_cmd("status")
            if cur is None:
                self._send(503, "text/plain", b"power control unavailable (not run by xdj-host)"); return
            want = w[0] if w else "toggle"
            upd = "update" in w[1:]
            test = "test" in w[1:] and not upd
            if want == "toggle":
                want = "off" if cur.startswith("on") else "on"
            if want == "on":
                if test:
                    # The keys must be down at rbp's first frame: cold boot with them held.
                    if cur.startswith("on"):
                        _power_cmd("off")
                        _leds_off()
                    with _browse_lock:
                        _apply_lines(list(TEST_MODE_KEYS))
                        _push()
                st = _power_cmd("on update" if upd else "on")
                if test:
                    threading.Thread(target=_test_mode_boot, args=(time.time(),), daemon=True).start()
                mode = " (firmware update mode)" if upd else " (test mode)" if test else ""
                _logline("subucom", "·  POWER ON" + mode)
                _logline("console", "──────── power on%s ────────" % mode)
                with _power_lock:
                    has_pending = bool(_pending_usb)
                if has_pending:
                    threading.Thread(target=_insert_pending, args=(upd,), daemon=True).start()
            else:
                st = _power_cmd("off")
                _leds_off()
                with _sock_lock:
                    for k in ("ctrl", "key"):
                        try:
                            if _conn[k]:
                                _conn[k].close()
                        except Exception:
                            pass
                        _conn[k] = None
                _logline("subucom", "·  POWER OFF")
                _logline("console", "──────── power off ────────")
            self._send(200, "text/plain", (st or "?").encode()); return
        if path == "/usb":
            parts = body.strip().split(" ", 1)
            if (len(parts) == 2 and parts[0] in ("usb1", "usb2") and os.path.exists(parts[1].strip())
                    and _power_cmd("status") == "off"):
                with _power_lock:
                    _pending_usb[parts[0]] = parts[1].strip()
                self._send(200, "text/plain", ("%s queued: %s (inserted after power on)"
                                               % (parts[0], parts[1].strip())).encode()); return
        if path == "/ctrl":
            # One spec per line. A = set an analog value (persists), B = press and
            # HOLD a button bit (until /release names it), a/b = one-shot,
            # S <off> <mask> <bits> = set a multi-position switch field (persists). Several
            # lines = simultaneous (e.g. the tempo fader's two complementary fields).
            lines = [ln.strip() for ln in body.strip().split("\n") if ln.strip()]
            if lines:
                try:
                    with _browse_lock:
                        shots = _apply_lines(lines)
                        _write_ctrl(_compose_ctrl(shots))
                    self._send(200, "text/plain", b"ok")
                except Exception as e:
                    self._send(500, "text/plain", str(e).encode())
            else:
                self._send(400, "text/plain", b"empty")
        elif path == "/release":
            # Body = the B lines to let go of (one button's bits). Empty body =
            # release EVERY held button (the panel's "Release all"). Analog values,
            # jog, browse counter and input select are kept.
            try:
                with _browse_lock:
                    names = [ln.split() for ln in body.strip().split("\n") if ln.strip()]
                    if not names:
                        _held.clear()
                        for j in _jog.values():
                            j["touch"] = 0
                    for parts in names:
                        if len(parts) == 3 and parts[0] in "Bb":
                            try:
                                _held.pop((int(parts[1], 16), int(parts[2], 16)), None)
                            except ValueError:
                                pass
                    _push()
            except Exception as e:
                self._send(500, "text/plain", str(e).encode())
                return
            self._send(200, "text/plain", b"ok")
        elif path == "/jog":
            # Jog wheel. Body: "<deck> move <degrees> <seconds>"  (platter turned by
            # <degrees> over <seconds>; +cw) or "<deck> touch <0|1>". rbp reads a
            # 16-bit position counter (deck +0x0e; it diffs successive values), a
            # pulse period (+0x0c; speed = 416.67/period, 0 = stopped) and flags
            # (+0x10: bit0 touch, bit1 reverse) -- FUN_00283dc4, code 0x4306.
            parts = body.strip().split()
            try:
                d = int(parts[0]); j = _jog[d]
                with _browse_lock:
                    if parts[1] == "touch":
                        j["touch"] = 1 if parts[2] == "1" else 0
                        j["active"] = True
                    elif parts[1] == "move":
                        deg, dt = float(parts[2]), max(1e-3, float(parts[3]))
                        ticks = int(round(deg / 360.0 * JOG_TICKS_PER_REV))
                        if ticks:
                            rps = abs(deg) / 360.0 / dt
                            speed = rps / JOG_NORMAL_RPS
                            j["cnt"] = (j["cnt"] + ticks) & 0xffff
                            j["per"] = max(1, min(0xfffe, int(round(JOG_SPEED_K / max(speed, 1e-3)))))
                            j["rev"] = 1 if ticks < 0 else 0
                            j["t"] = time.time(); j["active"] = True
                    _push()
                self._send(200, "text/plain", b"ok")
            except (IndexError, KeyError, ValueError):
                self._send(400, "text/plain", b"use '<1|2> move <deg> <sec>' or '<1|2> touch <0|1>'")
        elif path == "/input":
            # INPUT SELECT (per-channel source switch): body "<ch> DECK|LINE".
            # ch 0 = deck1/CH1, 1 = deck2/CH2. Persistent hold; default DECK.
            parts = body.strip().upper().split()
            if len(parts) == 2 and parts[0] in ("0", "1") and parts[1] in ("DECK", "LINE"):
                with _input_lock:
                    _input_src[int(parts[0])] = 1 if parts[1] == "DECK" else 0
                try:
                    with _browse_lock:
                        _push()
                    self._send(200, "text/plain", b"ok")
                except Exception as e:
                    self._send(500, "text/plain", str(e).encode())
            else:
                self._send(400, "text/plain", b"use '<0|1> DECK|LINE'")
        elif path == "/mainkey":
            # MAIN-UCOM direct GPIO key: body = Linux GPIO number. The QEMU hook
            # drives that pin LOW (pressed) while /tmp/mainkey holds the number.
            g = body.strip()
            if g.lstrip("-").isdigit():
                try:
                    _write_key(g)
                    self._send(200, "text/plain", b"ok")
                except Exception as e:
                    self._send(500, "text/plain", str(e).encode())
            else:
                self._send(400, "text/plain", b"bad gpio")
        elif path == "/mainkey_release":
            try:
                _write_key("")
            except Exception as e:
                self._send(500, "text/plain", str(e).encode())
                return
            self._send(200, "text/plain", b"ok")
        elif path == "/browse":
            # Browse ROTATE: body = signed detent count. +N scrolls the library
            # list DOWN (forward), -N scrolls UP (reverse). Each step nudges the
            # 16-bit counter at rbp[0x54]/[0x55] by +/-1 and holds it; a ~45 ms
            # dwell ensures every value is polled (faster collapses steps).
            try:
                n = int(body.strip() or "0")
            except ValueError:
                self._send(400, "text/plain", b"bad step"); return
            n = max(-500, min(500, n))

            def _ramp(count):
                global _browse_counter, _browse_active
                step = 1 if count > 0 else -1
                # Re-sync: the firmware reads delta = new-old of the frame counter.
                # If no 0x54 hold is currently in the ctrl file (first turn, or it was
                # cleared externally), the frame counter is 0 so the firmware's "old" is
                # 0 too -- reset to match, else the first write is one huge delta that
                # jumps to the list end.
                with _browse_lock:
                    _browse_active = True
                # Step one detent at a time, re-acquiring the lock each step so a push
                # (/ctrl) can interleave and still see the current counter.
                for _ in range(abs(count)):
                    with _browse_lock:
                        _browse_counter = (_browse_counter + step) & 0xffff
                        try:
                            _push()
                        except Exception:
                            break
                    time.sleep(0.045)

            if n:
                threading.Thread(target=_ramp, args=(n,), daemon=True).start()
            self._send(200, "text/plain", b"ok")
        elif path == "/usb":
            # Mount a USB on a front slot. Body: "<usb1|usb2> <path-to-image-or-device>".
            parts = body.strip().split(" ", 1)
            if len(parts) != 2 or parts[0] not in ("usb1", "usb2") or not parts[1].strip():
                self._send(400, "text/plain", b"usage: '<usb1|usb2> <path>'"); return
            slot, img = parts[0], parts[1].strip()
            if not os.path.exists(img):
                self._send(404, "text/plain", ("no such path: " + img).encode()); return
            if not (QMP or os.path.exists(MON)):
                self._send(503, "text/plain", b"no QEMU monitor (QMP/HMP) available for mount"); return
            try:
                self._send(200, "text/plain", mount_usb(slot, img).encode())
            except Exception as e:
                self._send(500, "text/plain", str(e).encode())
        elif path == "/savecal":
            try:
                json.loads(body)                       # validate
                os.makedirs(ASSETS, exist_ok=True)
                with open(CAL, "w") as f:
                    f.write(body)
                self._send(200, "text/plain", b"saved")
            except Exception as e:
                self._send(400, "text/plain", str(e).encode())
        else:
            self._send(404, "text/plain", b"not found")

    def log_message(self, *a):
        pass


def main():
    global MON, QMP, CTRL_SOCK, KEY_SOCK
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--mon", default=MON, help="QEMU monitor unix socket")
    ap.add_argument("--qmp", default=None, help="QEMU QMP unix socket (enables the USB LOAD "
                    "buttons in the .app, where there's no HMP monitor)")
    ap.add_argument("--ctrl-sock", default=None,
                    help="QEMU xdjctrl chardev socket (push sub-ucom snapshots here "
                         "instead of /tmp/subucom_ctrl)")
    ap.add_argument("--key-sock", default=None,
                    help="QEMU xdjkey chardev socket (push mainkey GPIO int here "
                         "instead of /tmp/mainkey)")
    ap.add_argument("--console", default=None,
                    help="guest console log file to show in View > System log (default: "
                         "console.log next to --ctrl-sock, else /tmp/xdj_console.log)")
    ap.add_argument("--power-sock", default=None,
                    help="xdj-host power control socket (default: power.sock beside --ctrl-sock)")
    args = ap.parse_args()
    global CONSOLE, POWER_SOCK
    POWER_SOCK = args.power_sock or (os.path.join(os.path.dirname(args.ctrl_sock), "power.sock")
                                     if args.ctrl_sock else None)
    CONSOLE = args.console or (os.path.join(os.path.dirname(args.ctrl_sock), "console.log")
                               if args.ctrl_sock and (os.path.exists(os.path.join(
                                   os.path.dirname(args.ctrl_sock), "console.log"))
                                   or os.path.exists(POWER_SOCK or "/nonexistent")
                                   or os.environ.get("XDJ_FW_DIR"))
                               else "/tmp/xdj_console.log")
    MON = args.mon
    QMP = args.qmp
    CTRL_SOCK = args.ctrl_sock
    KEY_SOCK = args.key_sock
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    socketserver.ThreadingTCPServer.daemon_threads = True
    with socketserver.ThreadingTCPServer(("127.0.0.1", args.port), Handler) as httpd:
        print("XDJ-RX virtual panel: http://127.0.0.1:%d  (monitor=%s)" % (args.port, MON))
        if CTRL_SOCK:
            print("control transport: socket  ctrl=%s key=%s" % (CTRL_SOCK, KEY_SOCK))
        else:
            print("control transport: /tmp files (%s, %s)" % (CTRL, MAINKEY))
        # Assert default INPUT-SELECT = DECK so a loaded deck is audible out of the
        # box (the emulator's native default is LINE/silent).
        try:
            _write_ctrl(_compose_ctrl([]))
            print("input-select default: CH1=DECK CH2=DECK")
        except Exception:
            pass
        threading.Thread(target=_jog_idle_loop, daemon=True).start()
        threading.Thread(target=_console_loop, daemon=True).start()
        print("console log: %s" % CONSOLE)
        threading.Thread(target=_leds_sock_loop if CTRL_SOCK else _leds_file_loop,
                         daemon=True).start()
        print("Ctrl-C to stop.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")


if __name__ == "__main__":
    main()
