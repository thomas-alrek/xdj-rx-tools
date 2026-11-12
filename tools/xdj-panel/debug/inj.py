#!/usr/bin/env python3
"""Gains-preserving inject helper. Always keeps the analog-gain holds in the ctrl
file so one-shots don't silence the mix.
  inj.py base                 # (re)assert gain holds only
  inj.py pulse 'b 5d 10' [s]  # fire a one-shot on top of the holds, then revert
  inj.py mainkey 158 [s]      # momentary MAIN-UCOM GPIO
"""
import sys, time, os
BASE = ("A 2c 3ff\nA 4a 3ff\nA 4e 200\nA 24 200\nA 2a 200\nA 28 200\nA 26 3ff\n")
CTRL = "/tmp/subucom_ctrl"; MK = "/tmp/mainkey"
def write(s):
    with open(CTRL+".tmp","w") as f: f.write(s)
    os.replace(CTRL+".tmp", CTRL)
c = sys.argv[1]
if c == "base":
    write(BASE)
elif c == "pulse":
    spec = sys.argv[2]; dwell = float(sys.argv[3]) if len(sys.argv)>3 else 0.5
    write(BASE + spec + "\n"); time.sleep(dwell); write(BASE)
elif c == "mainkey":
    g = sys.argv[2]; dwell = float(sys.argv[3]) if len(sys.argv)>3 else 0.4
    with open(MK,"w") as f: f.write(g+"\n")
    time.sleep(dwell)
    try: os.remove(MK)
    except FileNotFoundError: pass
print("ok")
