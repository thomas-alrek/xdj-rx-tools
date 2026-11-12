#!/usr/bin/env python3
"""Talk to the guest console on /tmp/d_con4.sock: log in as root, run a command,
print output. Usage: ser.py '<shell command>'   (empty = just drain)."""
import socket, time, sys

SOCK = "/tmp/d_con4.sock"

def slow(s, data, d=0.012):
    if isinstance(data,str): data=data.encode()
    for b in data:
        s.sendall(bytes([b])); time.sleep(d)

def drain(s, t=0.6):
    s.setblocking(False); out=b""; t0=time.time()
    while time.time()-t0 < t:
        try:
            d=s.recv(65536)
            if d: out+=d; t0=time.time()
        except BlockingIOError:
            time.sleep(0.05)
        except Exception:
            break
    s.setblocking(True); return out

def main():
    cmd = sys.argv[1] if len(sys.argv)>1 else ""
    s=socket.socket(socket.AF_UNIX); s.settimeout(5); s.connect(SOCK)
    s.sendall(b"\n"); time.sleep(0.4)
    pre = drain(s, 0.8).decode("latin1")
    # log in if at login prompt
    if "login:" in pre[-200:]:
        slow(s, "root\n"); time.sleep(0.8)
        pre += drain(s, 1.0).decode("latin1")
        if "assword" in pre[-120:]:
            s.sendall(b"\n"); time.sleep(0.6); pre += drain(s,0.8).decode("latin1")
    if cmd:
        marker = "RRR_%d" % int(time.time()%100000)
        slow(s, "echo %s_S; %s; echo %s_E\n" % (marker, cmd, marker))
        time.sleep(0.4)
        out=b""; t0=time.time()
        while time.time()-t0 < 30:
            chunk = drain(s, 0.7)
            out += chunk
            if (marker+"_E") in out.decode("latin1","replace"): break
        txt=out.decode("latin1","replace")
        marker=marker  # fallthrough
        import re as _re
        txt=_re.sub(r'\x1b\[[0-9;?]*[A-Za-z]','',txt).replace('\x08','').replace('\r','')
        a=txt.find(marker+"_S"); b=txt.rfind(marker+"_E")
        # skip the echoed command line after _S
        if a>=0:
            nl=txt.find("\n", a); a = nl+1 if nl>=0 else a+len(marker)+2
        if a>=0 and b>=0 and b>a:
            print(txt[a:b].strip()); s.close(); return
        print("[raw] "+txt[-1000:]); s.close(); return
        a=txt.find(marker+"_START"); b=txt.find(marker+"_END")
        if a>=0 and b>=0:
            print(txt[a+len(marker)+6:b].strip())
        else:
            print("[no marker] "+txt[-800:])
    else:
        print(pre[-800:])
    s.close()

if __name__=="__main__":
    main()
