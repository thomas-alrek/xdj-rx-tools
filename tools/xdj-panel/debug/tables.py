#!/usr/bin/env python3
"""Read the rbp audio source/channel routing tables over RSP, then detach (resume)."""
import rsp

SRC_BASE = 0x107ac88   # 7 source slots {buf, idx, flag}, stride 12
CH_BASE  = 0x107acdc   # 6 channel slots {buf, routed_src_idx, flag}, stride 12

def main():
    r = rsp.RSP()
    try:
        print("status:", repr(r.cmd("?")))
        data = r.read_mem(SRC_BASE, 0x9c)
        def w(off): return int.from_bytes(data[off:off+4], "little")
        print("\n== SOURCE table @%#x ==" % SRC_BASE)
        for i in range(7):
            o = i*12
            print("  src[%d] @%#010x: buf=%#010x idx=%d flag=%#x" %
                  (i, SRC_BASE+o, w(o), w(o+4), w(o+8)))
        print("\n== CHANNEL table @%#x ==" % CH_BASE)
        for j in range(6):
            o = (CH_BASE-SRC_BASE) + j*12
            print("  ch[%d]  @%#010x: buf=%#010x routed_src=%d flag=%#x" %
                  (j, CH_BASE+j*12, w(o), w(o+4), w(o+8)))
    finally:
        print("\ndetach:", repr(r.cmd("D")))
        r.close()

if __name__ == "__main__":
    main()
