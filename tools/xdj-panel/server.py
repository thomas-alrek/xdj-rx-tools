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

Usage:  python3 tools/xdj-panel/server.py [--port 8765] [--mon /tmp/d_mon4.sock]
Then open http://127.0.0.1:8765
"""
import argparse, http.server, socketserver, socket, time, os, threading

HERE = os.path.dirname(os.path.abspath(__file__))
CTRL = "/tmp/subucom_ctrl"
MON = "/tmp/d_mon4.sock"
PPM = "/tmp/xdjpanel_fb.ppm"

_mon_lock = threading.Lock()
_mon = None  # persistent monitor socket


def _mon_connect():
    global _mon
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(2.0)
    s.connect(MON)
    time.sleep(0.04)                       # let the banner arrive
    s.setblocking(False)
    try:
        while True:
            if not s.recv(65536):
                break
    except BlockingIOError:
        pass
    except Exception:
        pass
    s.setblocking(True)
    s.settimeout(2.0)
    _mon = s


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
    """Screendump via the persistent monitor connection; return (w, h, rgb) or None."""
    global _mon
    with _mon_lock:
        for attempt in (0, 1):
            try:
                if _mon is None:
                    _mon_connect()
                try:
                    os.remove(PPM)
                except FileNotFoundError:
                    pass
                _mon.sendall(('screendump "%s"\n' % PPM).encode())
                t0 = time.time()
                sz = -1
                while time.time() - t0 < 2.0:
                    if os.path.exists(PPM):
                        s2 = os.path.getsize(PPM)
                        if s2 > 1000 and s2 == sz:
                            break
                        sz = s2
                    time.sleep(0.003)
                # drain the monitor prompt
                _mon.setblocking(False)
                try:
                    while True:
                        if not _mon.recv(65536):
                            break
                except Exception:
                    pass
                _mon.setblocking(True)
                _mon.settimeout(2.0)
                with open(PPM, "rb") as f:
                    data = f.read()
                return _parse_ppm(data)
            except Exception:
                try:
                    _mon.close()
                except Exception:
                    pass
                _mon = None                                 # force reconnect on retry
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

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            try:
                with open(os.path.join(HERE, "index.html"), "rb") as f:
                    self._send(200, "text/html; charset=utf-8", f.read())
            except Exception as e:
                self._send(500, "text/plain", str(e).encode())
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
            spec = body.strip().replace("\n", " ")
            if spec:
                try:
                    with open(CTRL, "w") as f:
                        f.write(spec + "\n")
                    self._send(200, "text/plain", b"ok")
                except Exception as e:
                    self._send(500, "text/plain", str(e).encode())
            else:
                self._send(400, "text/plain", b"empty")
        elif path == "/release":
            try:
                os.remove(CTRL)
            except FileNotFoundError:
                pass
            except Exception as e:
                self._send(500, "text/plain", str(e).encode())
                return
            self._send(200, "text/plain", b"ok")
        else:
            self._send(404, "text/plain", b"not found")

    def log_message(self, *a):
        pass


def main():
    global MON
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--mon", default=MON, help="QEMU monitor unix socket")
    args = ap.parse_args()
    MON = args.mon
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    socketserver.ThreadingTCPServer.daemon_threads = True
    with socketserver.ThreadingTCPServer(("127.0.0.1", args.port), Handler) as httpd:
        print("XDJ-RX virtual panel: http://127.0.0.1:%d  (monitor=%s)" % (args.port, MON))
        print("Ctrl-C to stop.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")


if __name__ == "__main__":
    main()
