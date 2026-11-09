#!/usr/bin/env python3
"""
Local bridge for the virtual XDJ-RX panel.

Serves index.html and turns panel actions into the emulator's control-injection
protocol:
  POST /ctrl     body="B 12 08"  -> write that spec line to /tmp/subucom_ctrl
  POST /release                  -> remove /tmp/subucom_ctrl (release held control)
  GET  /screen                   -> live QEMU framebuffer as PNG (via monitor socket)

The QEMU xdj-subucom harness reads /tmp/subucom_ctrl each frame:
  b/a = one-shot press+release ; B/A = hold while the file exists (release on rm).
Only ONE control occupies the file at a time, so the panel holds the
most-recently-touched control and releases it on mouse-up.

Usage:  python3 tools/xdj-panel/server.py [--port 8765] [--mon /tmp/d_mon4.sock]
Then open http://127.0.0.1:8765
"""
import argparse, http.server, socketserver, socket, time, subprocess, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
CTRL = "/tmp/subucom_ctrl"
MON = "/tmp/d_mon4.sock"
PPM = "/tmp/xdjpanel_screen.ppm"
PNG = "/tmp/xdjpanel_screen.png"


def screendump():
    """Grab the QEMU framebuffer via the monitor socket, return PNG bytes or None."""
    try:
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(2.0)
        s.connect(MON)
        time.sleep(0.03)
        try:
            while True:
                if not s.recv(4096):
                    break
        except Exception:
            pass
        s.sendall(('screendump "%s"\n' % PPM).encode())
        time.sleep(0.45)
        s.close()
    except Exception:
        return None
    # convert PPM -> PNG (sips is always present on macOS)
    r = subprocess.run(["sips", "-s", "format", "png", PPM, "--out", PNG],
                       capture_output=True)
    if r.returncode != 0:
        return None
    try:
        with open(PNG, "rb") as f:
            return f.read()
    except Exception:
        return None


class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, code, ctype, body, cache=False):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if not cache:
            self.send_header("Cache-Control", "no-store")
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
        elif path == "/screen":
            png = screendump()
            if png:
                self._send(200, "image/png", png)
            else:
                self._send(503, "text/plain", b"no screen (is QEMU on the monitor socket?)")
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
    with socketserver.ThreadingTCPServer(("127.0.0.1", args.port), Handler) as httpd:
        print("XDJ-RX virtual panel: http://127.0.0.1:%d  (monitor=%s)" % (args.port, MON))
        print("Ctrl-C to stop.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")


if __name__ == "__main__":
    main()
