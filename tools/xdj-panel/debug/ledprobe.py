#!/usr/bin/env python3
"""Map panel LEDs: press controls one at a time and report which SubTxData bits change.

Needs QEMU patch 15 in FILE mode (no XDJ_RUNTIME_DIR), so rbp's LED payload lands in
/tmp/subucom_tx (or $XDJ_TX_PATH). For each spec the probe:
  1. samples the payload for 3 s (baseline),
  2. fires the control once (writes the spec, then rewrites the holds alone 0.5 s later;
     a one-shot left in the ctrl file next to holds re-fires every 30 frames),
  3. lets it settle for 2 s, samples again for 3 s, and
  4. prints each bit whose class changed: 0 / 1 / blink (>= 2 transitions).
The meter bytes and jog-ring bytes are skipped (they move constantly during play).
Layout and results: docs/subucom-led-map.md.

  ledprobe.py b_12_20 b_12_20          # SYNC on, SYNC off   (underscores = spaces)
  ledprobe.py key160                   # MAIN-UCOM GPIO key (USB1)
  HOLD=$'A 2c 3ff\\nA 4a 3ff' ledprobe.py b_5b_04   # extra holds kept in every write
"""
import os, sys, time

TX = os.environ.get("XDJ_TX_PATH") or "/tmp/subucom_tx"
CTRL = "/tmp/subucom_ctrl"
MAINKEY = "/tmp/mainkey"
HOLD = os.environ.get("HOLD", "A 2c 3ff\nA 4a 3ff\nA 4e 200\nA 24 200\nA 2a 200\nA 28 200\nA 26 3ff")
SKIP = {0x00, 0x04, 0x08, 0x09, 0x0c, 0x0d, 0x0e, 0x0f}   # jog rings + meters


def read_tx():
    try:
        with open(TX) as f:
            ln = f.read().strip()
        return bytes.fromhex(ln[2:]) if ln.startswith("T ") else None
    except Exception:
        return None


def sample(secs):
    out, last, end = [], None, time.time() + secs
    while time.time() < end:
        fr = read_tx()
        if fr and fr != last:
            out.append(fr)
            last = fr
        time.sleep(0.02)
    return out or ([read_tx()] if read_tx() else [])


def classify(frames):
    c = {}
    for byte in range(20):
        if byte in SKIP:
            continue
        for bit in range(8):
            seq = [(f[byte] >> bit) & 1 for f in frames]
            tr = sum(1 for i in range(1, len(seq)) if seq[i] != seq[i - 1])
            c[(byte, bit)] = "blink" if tr >= 2 else (str(seq[-1]) if seq else "?")
    return c


def setctl(extra=""):
    body = HOLD + "\n" + extra.replace("_", " ").replace(",", "\n") + "\n"
    with open(CTRL + ".t", "w") as f:
        f.write(body)
    os.replace(CTRL + ".t", CTRL)


def main():
    if not read_tx():
        sys.exit("no LED payload at %s (QEMU patch 15, file mode?)" % TX)
    setctl()
    for spec in sys.argv[1:]:
        base = classify(sample(3))
        if spec.startswith("key"):
            with open(MAINKEY, "w") as f:
                f.write(spec[3:])
            time.sleep(0.8)
            os.remove(MAINKEY)
        else:
            setctl(spec)
            time.sleep(0.5)
            setctl()
        sample(2)
        after = classify(sample(3))
        ch = ["%02x.%d:%s->%s" % (b, i, base[(b, i)], after[(b, i)])
              for (b, i) in base if base[(b, i)] != after[(b, i)]]
        print("%-14s %s" % (spec, " ".join(ch) or "-"), flush=True)


if __name__ == "__main__":
    main()
