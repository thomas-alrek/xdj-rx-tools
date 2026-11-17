#!/usr/bin/env python3
"""Stress test a custom firmware on a running emulated unit (xdj-host), until a deadline.

    python3 tools/test/stress.py --until 08:00 [--port 18765] [--unit build/flactest] [--seed N]
                                 [--out build/stress] [--safe-starts N]

Drives every 3.00 feature over and over, in random order: USB sticks (exFAT library, FAT32
library with coloured hot cues A-H, FLAC/WAV folders, a 10,000-track library) inserted, swapped
and pulled - also while playing (the sticks of tools/test/make-test-sticks.sh [--big]; a missing
one is skipped); browsing and loading MP3 / FLAC / hi-res WAV; play, jog,
tempo, crossfader; hot cues A-D, SHIFT + pad hot cues E-H, beat jump; every UTILITY setting;
[FOLDER] / Contents; normal and safe-start power cycles.

After every action it checks the unit: rbp still running (host status), no crash text on the
console (segfault, abort, kernel oops, relocation error, ...), and frames still being drawn
(the fps mod logs every 10 s). On a failure it saves the reason, the console, a screenshot and
the last actions to OUT/crashes/NNN/, power-cycles and carries on. OUT/report.md is the
running summary. Needs Pillow (screen detection) - any venv with it.
"""
import argparse, base64, datetime, io, json, os, random, re, shutil, subprocess, sys, time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ap = argparse.ArgumentParser()
ap.add_argument("--until", default="08:00")
ap.add_argument("--port", type=int, default=18765)
ap.add_argument("--unit", default=os.path.join(ROOT, "build/flactest"))
ap.add_argument("--out", default=os.path.join(ROOT, "build/stress"))
ap.add_argument("--seed", type=int, default=int(time.time()))
ap.add_argument("--safe-starts", type=int, default=0, metavar="N",
                help="only power-cycle N times with safe start (hold MIDI), then a normal boot; report and exit")
A = ap.parse_args()
from PIL import Image  # noqa: E402

P = f"http://127.0.0.1:{A.port}"
CONSOLE = os.path.join(A.unit, "run/console.log")
OUT = A.out
os.makedirs(os.path.join(OUT, "crashes"), exist_ok=True)
os.makedirs(os.path.join(OUT, "usb"), exist_ok=True)
rng = random.Random(A.seed)
hh, mm = map(int, A.until.split(":"))
now = datetime.datetime.now()
DEADLINE = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
if DEADLINE <= now:
    DEADLINE += datetime.timedelta(days=1)

STICKS = {  # name: (image, kind)
    "exfat_lib": ("test/usb/gen_lib_exfat.img", "library"),
    "fat_cues": ("test/usb/gen_lib_fat32.img", "library"),
    "flac_folder": ("test/usb/gen_flac.img", "folder"),
    "wav_folder": ("test/usb/gen_wav.img", "folder"),
    "big10k": ("test/usb/gen_big10k.img", "library"),
}
STICKS = {k: v for k, v in STICKS.items() if os.path.exists(os.path.join(ROOT, v[0]))}
if not STICKS:
    sys.exit("no test stick images found (test/usb/*.img)")
CRASH_RE = re.compile(r"Segmentation fault|SIGSEGV|Aborted|Illegal instruction|Bus error|"
                      r"Kernel panic|Unable to handle kernel|Internal error: Oops|"
                      r"relocation error|terminate called|double free|stack smashing|"
                      r"edbmalloc NG|Out of memory|oom-killer", re.I)

actions_log = []           # (time, action) - recent history
stats = {"actions": 0, "boots": 0, "crashes": 0, "safe_starts": 0, "by_action": {}}
crashes = []
console_pos = 0
expect_reset = True        # the console file restarts at a boot we started; otherwise = reboot
last_fps_seen = time.time()
xrun_count = 0


def log(msg):
    line = f"{datetime.datetime.now():%H:%M:%S} {msg}"
    print(line, flush=True)
    with open(os.path.join(OUT, "log.txt"), "a") as f:
        f.write(line + "\n")


def http(method, path, data=None, timeout=60):
    req = urllib.request.Request(P + path, data=data.encode() if isinstance(data, str) else data, method=method)
    if isinstance(data, (dict, list)):
        req.data = json.dumps(data).encode()
        req.add_header("content-type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def mcp(tool, args=None, timeout=90):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args or {}}}
    r = json.loads(http("POST", "/mcp", body, timeout))
    return r.get("result", {}).get("content", [])


def press(c, ms=160):
    mcp("press", {"control": c, "hold_ms": ms}, timeout=60 + ms // 1000)


def raw(lines):
    mcp("send_raw", {"lines": lines})


def release(lines):
    mcp("release", {"lines": lines})


def browse(n):
    mcp("browse", {"steps": n})


def screenshot():
    c = mcp("screenshot")
    return Image.open(io.BytesIO(base64.b64decode(c[0]["data"]))).convert("RGB")


def screen():
    """'browser', 'deck', 'utility' or 'other' (pixel (400, 18): header colours)."""
    try:
        r, g, b = screenshot().getpixel((400, 18))
    except Exception:
        return "other"
    if abs(r - 33) < 8 and abs(g - 60) < 8 and abs(b - 90) < 8:
        return "browser"
    if abs(r - 24) < 8 and abs(g - 32) < 8 and abs(b - 41) < 8:
        return "deck"
    if abs(r - 41) < 8 and abs(g - 44) < 8 and abs(b - 49) < 8:
        return "utility"
    return "other"


def status():
    try:
        return json.loads(mcp("status")[0]["text"])
    except Exception as e:
        return {"error": str(e)}


def read_console():
    """New console text since the last call (the file restarts at every boot)."""
    global console_pos
    try:
        size = os.path.getsize(CONSOLE)
    except OSError:
        return ""
    if size < console_pos:
        console_pos = 0
        if not expect_reset:
            raise Crash("the unit restarted on its own (console log began again)")
    with open(CONSOLE, "rb") as f:
        f.seek(console_pos)
        data = f.read()
    console_pos += len(data)
    if data:
        globals()["expect_reset"] = False
    return data.decode("latin-1")


# ---- health -----------------------------------------------------------------------

class Crash(Exception):
    pass


def check(context):
    """Raise Crash if the unit isn't healthy."""
    global last_fps_seen, xrun_count
    text = read_console()
    if "fps: P=" in text:
        last_fps_seen = time.time()
    xrun_count += text.count("Resolve xrun")
    m = CRASH_RE.search(text)
    if m:
        s = max(0, m.start() - 600)
        raise Crash(f"console: {m.group(0)!r} near: {text[s:m.end() + 400]!r}")
    st = status()
    if "error" in st:
        raise Crash(f"host status unavailable: {st['error']}")
    if st.get("power", {}).get("state") != "on":
        raise Crash(f"unit power state {st.get('power', {}).get('state')!r} (QEMU exited?)")
    if not st.get("rbp_running"):
        raise Crash("rbp is not running")
    if time.time() - last_fps_seen > 45:
        raise Crash(f"no frames reported for {time.time() - last_fps_seen:.0f} s (UI frozen?)")


def record_crash(reason, action):
    stats["crashes"] += 1
    n = stats["crashes"]
    d = os.path.join(OUT, "crashes", f"{n:03d}")
    os.makedirs(d, exist_ok=True)
    try:
        shutil.copy(CONSOLE, os.path.join(d, "console.log"))
    except OSError:
        pass
    try:
        screenshot().save(os.path.join(d, "screen.png"))
    except Exception:
        pass
    with open(os.path.join(d, "reason.txt"), "w") as f:
        f.write(f"time: {datetime.datetime.now():%Y-%m-%d %H:%M:%S}\naction: {action}\nreason: {reason}\n\nlast actions:\n")
        for t, a in actions_log[-60:]:
            f.write(f"  {t} {a}\n")
    crashes.append((datetime.datetime.now().strftime("%H:%M:%S"), action, reason[:300], d))
    log(f"CRASH #{n} during {action}: {reason[:200]}")
    write_report()


# ---- unit control -----------------------------------------------------------------

state = {"usb1": None, "usb2": None, "loaded": [False, False]}


def boot(safe=False):
    global console_pos, last_fps_seen
    stats["boots"] += 1
    try:
        http("POST", "/power", "off")
    except Exception:
        pass
    time.sleep(4)
    console_pos = 0
    globals()["expect_reset"] = True
    http("POST", "/power", "on")
    state.update(usb1=None, usb2=None, loaded=[False, False])
    if safe:
        stats["safe_starts"] += 1
        # Hold MIDI (GPIO 161) from early in the boot until rbp starts, however long that takes:
        # apl_start reads it just after the USB hub setup, which in the emulator can stall for
        # minutes under host load (its I2C writes to the unmodelled hub chip time out).
        t0 = time.time()
        text = ""
        while time.time() - t0 < 60 and not text:       # QEMU up: the console has started
            text += read_console()
            time.sleep(0.5)
        http("POST", "/mainkey", "161")
        try:
            while time.time() - t0 < 600 and "Application Start" not in text:
                text += read_console()
                time.sleep(1)
            time.sleep(5)
            text += read_console()
        finally:
            http("POST", "/mainkey_release", "")
        if "safe start" not in text:
            raise Crash("safe start: no 'MIDI held' marker on the console")
        if "loaded into rbp" in text:
            raise Crash("safe start: the mods loaded anyway")
        log("safe start OK (stock rbp, no mods)")
        last_fps_seen = time.time() + 120      # stock rbp has no fps log: skip that check
        time.sleep(20)
        return
    t0 = time.time()
    text = ""
    while time.time() - t0 < 180:
        text += read_console()
        if "loaded into rbp" in text:
            break
        if CRASH_RE.search(text):
            break
        time.sleep(2)
    if "loaded into rbp" not in text:
        raise Crash(f"boot: no 'loaded into rbp' within 180 s; console tail: {text[-500:]!r}")
    last_fps_seen = time.time()
    time.sleep(25)


def pick(*names):
    """The given sticks that are present (else any present one)."""
    return [n for n in names if n in STICKS] or [n for n in STICKS if n != "big10k"] or list(STICKS)


def clone(name):
    src = os.path.join(ROOT, STICKS[name][0]) if not STICKS[name][0].startswith("/") else STICKS[name][0]
    dst = os.path.join(OUT, "usb", f"{name}.img")
    if not os.path.exists(dst):
        subprocess.run(["cp", "-c", src, dst], check=True)
    return dst


def insert(slot, name):
    for other in ("usb1", "usb2"):
        if state[other] == name and other != slot:
            state[other] = None
    http("POST", "/usb", f"{slot} {clone(name)}", timeout=120)
    state[slot] = name
    time.sleep(30 if name == "big10k" else 18)


def to_browser():
    for _ in range(3):
        s = screen()
        if s == "browser":
            return True
        if s == "utility":
            press("menu")
        else:
            press("browse")
        time.sleep(3)
    return screen() == "browser"


def to_deck():
    for _ in range(3):
        s = screen()
        if s == "deck":
            return True
        press("menu" if s == "utility" else "browse")
        time.sleep(3)
    return False


# ---- actions ----------------------------------------------------------------------

def a_insert():
    slot = rng.choice(["usb1", "usb1", "usb2"])
    name = rng.choice(list(STICKS))
    insert(slot, name)
    return f"insert {name} -> {slot}"


def a_browse_load():
    if not state["usb1"]:
        insert("usb1", rng.choice(pick("exfat_lib", "fat_cues", "flac_folder", "wav_folder")))
    if not to_browser():
        return "browse: browser didn't open"
    for _ in range(4):
        press("back")
        time.sleep(1)
    browse(-15)
    time.sleep(1)
    kind = STICKS[state["usb1"]][1]
    if kind == "library":
        browse(rng.choice([0, 0, 2, 4, 7]))      # ARTIST / TRACK / PLAYLIST / FOLDER
    time.sleep(1)
    for _ in range(rng.randint(1, 4)):
        press("browse_push")
        time.sleep(2.5)
        browse(rng.randint(0, 6))
        time.sleep(1.5)
    deck = rng.choice([1, 2])
    press(f"load_deck{deck}")
    state["loaded"][deck - 1] = True
    time.sleep(6)
    return f"browse ({state['usb1']}, {kind}) + load deck {deck}"


def a_play():
    d = rng.choice([1, 2])
    to_deck()
    press(f"deck{d}.play")
    time.sleep(rng.uniform(2, 8))
    return f"play/pause deck {d}"


def a_jog_tempo():
    d = rng.choice([1, 2])
    mcp("jog", {"deck": d, "degrees": rng.uniform(-720, 720), "seconds": rng.uniform(0.3, 1.5), "touch": rng.random() < 0.3})
    mcp("set", {"control": f"deck{d}.tempo", "value": rng.randint(0, 1023)})
    mcp("set", {"control": "crossfader", "value": rng.randint(0, 1023)})
    time.sleep(2)
    return f"jog + tempo + crossfader deck {d}"


def a_hotcues_ad():
    d = rng.choice([1, 2])
    to_deck()
    press(f"deck{d}.hot_cue")
    for _ in range(rng.randint(2, 6)):
        press(f"deck{d}.pad_{rng.choice('abcd')}")
        time.sleep(rng.uniform(0.5, 2))
    return f"hot cues A-D deck {d}"


def a_hotcues_eh():
    d = rng.choice([1, 2])
    to_deck()
    press(f"deck{d}.hot_cue")
    shift = f"B {'12' if d == 1 else '22'} 01"
    for _ in range(rng.randint(2, 6)):
        raw(shift)
        time.sleep(0.3)
        press(f"deck{d}.pad_{rng.choice('abcd')}")
        time.sleep(0.3)
        release(shift)
        time.sleep(rng.uniform(0.5, 2))
    return f"SHIFT + pads (E-H) deck {d}"


def a_beatjump():
    d = rng.choice([1, 2])
    to_deck()
    shift = f"B {'12' if d == 1 else '22'} 01"
    raw(shift)
    time.sleep(0.3)
    press(f"deck{d}.beat_loop")
    time.sleep(0.3)
    release(shift)
    for _ in range(rng.randint(3, 10)):
        press(f"deck{d}.pad_{rng.choice('abcd')}")
        time.sleep(rng.uniform(0.3, 1.5))
    press(f"deck{d}.hot_cue")          # back to rbp's pads
    return f"beat jump deck {d}"


ROWS = {"FRAME RATE": (-7, 3), "WAVEFORM COLOR": (-6, 3), "WAVEFORM SCROLL": (-5, 2),
        "BEAT JUMP A": (-3, 6), "BEAT JUMP B": (-2, 6)}


def a_setting():
    row = rng.choice(list(ROWS))
    up, n = ROWS[row]
    to_deck()
    press("menu", 1500)
    time.sleep(3)
    if screen() != "utility":
        return "setting: UTILITY didn't open"
    browse(30)
    time.sleep(1.5)
    browse(up)
    time.sleep(1.5)
    press("browse_push")
    time.sleep(1.5)
    browse(rng.choice([-1, 1, 2]))
    time.sleep(1.5)
    press("browse_push")
    time.sleep(1.5)
    press("menu")
    time.sleep(2)
    return f"UTILITY: change {row}"


def a_pull_while_playing():
    if not state["usb1"]:
        return a_insert()
    to_deck()
    for d in (1, 2):
        if state["loaded"][d - 1]:
            press(f"deck{d}.play")
    time.sleep(3)
    name = rng.choice(list(STICKS))
    insert("usb1", name)                 # replaces the stick under a loaded/playing deck
    return f"swap usb1 -> {name} while decks loaded"


def a_folder_contents():
    if state["usb1"] not in ("exfat_lib", "fat_cues", "big10k"):
        insert("usb1", pick("exfat_lib", "fat_cues")[0])
    if not to_browser():
        return "folder: browser didn't open"
    for _ in range(4):
        press("back")
        time.sleep(1)
    browse(-15)
    browse(7)
    time.sleep(1)
    for _ in range(rng.randint(1, 4)):
        press("browse_push")
        time.sleep(2.5)
        browse(rng.randint(0, 3))
    if rng.random() < 0.5:
        press(f"load_deck{rng.choice([1, 2])}")
        time.sleep(5)
    return "[FOLDER] / Contents browse"


def a_big_scroll():
    insert("usb1", "big10k")
    if not to_browser():
        return "big: browser didn't open"
    for _ in range(4):
        press("back")
        time.sleep(1)
    browse(-15)
    browse(rng.choice([2, 4]))
    press("browse_push")
    time.sleep(5)
    if STICKS["big10k"][1] == "library" and rng.random() < 0.5:
        press("browse_push")
        time.sleep(5)
    browse(rng.choice([500, 1500, -800]))
    time.sleep(8)
    return "10k library: open + scroll"


def a_reboot():
    safe = rng.random() < 0.25
    boot(safe=safe)
    if safe:
        boot(safe=False)                 # back to the mods
        return "safe start + normal boot"
    return "power cycle"


ACTIONS = [  # (weight, function)
    (10, a_browse_load), (8, a_play), (8, a_jog_tempo), (8, a_hotcues_ad), (8, a_hotcues_eh),
    (6, a_beatjump), (5, a_setting), (4, a_insert), (3, a_pull_while_playing),
    (3, a_folder_contents), (1, a_big_scroll), (1, a_reboot),
]
if "big10k" not in STICKS:
    ACTIONS = [(w, f) for w, f in ACTIONS if f is not a_big_scroll]


def write_report():
    el = datetime.datetime.now() - START
    with open(os.path.join(OUT, "report.md"), "w") as f:
        f.write(f"# Stress test: custom firmware {RELEASE}\n\n")
        f.write(f"- started {START:%Y-%m-%d %H:%M}, until {DEADLINE:%H:%M}, elapsed {str(el).split('.')[0]}, seed {A.seed}\n")
        f.write(f"- actions {stats['actions']}, boots {stats['boots']} (safe starts {stats['safe_starts']}), "
                f"crashes **{stats['crashes']}**, ALSA xruns seen {xrun_count}\n\n")
        f.write("| action | runs |\n|---|---|\n")
        for k, v in sorted(stats["by_action"].items(), key=lambda kv: -kv[1]):
            f.write(f"| {k} | {v} |\n")
        f.write("\n## Crashes\n\n")
        if not crashes:
            f.write("None.\n")
        for t, a, r, d in crashes:
            f.write(f"- **{t}** during *{a}*: {r} — `{os.path.relpath(d, ROOT)}`\n")


START = datetime.datetime.now()
RELEASE = status().get("installed_release", "?")
log(f"stress test of {RELEASE} until {DEADLINE:%H:%M} (seed {A.seed})")
if A.safe_starts:
    for i in range(A.safe_starts):
        try:
            boot(safe=True)
        except Crash as c:
            record_crash(str(c), "safe start")
    try:
        boot()
    except Crash as c:
        record_crash(str(c), "normal boot")
    write_report()
    log(f"done: {A.safe_starts} safe starts, {stats['crashes']} failures")
    sys.exit(1 if stats["crashes"] else 0)
weights = [w for w, _ in ACTIONS]
need_boot = True
last_report = 0
while datetime.datetime.now() < DEADLINE:
    action = "boot"
    try:
        if need_boot:
            boot()
            need_boot = False
            insert("usb1", rng.choice(pick("exfat_lib", "fat_cues")))
            check("after boot")
        fn = rng.choices([f for _, f in ACTIONS], weights)[0]
        action = fn.__name__
        desc = fn()
        stats["actions"] += 1
        stats["by_action"][action] = stats["by_action"].get(action, 0) + 1
        actions_log.append((datetime.datetime.now().strftime("%H:%M:%S"), desc))
        actions_log[:] = actions_log[-200:]
        check(desc)
    except Crash as c:
        record_crash(str(c), action)
        need_boot = True
    except Exception as e:                       # harness / host trouble: note it, recover
        log(f"harness error during {action}: {type(e).__name__}: {e}")
        actions_log.append((datetime.datetime.now().strftime("%H:%M:%S"), f"harness error: {e}"))
        try:
            check("after harness error")
        except Crash as c:
            record_crash(str(c), action)
            need_boot = True
        time.sleep(5)
    if time.time() - last_report > 60:
        write_report()
        last_report = time.time()
write_report()
log(f"done: {stats['actions']} actions, {stats['crashes']} crashes")
