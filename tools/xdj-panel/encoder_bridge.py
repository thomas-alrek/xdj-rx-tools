#!/usr/bin/env python3
"""
Browse-encoder ROTATE bridge for the XDJ-RX emulator.  *** EXPERIMENTAL / NOT CLEAN ***

KNOWN LIMITATION (2026-11-10): this force-calls FUN_000e159c out of the browser page's
own update() context, so the selection index moves but the page does NOT re-render
normally -- it sticks showing rekordbox's ▲▼ rotation overlay (NOT seen on real hardware)
and the selection can wedge. The real fix is making the browser page interactive/ticking
so its own update() calls FUN_000e159c in context; see docs/xdj-rx-control-map.md
"Session 4d". Keep this only as a proof that the scroll mechanism works.


Background (see docs/xdj-rx-control-map.md "Session 4c/4d"): the browse encoder's
scroll is applied by rbp's `FUN_000e159c` (@0xe159c) -- it reads the accumulated
rotation `DAT_017f3234` (@0x17f3234) and moves the category/list cursor by sign.
The SCROLL WORKS, but on the active category page rbp's browser update tick that
would call `FUN_000e159c` is dormant (the long-standing "browser page not ticking"
gating). So a subucom frame injection accumulates the rotation but it is never
applied.

This bridge closes that gap WITHOUT patching the rbp binary (same spirit as the
QEMU subucom harness): over the gdb stub it sets `DAT_017f3234 += steps` and
force-calls `FUN_000e159c` in rbp userspace context, so the list scrolls. One
"detent" per step; sign = direction.

Usage:  python3 tools/xdj-panel/encoder_bridge.py            # daemon: polls /tmp/subucom_enc
        python3 tools/xdj-panel/encoder_bridge.py 3          # one-shot: scroll +3
        python3 tools/xdj-panel/encoder_bridge.py -2         # one-shot: scroll -2
The panel server writes a signed integer to /tmp/subucom_enc; the daemon applies it.
Requires the emulator launched with -gdb tcp::1234 and the browser page on screen.
"""
import socket, time, struct, os, sys

GDB_HOST, GDB_PORT = "127.0.0.1", 1234
ENC_FILE   = "/tmp/subucom_enc"
CTRL_FILE  = "/tmp/subucom_ctrl"
ADDR_ACCUM = 0x17f3234      # DAT_017f3234  (accumulated signed rotation)
ADDR_APPLY = 0xe159c        # FUN_000e159c  (EncoderRotate applier -> scrolls)
ADDR_CTX   = 0x283600       # subucom decoder: runs every frame, no key press
RBP_LO, RBP_HI = 0xeda0, 0x35af40   # rbp .text range (userspace context check)


class Gdb:
    def __init__(s):
        s.s = socket.socket(); s.s.settimeout(2.0)
        s.s.connect((GDB_HOST, GDB_PORT)); s.buf = b""
    @staticmethod
    def _ck(d): return sum(d.encode()) & 0xff
    def send(s, d): s.s.sendall(("$%s#%02x" % (d, s._ck(d))).encode())
    def pkt(s, to=3.0):
        end = time.time() + to
        while time.time() < end:
            i = s.buf.find(b'$')
            if i >= 0:
                h = s.buf.find(b'#', i)
                if h >= 0 and len(s.buf) >= h + 3:
                    d = s.buf[i+1:h]; s.buf = s.buf[h+3:]; s.s.sendall(b'+'); return d.decode('latin1')
            try:
                c = s.s.recv(4096); s.buf += c if c else b""
            except socket.timeout:
                return None
        return None
    def cmd(s, d, to=3.0): s.send(d); return s.pkt(to)
    def intr(s): s.s.sendall(b"\x03"); return s.pkt(1.5)
    def regs(s):
        r = s.cmd("g")
        return [struct.unpack("<I", bytes.fromhex(r[i*8:i*8+8]))[0] for i in range(16)] if r and len(r) >= 128 else None
    def wr32(s, a, v): return s.cmd("M%x,4:%s" % (a, "".join("%02x" % ((v >> (8*i)) & 0xff) for i in range(4))))
    def setreg(s, n, v): return s.cmd("P%x=%s" % (n, "".join("%02x" % ((v >> (8*i)) & 0xff) for i in range(4))))
    def setbp(s, a): return s.cmd("Z1,%x,4" % a)
    def clrbp(s, a): return s.cmd("z1,%x,4" % a)
    def cont(s, to=3.0): s.send("c"); return s.pkt(to)


def scroll(g, steps):
    """Accumulate `steps` detents into DAT_017f3234 and force-call FUN_000e159c."""
    if steps == 0:
        return False
    # 1) land in rbp userspace context WITHOUT injecting any key (so no encoder-press
    #    overlay): use the current thread if it is already in rbp .text, else trap the
    #    subucom TX frame builder, which runs every frame with no input.
    g.intr()
    orig = g.regs()
    if not (orig and RBP_LO <= orig[15] < RBP_HI):
        g.setbp(ADDR_CTX)
        r = g.cont(4.0)
        if not (r and r.startswith("T")):
            g.clrbp(ADDR_CTX); g.send("c"); return False
        orig = g.regs(); g.clrbp(ADDR_CTX)
    if not orig:
        g.send("c"); return False
    # 2) set accumulator and force-call the applier, returning to a trap we catch.
    g.wr32(ADDR_ACCUM, steps & 0xffffffff)
    g.setbp(ADDR_CTX)
    g.setreg(14, ADDR_CTX)      # lr = trap
    g.setreg(15, ADDR_APPLY)    # pc = FUN_000e159c
    g.cont(3.0)
    g.clrbp(ADDR_CTX)
    # 3) restore the hijacked thread exactly and resume.
    for n in range(16):
        g.setreg(n, orig[n])
    g.send("c")
    return True


def main():
    if len(sys.argv) > 1:
        g = Gdb()
        ok = scroll(g, int(sys.argv[1]))
        print("scrolled" if ok else "no-op (browser page on screen?)")
        return
    print("encoder bridge: polling %s (write a signed int). Ctrl-C to stop." % ENC_FILE)
    g = Gdb()
    while True:
        try:
            if os.path.exists(ENC_FILE):
                with open(ENC_FILE) as f:
                    txt = f.read().strip()
                os.remove(ENC_FILE)
                if txt.lstrip("-").isdigit() and int(txt) != 0:
                    scroll(g, int(txt))
            time.sleep(0.03)
        except KeyboardInterrupt:
            print("\nstopped"); break
        except Exception as e:
            print("err:", e); time.sleep(0.5)
            try:
                g = Gdb()
            except Exception:
                pass


if __name__ == "__main__":
    main()
