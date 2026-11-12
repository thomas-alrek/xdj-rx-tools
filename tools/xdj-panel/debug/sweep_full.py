#!/usr/bin/env python3
"""Comprehensive sweep: inject each frame byte (0x00-0x63) as a button-mask hold
and watch ch0.routed_src flip from baseline (3=LINE). Spaced/retried RSP reads
(never hammered) to avoid the rbp watchdog. Prints any offset that flips routing."""
import time, os
import rsp
CTRL = "/tmp/subucom_ctrl"
CH0 = 0x107acdc + 4

def read_routed(retries=4):
    for _ in range(retries):
        try:
            r = rsp.RSP()
            try:
                return int.from_bytes(r.read_mem(CH0, 4), "little")
            finally:
                try: r.cmd("D")
                except: pass
                r.close()
        except Exception:
            time.sleep(3)
    return None

def clear():
    try: os.remove(CTRL)
    except FileNotFoundError: pass

def main():
    clear(); time.sleep(2)
    base = read_routed()
    print("baseline ch0.routed =", base, flush=True)
    if base is None:
        print("no baseline; abort"); return
    for off in range(0x00, 0x64):
        if off in (0x5d,):   # 0x5d = USB STOP bits; skip to avoid eject noise
            continue
        with open(CTRL, "w") as f:
            f.write("B %02x ff\n" % off)
        time.sleep(1.3)
        v = read_routed()
        if v is not None and v != base:
            print(">>> FLIP at off %#04x: ch0.routed %s -> %s" % (off, base, v), flush=True)
        else:
            print("off %#04x -> %s" % (off, v), flush=True)
        clear(); time.sleep(0.8)
    print("sweep done")

if __name__ == "__main__":
    main()
