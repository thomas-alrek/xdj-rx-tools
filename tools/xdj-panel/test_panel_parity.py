#!/usr/bin/env python3
"""
Parity check: the in-process Rust panel (app/host/src/panel.rs, run by xdj-host) against
the reference Python server (tools/xdj-panel/server.py, socket mode).

Each server gets its own fake QEMU: a unix-socket server standing in for the xdjctrl /
xdjkey chardevs, recording every NUL-terminated snapshot and able to send rbp-style
"T <hex>" LED lines back. The same scripted panel session is played against both, then
compared: the snapshot sequence on each socket, every HTTP reply (status + body), the
/leds state and the control log (timestamps stripped). Power and USB are not driven
(they start QEMU / need a monitor); everything else the panel does is.

    tools/xdj-panel/test_panel_parity.py [--fw-dir <prepared firmware dir>]

Needs a built app/host/target/release/xdj-host and a prepared firmware dir for its
preflight (default: the newest dir in the app's cache). Exit 0 = identical.
"""
import argparse, glob, json, os, re, shutil, socket, subprocess, sys, tempfile, threading, time
import urllib.request

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
HOST = os.path.join(ROOT, "app", "host", "target", "release", "xdj-host")


class FakeChardev:
    """One QEMU chardev socket (server). Records NUL-terminated records."""

    def __init__(self, path):
        self.path, self.records, self.conn, self.lock = path, [], None, threading.Lock()
        self.srv = None

    def start(self):
        try:
            os.remove(self.path)
        except FileNotFoundError:
            pass
        self.srv = socket.socket(socket.AF_UNIX)
        self.srv.bind(self.path)
        self.srv.listen(4)
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            with self.lock:
                self.conn = c
            buf = b""
            while True:
                try:
                    d = c.recv(65536)
                except OSError:
                    break
                if not d:
                    break
                buf += d
                while b"\0" in buf:
                    rec, buf = buf.split(b"\0", 1)
                    with self.lock:
                        self.records.append(rec.decode())

    def send(self, line):
        with self.lock:
            if self.conn:
                self.conn.sendall(line.encode())


def http(port, method, path, body=""):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), method=method,
                                 data=body.encode() if method == "POST" else None)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def wait_port(port, t=20):
    t0 = time.time()
    while time.time() - t0 < t:
        try:
            socket.create_connection(("127.0.0.1", port), 0.5).close()
            return True
        except OSError:
            time.sleep(0.2)
    return False


def sse_lines(port, path, seconds=1.0):
    """Collect 'data:' lines from an SSE stream for a while."""
    s = socket.create_connection(("127.0.0.1", port))
    s.sendall(("GET %s HTTP/1.1\r\nHost: x\r\n\r\n" % path).encode())
    s.settimeout(0.2)
    buf, t0 = b"", time.time()
    while time.time() - t0 < seconds:
        try:
            d = s.recv(65536)
            if not d:
                break
            buf += d
        except socket.timeout:
            pass
    s.close()
    body = buf.split(b"\r\n\r\n", 1)[-1].decode("utf-8", "replace")
    return [l[6:] for l in body.split("\n") if l.startswith("data: ")]


SESSION = [
    # (method, path, body, settle seconds)
    ("POST", "/ctrl", "A 46 200\nA 44 200\nS 5b c0 0\nA 48 200\nA 2e 200\nA 3e 3ff", 0.1),
    ("POST", "/input", "0 DECK", 0.1),
    ("POST", "/input", "1 LINE", 0.1),
    ("POST", "/ctrl", "B 13 40", 0.1),                 # PLAY held
    ("POST", "/ctrl", "B 12 01\nB 21 08", 0.1),        # SHIFT + a pad
    ("POST", "/release", "B 13 40", 0.1),
    ("POST", "/ctrl", "b 1b 01", 0.1),                 # one-shot MENU
    ("POST", "/ctrl", "a 2c 1ff", 0.1),                # one-shot analog
    ("POST", "/ctrl", "S 5a e0 20\nS 5a 1c 08", 0.1),  # switches
    ("POST", "/ctrl", "S 5a e0 0", 0.1),
    ("POST", "/ctrl", "A 54 7", 0.1),                  # browse counter is owned by /browse
    ("POST", "/ctrl", "B 0x13 0x04\nA -1 5\nZ 1 2\nB zz 1\nA 30", 0.1),  # odd/malformed lines
    ("POST", "/ctrl", "", 0.1),                        # empty -> 400
    ("POST", "/jog", "1 touch 1", 0.1),
    ("POST", "/jog", "1 move 30 0.05", 0.4),           # move, then idle -> period 0
    ("POST", "/jog", "1 move -12.5 0.1", 0.4),
    ("POST", "/jog", "2 move 0.1 0.01", 0.4),          # rounds to 0 ticks
    ("POST", "/jog", "2 move 720 0.5", 0.4),
    ("POST", "/jog", "1 touch 0", 0.1),
    ("POST", "/jog", "3 touch 1", 0.1),                # bad deck -> 400
    ("POST", "/jog", "1 spin", 0.1),                   # bad verb -> 400
    ("POST", "/browse", "3", 0.5),
    ("POST", "/browse", "-2", 0.4),
    ("POST", "/browse", "x", 0.1),                     # -> 400
    ("POST", "/input", "2 DECK", 0.1),                 # -> 400
    ("POST", "/input", "1 deck", 0.1),
    ("POST", "/mainkey", "162", 0.1),
    ("POST", "/mainkey_release", "", 0.1),
    ("POST", "/mainkey", "abc", 0.1),                  # -> 400
    ("POST", "/release", "", 0.1),                     # release all
    ("POST", "/log/note", "hello from the parity test", 0.1),
    ("GET", "/nope", "", 0.0),
    ("POST", "/nope", "", 0.0),
]


def run_one(name, port, cfg_dir, start_server):
    ctrl = FakeChardev(os.path.join(cfg_dir, "ctrl.sock"))
    key = FakeChardev(os.path.join(cfg_dir, "key.sock"))
    proc = start_server()
    if not wait_port(port):
        proc.kill()
        sys.exit("%s: panel port %d never opened" % (name, port))
    ctrl.start(); key.start()
    time.sleep(1.5)                                    # connect + the 0.3 s re-push settle
    replies = []
    for m, path, body, settle in SESSION:
        replies.append((m, path, body) + http(port, m, path, body))
        time.sleep(settle)
    # LED back-channel
    ctrl.send("T " + "11" * 20 + "\n"); time.sleep(0.3)
    ctrl.send("T " + "11" * 19 + "13 05\n"); time.sleep(0.3)
    ctrl.send("T zz\n"); time.sleep(0.3)               # ignored
    leds = json.loads(http(port, "GET", "/leds?once=1")[1])
    led_sse = sse_lines(port, "/leds", 0.8)
    log = [re.sub(r"^\d\d:\d\d:\d\d  ", "", l) for l in sse_lines(port, "/log/subucom", 1.0)]
    pages = [http(port, "GET", p)[0] for p in ("/", "/logs.html", "/novnc/core/rfb.js",
                                               "/knobs/../../../etc/passwd", "/raw")]
    time.sleep(0.3)
    proc.terminate()
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        proc.kill()
    return {"ctrl": list(ctrl.records), "key": list(key.records), "replies": replies,
            "leds": (leds.get("hex"), leds.get("seq")), "led_sse": led_sse, "log": log,
            "pages": pages}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fw-dir", default=None)
    a = ap.parse_args()
    fw = a.fw_dir
    if not fw:
        cands = sorted(glob.glob(os.path.expanduser("~/Library/Application Support/XDJ-RX/*/prep-v3.stamp")),
                       key=os.path.getmtime)
        if not cands:
            sys.exit("need --fw-dir (a prepared firmware dir)")
        fw = os.path.dirname(cands[-1])
    tmp = tempfile.mkdtemp(prefix="xpp", dir="/tmp")
    py_dir, rs_dir = os.path.join(tmp, "py"), os.path.join(tmp, "rs")
    os.makedirs(py_dir); os.makedirs(rs_dir)
    try:
        py = run_one("python", 8811, py_dir, lambda: subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "tools", "xdj-panel", "server.py"), "--port", "8811",
             "--ctrl-sock", os.path.join(py_dir, "ctrl.sock"), "--key-sock", os.path.join(py_dir, "key.sock"),
             "--console", os.path.join(py_dir, "console.log")],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        env = dict(os.environ, XDJ_REPO=ROOT, XDJ_FW_DIR=fw, XDJ_RUNTIME_DIR=rs_dir,
                   XDJ_PANEL_PORT="8812", XDJ_VNC_WS_PORT="6112", XDJ_AUTOSTART="0", XDJAUDIO="none")
        env.pop("XDJ_NAND", None)
        rs = run_one("rust", 8812, rs_dir, lambda: subprocess.Popen(
            [HOST], env=env, cwd=ROOT, stdout=subprocess.DEVNULL,
            stderr=open(os.path.join(tmp, "host.log"), "w")))
    finally:
        pass
    bad = 0
    for k in ("ctrl", "key", "replies", "leds", "led_sse", "log", "pages"):
        if py[k] == rs[k]:
            print("  OK    %-8s (%d items)" % (k, len(py[k]) if isinstance(py[k], list) else 1))
            continue
        bad += 1
        print("  DIFF  %s" % k)
        pa, ra = (py[k], rs[k]) if isinstance(py[k], list) else ([py[k]], [rs[k]])
        for i in range(max(len(pa), len(ra))):
            x = pa[i] if i < len(pa) else "<missing>"
            y = ra[i] if i < len(ra) else "<missing>"
            if x != y:
                print("     #%d\n       py: %r\n       rs: %r" % (i, x, y))
    shutil.rmtree(tmp, ignore_errors=True)
    print("identical" if not bad else "%d section(s) differ" % bad)
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
