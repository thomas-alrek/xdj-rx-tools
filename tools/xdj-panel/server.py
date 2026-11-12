#!/usr/bin/env python3
"""
Local bridge for the virtual XDJ-RX panel.

Serves index.html and turns panel actions into the emulator's control-injection
protocol, and streams the QEMU framebuffer to the browser as raw RGB (drawn on a
<canvas>) — no per-frame image encoding, over a persistent monitor connection:

  POST /ctrl     body="B 12 08"  -> write that spec line to /tmp/subucom_ctrl
  POST /release                  -> remove /tmp/subucom_ctrl (release held control)
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
import argparse, http.server, socketserver, socket, time, os, threading, json

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, "assets")
CAL = os.path.join(ASSETS, "calibration.json")
# Vendored noVNC (served at /novnc/* so panel.html's screen pane can use RFB-over-
# WebSocket when launched with ?vnc=ws://... -- the standalone-app Phase 2 path).
NOVNC = os.path.normpath(os.path.join(HERE, "..", "..", "app", "ui", "novnc"))
CTRL = "/tmp/subucom_ctrl"
MAINKEY = "/tmp/mainkey"        # MAIN-UCOM direct-key GPIO (USB1/USB2/rekordbox/MIDI)
MON = "/tmp/d_mon4.sock"
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


def _compose_ctrl(lines):
    """Build the ctrl-file body. Once the browse encoder has moved, the 16-bit
    counter at 0x54/0x55 MUST be present in EVERY frame: if a later frame drops
    it, the firmware reads 0x54 jumping back to 0 as a big rotate delta (so e.g.
    a push scrolls instead of drilling in). The QEMU harness (patch 07, updated)
    applies one-shot 'b'/'a' presses ON TOP of holds, so we no longer promote
    them to holds -- they stay one-shots (a sustained hold on the encoder push
    would trigger the wrong mode) and just ride alongside the held counter."""
    out = [ln.strip() for ln in lines if ln.strip()]
    if _browse_active and not any(l.lower().startswith("a 54") for l in out):
        out.append("A 54 %x" % _browse_counter)
    # Persistent INPUT-SELECT holds (DECK channels) ride alongside, deduped.
    have = {l.lower() for l in out}
    for hl in _input_hold_lines():
        if hl.lower() not in have:
            out.append(hl)
    return "\n".join(out) + "\n"


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
            # one spec per line; multiple lines = simultaneous holds (e.g. a tempo
            # fader driving both its complementary frame fields at once).
            lines = [ln.strip() for ln in body.strip().split("\n") if ln.strip()]
            if lines:
                try:
                    with _browse_lock:
                        _write_ctrl(_compose_ctrl(lines))
                    self._send(200, "text/plain", b"ok")
                except Exception as e:
                    self._send(500, "text/plain", str(e).encode())
            else:
                self._send(400, "text/plain", b"empty")
        elif path == "/release":
            try:
                with _browse_lock:
                    # Keep persistent state (browse counter + INPUT-SELECT holds);
                    # only the momentary control is dropped. _write_ctrl handles the
                    # empty-snapshot release for both transports.
                    _write_ctrl(_compose_ctrl([]))
            except Exception as e:
                self._send(500, "text/plain", str(e).encode())
                return
            self._send(200, "text/plain", b"ok")
        elif path == "/input":
            # INPUT SELECT (per-channel source switch): body "<ch> DECK|LINE".
            # ch 0 = deck1/CH1, 1 = deck2/CH2. Persistent hold; default DECK.
            parts = body.strip().upper().split()
            if len(parts) == 2 and parts[0] in ("0", "1") and parts[1] in ("DECK", "LINE"):
                with _input_lock:
                    _input_src[int(parts[0])] = 1 if parts[1] == "DECK" else 0
                try:
                    with _browse_lock:
                        # preserve any currently-held lines; swap only the source holds
                        cur = [l for l in _read_ctrl().splitlines() if l.strip()]
                        kept = [l for l in cur if l.lower()[:4] not in ("b 33", "b 43")]
                        _write_ctrl(_compose_ctrl(kept))
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
                    held = _read_ctrl().lower()
                    if "a 54" not in held:
                        _browse_counter = 0
                    _browse_active = True
                # Step one detent at a time, re-acquiring the lock each step so a push
                # (/ctrl) can interleave and still see the current counter.
                for _ in range(abs(count)):
                    with _browse_lock:
                        _browse_counter = (_browse_counter + step) & 0xffff
                        try:
                            _write_ctrl("\n".join(
                                ["A 54 %x" % _browse_counter] + _input_hold_lines()) + "\n")
                        except Exception:
                            break
                    time.sleep(0.045)

            if n:
                threading.Thread(target=_ramp, args=(n,), daemon=True).start()
            self._send(200, "text/plain", b"ok")
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
    global MON, CTRL_SOCK, KEY_SOCK
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--mon", default=MON, help="QEMU monitor unix socket")
    ap.add_argument("--ctrl-sock", default=None,
                    help="QEMU xdjctrl chardev socket (push sub-ucom snapshots here "
                         "instead of /tmp/subucom_ctrl)")
    ap.add_argument("--key-sock", default=None,
                    help="QEMU xdjkey chardev socket (push mainkey GPIO int here "
                         "instead of /tmp/mainkey)")
    args = ap.parse_args()
    MON = args.mon
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
        print("Ctrl-C to stop.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")


if __name__ == "__main__":
    main()
