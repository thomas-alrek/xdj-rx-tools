#!/usr/bin/env python3
"""Send one HMP command to the QEMU monitor socket and print the reply."""
import socket, time, sys
SOCK="/tmp/d_mon4.sock"
def mon(cmd):
    s=socket.socket(socket.AF_UNIX); s.settimeout(3); s.connect(SOCK)
    time.sleep(0.2)
    s.setblocking(False)
    try:
        while s.recv(65536): pass
    except: pass
    s.setblocking(True); s.settimeout(3)
    s.sendall((cmd+"\n").encode())
    time.sleep(0.5)
    out=b""; s.setblocking(False); t0=time.time()
    while time.time()-t0<2:
        try:
            d=s.recv(65536)
            if d: out+=d; t0=time.time()
        except BlockingIOError: time.sleep(0.05)
        except: break
    s.close(); return out.decode("latin1")
if __name__=="__main__":
    print(mon(" ".join(sys.argv[1:])))
