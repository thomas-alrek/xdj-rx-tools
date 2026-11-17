#!/usr/bin/env python3
"""
End-to-end regression test for the emulator + desktop-app host, on a throwaway unit.

    tools/test/regress.py [--fw build/XDJ-RX_v221.zip] [--unit <installed fw dir>]
                          [--only provision,boot,panel,usb,testmode,custom] [--keep]

Runs the real xdj-host (release build) with a private $HOME / runtime dir / ports, so it
never touches the app's cache or a running app. Stages:
  prep       xdj-host prep <fw>: decrypt, boot blobs, update stick
  provision  blank unit installs itself (ALL update) -> installed, then boots from NAND by itself
  boot       normal NAND boot -> home screen
  panel      MENU held opens UTILITY, a short MENU tap leaves it; rbp's LED stream is live;
             MCP: handshake, tool list, a named press, a PNG screenshot
  usb        BROWSE, panel USB LOAD of a FAT32 library stick (test/usb/gen_lib_fat32.img, else the
             first non-exFAT test/usb/*.img) on USB1 -> its library appears
  standby    POWER off while rbp runs: POWER key -> rbp prepares -> TX 'S' -> power cut (< 10 s)
  testmode   File > Power On in Test Mode -> VERSION INFORMATION screen
  custom     tools/fw/make-test-upd.sh TEST.UPD (2.30) installed via File > Install Firmware
             Update (POST /install-update) ->
             env release=2.30, both console markers, test mode right after the update
--unit clones an already-installed firmware dir (skips provision; ~2 min faster).
--app dist/XDJ-RX.app tests the packaged app (bundled host, relocated QEMU, panel files).
Screens are classified from pixel statistics (no reference images: the UI is Pioneer's).
Exit 0 = all selected stages passed. Needs ~8 min and nothing else heavy running.
"""
import argparse, glob, json, os, shutil, socket, subprocess, sys, tempfile, time, urllib.request

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
HOST = os.path.join(ROOT, "app/host/target/release/xdj-host")
XDJ = os.path.join(ROOT, "crates/xdj-fw/target/release/xdj")
PORT, WS = 8797, 6097
RESULTS = []
APP_RES = None   # --app: run the packaged app's host/QEMU/panel instead of the build tree


def log(*a):
    print(*a, flush=True)


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    log("  %s  %-28s %s" % ("PASS" if ok else "FAIL", name, detail))
    return ok


class Unit:
    def __init__(self, work, fw_dir):
        self.work, self.fw = work, fw_dir
        self.run = os.path.join(work, "run")
        self.proc = None

    def env(self):
        extra = {"XDJ_RESOURCES": APP_RES} if APP_RES else {}
        return dict(os.environ, **extra, HOME=os.path.join(self.work, "home"), XDJ_REPO=ROOT, XDJ_FW_DIR=self.fw,
                    XDJ_RUNTIME_DIR=self.run, XDJ_PANEL_PORT=str(PORT), XDJ_VNC_WS_PORT=str(WS),
                    XDJAUDIO="none", XDJ_AUTOSTART="0", XDJ_FRAME_LOG="1")

    def start(self):
        os.makedirs(self.run, exist_ok=True)
        self.proc = subprocess.Popen([HOST], env=self.env(), cwd=ROOT, stdout=subprocess.DEVNULL,
                                     stderr=open(os.path.join(self.work, "host.log"), "a"))
        t0 = time.time()
        while time.time() - t0 < 30:
            try:
                self.http("GET", "/power")
                return
            except Exception:
                time.sleep(0.3)
        raise RuntimeError("panel never came up (see %s/host.log)" % self.work)

    def stop(self):
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None

    def http(self, method, path, body=""):
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (PORT, path), method=method,
                                     data=body.encode() if method == "POST" else None)
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.read().decode("utf-8", "replace")

    def mcp(self, method, params=None):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
        return json.loads(self.http("POST", "/mcp", body))["result"]

    def console(self):
        try:
            return open(os.path.join(self.run, "console.log"), "rb").read().decode("utf-8", "replace")
        except FileNotFoundError:
            return ""

    def wait_console(self, marker, timeout):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if marker in self.console():
                return time.time() - t0
            time.sleep(1)
        return None

    def tap(self, spec, ms=0.16):
        self.http("POST", "/ctrl", spec)
        time.sleep(ms)
        self.http("POST", "/release", spec)

    def env_nand(self):
        out = subprocess.run([XDJ, "nand", "env", os.path.join(self.fw, "nand-flash.img")],
                             capture_output=True, text=True).stdout
        return dict(l.split("=", 1) for l in out.splitlines() if "=" in l)

    def shot(self, name):
        path = os.path.join(self.work, name + ".ppm")
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(10)
        s.connect(os.path.join(self.run, "qmp.sock"))
        f = s.makefile("rw")
        f.readline()
        f.write('{"execute":"qmp_capabilities"}\n'); f.flush(); f.readline()
        f.write(json.dumps({"execute": "screendump", "arguments": {"filename": path}}) + "\n"); f.flush()
        f.readline()
        s.close()
        return ppm(path)


def ppm(path):
    d = open(path, "rb").read()
    parts, i = [], 2
    while len(parts) < 3:
        while d[i:i + 1].isspace():
            i += 1
        j = i
        while not d[j:j + 1].isspace():
            j += 1
        parts.append(int(d[i:j])); i = j
    w, h = parts[0], parts[1]
    return w, h, d[i + 1:i + 1 + w * h * 3]


def stats(img):
    """Fractions over a sample grid: black, white, saturated colour, the update box blue, red."""
    w, h, px = img
    n = blk = wht = col = blue = red = 0
    for y in range(0, h, 4):
        for x in range(0, w, 4):
            o = (y * w + x) * 3
            r, g, b = px[o], px[o + 1], px[o + 2]
            n += 1
            if max(r, g, b) < 24: blk += 1
            elif min(r, g, b) > 200: wht += 1
            if max(r, g, b) - min(r, g, b) > 60: col += 1
            if b > 100 and r < 70 and 40 < g < 120 and b - r > 60: blue += 1
            if r > 150 and g < 70 and b < 70: red += 1
    return {k: round(v / n, 4) for k, v in dict(black=blk, white=wht, colour=col, blue=blue, red=red).items()}


def is_testmode(s):     # white text on black, no colour (VERSION INFORMATION)
    return s["black"] > 0.85 and s["white"] > 0.005 and s["colour"] < 0.01


def is_home(s):         # the deck screen: colour everywhere, red "Not Loaded."/QUANTIZE
    return s["colour"] > 0.03 and s["red"] > 0.001 and s["black"] < 0.9


def is_update_done(s):  # "Firmware update is complete." box + red progress bars
    return s["blue"] > 0.02 and s["red"] > 0.01


def diff(a, b):
    pa, pb = a[2], b[2]
    n = changed = 0
    for o in range(0, min(len(pa), len(pb)), 3 * 31):
        n += 1
        if abs(pa[o] - pb[o]) + abs(pa[o + 1] - pb[o + 1]) + abs(pa[o + 2] - pb[o + 2]) > 60:
            changed += 1
    return changed / max(n, 1)


def boot(u, mode, name, settle=8):
    u.http("POST", "/power", mode)
    t = u.wait_console("Resolve xrun", 300)
    if t is None:
        return None
    time.sleep(settle)
    s = stats(u.shot(name))
    return t, s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fw", default=os.path.join(ROOT, "build/XDJ-RX_v221.zip"))
    ap.add_argument("--unit", help="clone this installed firmware dir instead of provisioning")
    ap.add_argument("--only", default="provision,boot,panel,usb,standby,testmode,custom")
    ap.add_argument("--keep", action="store_true", help="keep the work dir")
    ap.add_argument("--app", help="test a packaged XDJ-RX.app (its xdj-host, QEMU and panel)")
    a = ap.parse_args()
    global HOST, APP_RES
    if a.app:
        APP_RES = os.path.join(os.path.abspath(a.app), "Contents", "Resources")
        HOST = os.path.join(APP_RES, "xdj-host")
    only = set(a.only.split(","))
    for b in (HOST, XDJ):
        if not os.path.exists(b):
            sys.exit("build first: %s" % b)
    work = tempfile.mkdtemp(prefix="xr", dir="/tmp")      # short: unix socket paths <= 104
    log("work dir: %s" % work)
    try:
        if a.unit:
            fw = os.path.join(work, "fw")
            subprocess.run(["cp", "-c", "-R", a.unit, fw], check=True)
            log("== unit: cloned %s" % a.unit)
        else:
            log("== prep")
            out = subprocess.run([HOST, "prep", a.fw], capture_output=True, text=True,
                                 env=dict(os.environ, HOME=os.path.join(work, "home"), XDJ_REPO=ROOT))
            fw = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else ""
            need = ["vmlinux.bin", "tramp.bin", "atags.bin", "fw.iso", "update-stick.img", "prep-v3.stamp"]
            if not check("prep", fw and all(os.path.exists(os.path.join(fw, f)) for f in need), fw or out.stderr[-200:]):
                return 1
        u = Unit(work, fw)
        u.start()
        if not a.unit and "provision" in only:
            log("== provision (blank unit, ALL update)")
            t0 = time.time()
            while time.time() - t0 < 1500 and u.env_nand().get("release") != "2.21":
                time.sleep(10)
            ok = u.env_nand().get("release") == "2.21"
            check("provision: installed", ok, "release=%s after %ds" % (u.env_nand().get("release"), time.time() - t0))
            # the host sees release= in the NAND env, cuts power and boots the unit from NAND
            t1 = time.time()
            while time.time() - t1 < 60 and json.loads(u.http("GET", "/power")).get("state") != "on":
                time.sleep(2)
            check("provision: boots from NAND", json.loads(u.http("GET", "/power")).get("state") == "on",
                  "after %ds" % (time.time() - t1))
            u.http("POST", "/power", "off"); time.sleep(3)
        if only & {"boot", "panel", "usb"}:
            log("== normal boot")
            r = boot(u, "on", "home")
            ok = r is not None and is_home(r[1])
            check("boot: home screen", ok, "rbp up after %.0fs %s" % (r[0], r[1]) if r else "never booted")
            if "panel" in only and r:
                # MENU must be HELD (~1 s+) to open UTILITY; a short tap does nothing on the home screen
                before = u.shot("menu0")
                u.tap("B 5f 01", 2.0); time.sleep(2)
                after = u.shot("menu1")
                d = diff(before, after)
                check("panel: MENU (held) opens UTILITY", d > 0.05, "%.1f%% changed" % (100 * d))
                u.tap("B 5f 01"); time.sleep(2)                       # short MENU tap leaves UTILITY
                d = diff(before, u.shot("menu2"))
                check("panel: MENU tap leaves UTILITY", d < 0.05, "%.1f%% differs from home" % (100 * d))
                leds = json.loads(u.http("GET", "/leds?once=1"))
                check("panel: LED stream", leds.get("seq", 0) > 0 and len(leds.get("hex", "")) >= 40, "seq=%s" % leds.get("seq"))
                # MCP (POST /mcp): handshake, tool list, a named control, a PNG screenshot
                init = u.mcp("initialize", {"protocolVersion": "2025-06-18"})
                tools = [t["name"] for t in u.mcp("tools/list")["tools"]]
                check("mcp: initialize + tools", init.get("serverInfo", {}).get("name") == "xdj-rx"
                      and {"load_firmware", "press", "set", "insert_usb", "screenshot"} <= set(tools), "%d tools" % len(tools))
                before = u.shot("mcp0")
                r = u.mcp("tools/call", {"name": "press", "arguments": {"control": "menu", "hold_ms": 2000}})
                time.sleep(2)
                d = diff(before, u.shot("mcp1"))
                check("mcp: press menu (held)", not r.get("isError") and d > 0.05, "%.1f%% changed" % (100 * d))
                u.mcp("tools/call", {"name": "press", "arguments": {"control": "menu"}}); time.sleep(2)
                img = u.mcp("tools/call", {"name": "screenshot", "arguments": {}})["content"][0]
                import base64
                png = base64.b64decode(img.get("data", "")) if img.get("type") == "image" else b""
                check("mcp: screenshot PNG", png[:8] == b"\x89PNG\r\n\x1a\n" and png[16:24] == bytes.fromhex("00000320000001e0"),
                      "%d bytes" % len(png))
            if "usb" in only and r:
                # a FAT32 rekordbox library stock firmware reads: the generated one
                # (tools/test/make-test-sticks.sh), else the first stick that isn't exFAT
                img = sorted(glob.glob(os.path.join(ROOT, "test/usb/*.img")))
                pref = os.path.join(ROOT, "test/usb/gen_lib_fat32.img")
                img = [pref] if pref in img else [i for i in img if "exfat" not in os.path.basename(i).lower()]
                if img:
                    # BROWSE shows USB1 ("NO USB" until a stick mounts); the library replaces it
                    u.tap("B 5f 08"); time.sleep(3)
                    before = u.shot("browse0")
                    stick = os.path.join(u.work, "usb1.img")   # slots are read/write: keep the fixture pristine
                    # an APFS clone where possible (no space used; the images are 1 GB sparse)
                    if subprocess.run(["cp", "-c", img[0], stick], capture_output=True).returncode:
                        shutil.copyfile(img[0], stick)
                    msg = u.http("POST", "/usb", "usb1 " + stick)
                    t0, d = time.time(), 0.0
                    while time.time() - t0 < 40 and d < 0.03:
                        time.sleep(3)
                        d = diff(before, u.shot("browse1"))
                    check("usb: library shown in browser", "mounted" in msg and d > 0.03,
                          "%s; %.1f%% of the screen changed" % (msg.strip(), 100 * d))
                    u.tap("B 5f 08"); time.sleep(1)
                else:
                    log("  skip usb (no test/usb/*.img)")
        if "standby" in only:
            log("== POWER off = standby handshake")
            if not u.console() or "commandline read: rbp" not in u.console():
                boot(u, "on", "pre_standby")
            t0 = time.time()
            r = u.http("POST", "/power", "off")
            took = time.time() - t0
            check("standby: rbp answered 'S', power cut", r.strip() == "off" and took < 10, "%.1fs" % took)
        if "testmode" in only:
            log("== test mode")
            # Known flake (~1 in 8, docs/service-modes.md): rbp sometimes boots normal although the
            # keys are in its first frame. One retry; the result says if it was needed.
            r = boot(u, "on test", "testmode")
            note = ""
            if not (r and is_testmode(r[1])):
                u.http("POST", "/power", "off"); time.sleep(3)
                r = boot(u, "on test", "testmode2")
                note = " (passed on retry: known rbp-side flake)"
            check("testmode: VERSION screen", r is not None and is_testmode(r[1]), (str(r[1]) if r else "never booted") + note)
        if "custom" in only:
            # one step above what's installed (an equal release takes the updater's other path)
            try:
                cur = float(u.env_nand().get("release", "0"))
            except ValueError:
                cur = 0.0
            target = "%.2f" % max(2.30, round(cur + 0.01, 2))
            log("== custom firmware (TEST.UPD %s over %s)" % (target, u.env_nand().get("release")))
            tdir = os.path.join(work, "test-upd")
            b = subprocess.run([os.path.join(ROOT, "tools/fw/make-test-upd.sh"), a.fw, target, tdir],
                               capture_output=True, text=True, cwd=ROOT)
            if check("custom: build TEST.UPD", b.returncode == 0, b.stdout.strip().splitlines()[-1] if b.stdout else b.stderr[-200:]):
                # File > Install Firmware Update: the panel builds the stick + boots update mode
                msg = u.http("POST", "/install-update", os.path.join(tdir, "TEST.UPD"))
                check("custom: install-update accepted", msg.startswith("installing firmware " + target), msg.strip())
                t0 = time.time()
                while time.time() - t0 < 900 and u.env_nand().get("release") != target:
                    time.sleep(10)
                ok = u.env_nand().get("release") == target
                con = u.console()
                check("custom: installed " + target, ok and ("signature ok. version = " + target) in con
                      and "USER UPDATE" in con, "after %ds" % (time.time() - t0))
                time.sleep(15)
                s = stats(u.shot("custom_done"))
                check("custom: complete screen", is_update_done(s), str(s))
                u.http("POST", "/power", "off"); time.sleep(3)
                r = boot(u, "on test", "custom_testmode")
                con = u.console()
                check("custom: rootfs marker", "xdj-fw TEST: rootfs.cramfs rebuilt" in con)
                check("custom: pioneer marker", "xdj-fw TEST: pioneer.tar.gz rebuilt" in con)
                check("custom: test mode after update", r is not None and is_testmode(r[1]), str(r[1]) if r else "never booted")
        u.stop()
    finally:
        try:
            u.stop()
        except Exception:
            pass
        fails = [n for n, ok, _ in RESULTS if not ok]
        log("\n%d passed, %d failed%s" % (len(RESULTS) - len(fails), len(fails), (": " + ", ".join(fails)) if fails else ""))
        if a.keep or fails:
            log("work dir kept: %s (host.log, run/console.log, run/qemu.log, *.ppm)" % work)
        else:
            shutil.rmtree(work, ignore_errors=True)
    return 1 if [n for n, ok, _ in RESULTS if not ok] else 0


if __name__ == "__main__":
    sys.exit(main())
