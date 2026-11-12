#!/usr/bin/env python3
"""Find the sub-ucom frame field for InputSourceSW by injecting candidates and
watching ch0.routed_src flip 3->0 (LINE->DECK). RSP reads are spaced + retried
(never hammered) to avoid tripping rbp's real-time watchdog."""
import socket, time, sys
import rsp

CTRL = "/tmp/subucom_ctrl"
CH0_ROUTED = 0x107acdc + 4   # ch[0].routed_src

def read_routed(retries=5):
    for _ in range(retries):
        try:
            r = rsp.RSP()
            try:
                v = int.from_bytes(r.read_mem(CH0_ROUTED, 4), "little")
                return v
            finally:
                try: r.cmd("D")
                except: pass
                r.close()
        except Exception:
            time.sleep(4)
    return None

def inject(line):
    with open(CTRL, "w") as f:
        f.write(line + "\n")

def clear():
    try:
        import os; os.remove(CTRL)
    except FileNotFoundError:
        pass

def main():
    clear(); time.sleep(2)
    base = read_routed()
    print("baseline ch0.routed =", base, flush=True)
    if base is None:
        print("cannot read baseline; abort"); return
    # candidate frame offsets (unmapped gaps + edges), tried as button-mask holds
    offs = [0x40,0x41,0x42,0x43,0x51,0x52,0x53,0x60,0x61,0x62,0x00,0x01,0x02,0x03,0x55,0x58,0x59]
    for off in offs:
        for spec in ("B %02x ff" % off, "A %02x 3ff" % off):
            inject(spec); time.sleep(1.5)
            v = read_routed()
            flag = "  <<< FLIP!" if (v is not None and v != base) else ""
            print("inject '%s' -> ch0.routed=%s%s" % (spec, v, flag), flush=True)
            clear(); time.sleep(1.0)
            if v is not None and v != base:
                print("CANDIDATE FOUND at offset %#x via '%s'" % (off, spec)); return
    print("no flip found in candidate set")

if __name__ == "__main__":
    main()
