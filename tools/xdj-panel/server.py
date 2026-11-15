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
    # Do NOT create the patch-06 file (/tmp/usb*_insert) here. Those GPIOs
    # (GPIO4_IO30 / GPIO7_IO12) are rbp's active-low USB *fault* flags, not
    # insert-detect lines; holding one low raises "USB Error. Remove the device."
    # once the slot mounts. Host1 resume on attach comes from patch 10 (USBNC OWIR).
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
    args = ap.parse_args()
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
        print("Ctrl-C to stop.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")


if __name__ == "__main__":
    main()
