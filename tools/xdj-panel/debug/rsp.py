#!/usr/bin/env python3
"""Minimal GDB RSP client for the QEMU gdbstub on tcp::1234 (ARM LE).

Usage as a library (import rsp) or CLI:
  rsp.py mem <addr_hex> <len>              # hexdump memory
  rsp.py words <addr_hex> <count>          # dump as 32-bit LE words
  rsp.py wr32 <addr_hex> <val_hex>         # write a 32-bit word
  rsp.py regs                              # dump core regs
  rsp.py status                            # ? query
"""
import socket, sys, time

HOST, PORT = "127.0.0.1", 1234


class RSP:
    def __init__(self, host=HOST, port=PORT, timeout=5.0):
        self.s = socket.create_connection((host, port), timeout=timeout)
        self.s.settimeout(timeout)
        self.buf = b""
        self.noack = False

    def close(self):
        try:
            self.s.close()
        except Exception:
            pass

    def _recv_byte(self):
        if not self.buf:
            self.buf = self.s.recv(65536)
            if not self.buf:
                raise EOFError("stub closed")
        b, self.buf = self.buf[:1], self.buf[1:]
        return b

    def send(self, data):
        if isinstance(data, str):
            data = data.encode()
        csum = sum(data) & 0xff
        pkt = b"$" + data + b"#" + ("%02x" % csum).encode()
        self.s.sendall(pkt)
        if not self.noack:
            # wait for '+'
            while True:
                b = self._recv_byte()
                if b == b"+":
                    break
                if b == b"-":
                    self.s.sendall(pkt)  # resend

    def recv(self, timeout=None):
        """Read one packet $...#cc, return the inner data string."""
        if timeout is not None:
            self.s.settimeout(timeout)
        try:
            while True:
                b = self._recv_byte()
                if b == b"$":
                    break
            data = b""
            while True:
                b = self._recv_byte()
                if b == b"#":
                    break
                data += b
            self._recv_byte(); self._recv_byte()  # checksum digits
            if not self.noack:
                self.s.sendall(b"+")
            return data.decode("latin1")
        finally:
            if timeout is not None:
                self.s.settimeout(5.0)

    def cmd(self, c, timeout=None):
        self.send(c)
        return self.recv(timeout=timeout)

    def interrupt(self):
        self.s.sendall(b"\x03")

    # ---- memory ----
    def read_mem(self, addr, length):
        out = b""
        while length > 0:
            chunk = min(length, 512)
            r = self.cmd("m%x,%x" % (addr, chunk))
            if r.startswith("E"):
                raise RuntimeError("read_mem E%s @ %#x" % (r, addr))
            out += bytes.fromhex(r)
            addr += chunk
            length -= chunk
        return out

    def write_mem(self, addr, data):
        r = self.cmd("M%x,%x:%s" % (addr, len(data), data.hex()))
        if r != "OK":
            raise RuntimeError("write_mem -> %s" % r)

    def rd32(self, addr):
        return int.from_bytes(self.read_mem(addr, 4), "little")

    def wr32(self, addr, val):
        self.write_mem(addr, int(val).to_bytes(4, "little"))

    # ---- watchpoints ----
    def set_watch(self, addr, length=4, kind=2):  # Z2 = write
        r = self.cmd("Z%d,%x,%x" % (kind, addr, length))
        return r
    def clear_watch(self, addr, length=4, kind=2):
        return self.cmd("z%d,%x,%x" % (kind, addr, length))

    # ---- registers (ARM core: 0-15 = r0..r15/pc, 25 = cpsr) ----
    def read_reg(self, n):
        r = self.cmd("p%x" % n)
        if r.startswith("E") or not r:
            raise RuntimeError("read_reg %d -> %r" % (n, r))
        return int.from_bytes(bytes.fromhex(r), "little")
    def regs_lo(self):
        return {("r%d" % i): self.read_reg(i) for i in range(4)}

    def set_bp(self, addr, kind=0, length=4):  # Z0 = sw bp
        return self.cmd("Z%d,%x,%x" % (kind, addr, length))
    def clear_bp(self, addr, kind=0, length=4):
        return self.cmd("z%d,%x,%x" % (kind, addr, length))

    def cont_wait(self, timeout=20.0):
        """Continue; return the stop-reply string (or None on timeout)."""
        self.send("c")
        try:
            return self.recv(timeout=timeout)
        except socket.timeout:
            return None


def _fmt_words(data, base):
    lines = []
    for i in range(0, len(data) - 3, 4):
        w = int.from_bytes(data[i:i+4], "little")
        lines.append("  %#010x: %#010x" % (base + i, w))
    return "\n".join(lines)


if __name__ == "__main__":
    r = RSP()
    a = sys.argv[1] if len(sys.argv) > 1 else "status"
    if a == "mem":
        addr = int(sys.argv[2], 16); n = int(sys.argv[3])
        print(r.read_mem(addr, n).hex())
    elif a == "words":
        addr = int(sys.argv[2], 16); n = int(sys.argv[3])
        print(_fmt_words(r.read_mem(addr, n * 4), addr))
    elif a == "wr32":
        r.wr32(int(sys.argv[2], 16), int(sys.argv[3], 16)); print("OK")
    elif a == "status":
        print(repr(r.cmd("?")))
    elif a == "regs":
        print(repr(r.cmd("g")))
    r.close()
