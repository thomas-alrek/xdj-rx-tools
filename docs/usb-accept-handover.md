# Handover: rbp USB library-accept (the "USB Error" flaky race)

**For:** a fresh session continuing ONLY the USB library-accept problem. Everything
else in the emulator works; this is the last gate before a loaded track.

## 0. The one-paragraph summary
The XDJ-RX emulator boots unmodified firmware (`rbp`) to a live UI, the MAIN-UCOM
source buttons (USB1/USB2/rekordbox/MIDI) open the browser, and a genuine rekordbox
USB (`test/usb/rekordbox_new.img`) mounts correctly at slot `2-1.1` → `/media/usb2/sda1`.
**But most fresh boots fail to register the library:** rbp shows `USB Error. Remove the
device.` / the USB2 browse screen reads `NO USB`, and never opens `export.pdb`. One lucky
boot earlier DID load it (same image/topology), so it is a **load-sensitive race**, not a
deterministic bug. The rejection is **100% rbp-side and pre-scan**. Your job: find why rbp
doesn't consume/accept the slot event, and make accept reliable. **HARD CONSTRAINT: do NOT
patch the rbp binary** — vanilla rbp must run so future custom firmware can be tested.
Kernel/rootfs changes are last resort; QEMU-side fixes preferred.

## 1. How to run the emulator
Build (only if you change a QEMU patch): `qemu/build-qemu.sh` then `qemu/build-nand.sh`.
The binary is `build/qemu-src/qemu-11.1.0/build/qemu-system-arm`. Exact launch command
(cold-plugs the USB on the hub at 2-1.1; detect line idles high so rbp sees "no USB" until
you drive it low):

```bash
build/qemu-src/qemu-11.1.0/build/qemu-system-arm -M sabrelite -smp 1 -m 256 -no-reboot \
  -display none \
  -drive if=none,id=usbstick,file=test/usb/rekordbox_new.img,format=raw,snapshot=on \
  -device usb-hub,bus=usb-bus.1,port=1,id=xdjhub \
  -device usb-storage,bus=usb-bus.1,port=1.1,drive=usbstick,id=xdjusb,removable=on \
  -device loader,file=build/qemu/vmlinux.bin,addr=0x10008000,force-raw=on \
  -device loader,file=build/qemu/atags.bin,addr=0x10000100,force-raw=on \
  -device loader,file=build/qemu/tramp.bin,addr=0x10000000,cpu-num=0 \
  -drive if=none,id=xdjsd,file=build/qemu/nand.img,format=raw,snapshot=on \
  -nic user \
  -monitor unix:/tmp/d_mon4.sock,server,nowait \
  -serial unix:/tmp/d_con4.sock,server,nowait -serial null -gdb tcp::1234 &
disown   # it is orphaned but keeps running; kill with: pkill -f "qemu-system-arm -M sabrelite"
```

Endpoints: monitor `/tmp/d_mon4.sock`, guest serial `/tmp/d_con4.sock`, gdb `tcp::1234`.
Boot takes ~1–3 min (load-sensitive). rbp pid appears via `pidof rbp` on the console.
Always launch as the user, never sudo.

### Presenting the USB
- `touch /tmp/usb2_insert` drives the USB2 detect GPIO (GPIO7_IO12) LOW = "device present"
  (patch 06). `rm` releases it. (`/tmp/usb_insert` = USB1 / GPIO4_IO30.)
- A detect-line **re-toggle after boot does NOT reliably re-post the slot event**; the
  decisive observation window is the FIRST insert on a fresh boot. Timing matters.
- Source-key presses: `echo <gpio> > /tmp/mainkey` (158=USB2, 160=USB1, 159=rekordbox,
  161=MIDI), `rm` to release (patch 09). Pressing USB2 opens the `NO USB(USB2)` screen.

### Screenshot / console / gdb helpers (embedded — recreate in your scratchpad)
`con4.py` — run one command on the guest serial console:
```python
import socket,time,sys
s=socket.socket(socket.AF_UNIX); s.connect("/tmp/d_con4.sock"); s.settimeout(0.5)
buf=b""
def pump(t=0.8):
    global buf; end=time.time()+t
    while time.time()<end:
        try:
            d=s.recv(4096)
            if d: buf+=d
        except socket.timeout: pass
        except: break
def send(x):
    for ch in x.encode(): s.sendall(bytes([ch])); time.sleep(0.004)
def wait(pats,to=20):
    global buf; end=time.time()+to
    while time.time()<end:
        pump(0.25)
        for p in pats:
            if p in buf: return p
    return None
send("\r"); pump(0.8)
w=wait([b"login:",b"# ",b"~#",b"~$"],6)
if w==b"login:":
    buf=b""; send("root\r")
    if wait([b"Password:",b"# ",b"~$"],6)==b"Password:": send("\r")
    wait([b"# ",b"~#",b"~$"],6)
buf=b""
cmd=sys.argv[1] if len(sys.argv)>1 else "echo HI"
send(cmd+" ; printf 'EN''DX_%d\\n' $?\r")
wait([b"ENDX_"],40); pump(0.6)
sys.stdout.write(buf.decode(errors="replace")); s.close()
```
Usage: `python3 con4.py "dmesg | tail"`. NOTE: busybox userland — `od` only supports
`-aBbcDdeFfHhIiLlOovXx` (no `-t`/`-A`); use `od -x` or `hexdump`. `cat /proc/udev_usbctn*`
BLOCKS (event channel) — wrap in `timeout`.

Screenshot: `printf 'screendump /tmp/x.ppm\n' | nc -U -w2 /tmp/d_mon4.sock; sips -s format png /tmp/x.ppm --out /tmp/x.png`

gdb RSP helper (the `G` class — minimal, reused all session). bp with `Z1,<va>,4`, continue
`c`, read regs `g`, read mem `m<addr>,<len>`, write mem `M<addr>,<len>:<hex>`, interrupt `\x03`:
```python
import socket,time,struct
def cks(d): return sum(d.encode())&0xff
class G:
    def __init__(s): s.s=socket.socket(); s.s.settimeout(1.0); s.s.connect(("127.0.0.1",1234)); s.buf=b""
    def send(s,d): s.s.sendall(("$%s#%02x"%(d,cks(d))).encode())
    def pkt(s,to=3.0):
        end=time.time()+to
        while time.time()<end:
            i=s.buf.find(b'$')
            if i>=0:
                h=s.buf.find(b'#',i)
                if h>=0 and len(s.buf)>=h+3:
                    d=s.buf[i+1:h]; s.buf=s.buf[h+3:]; s.s.sendall(b'+'); return d.decode(errors='replace')
            try: c=s.s.recv(4096); s.buf+=c if c else b""
            except socket.timeout: pass
            except: break
        return None
    def cmd(s,d,to=3.0): s.send(d); return s.pkt(to)
    def intr(s): s.s.sendall(b"\x03"); return s.pkt(1.5)
    def regs(s):
        r=s.cmd("g"); 
        return [struct.unpack("<I",bytes.fromhex(r[i*8:i*8+8]))[0] for i in range(16)] if r and len(r)>=128 else None
```
Validated anchor: bp `Z1,28411c,4` (panel receiver FUN_0028411c) reliably fires on panel
input — use it to sanity-check your gdb path. HW write-watchpoints work: `Z2,<addr>,4`.

## 2. What is SOLID (don't re-derive)
- **rbp = `build/extract/rbp`** (ARM32, non-PIE so runtime VA == file VA == Ghidra VA for
  defined procs). Analyze with the REA MCP (ghidra provider). The library accept path is
  pure rbp userspace.
- **Topology is correct every boot:** device at devpath `2-1.1`, kernel mounts
  `/media/usb2/sda1` (slot-specific — so the kernel's Pioneer slot rule DID fire), SCSI
  `sda1` attached, `export.pdb` readable off the disk. dmesg logs `2-1.1: not running at
  top speed` (full-speed) — this is NOT the trigger (the one accepting boot was full-speed
  too). Patch 08 = high-speed hub so the drive can sit at 2-1.1; see docs/usb-slot-topology.md.
- **Rejection is pre-scan:** rbp's library scanner access-check `FUN_0024cb38` @ **`0x24cf14`**
  (bl to access() on the `…/PIONEER/rekordbox/export.pdb` path) **never fires** on a failing
  insert → the "USB Error" is raised before any library read. On a GOOD boot this bp fires
  and `export.pdb` gets opened (fd read to offset 126976/217088).
- **Not caused by the panel/key work:** a clean build with patches 01–08 only (no patch 09
  MAIN-UCOM keys) reproduces the failure identically. Patch 09 idles the key GPIOs high from
  reset, which is correct and irrelevant to USB.
- **rbp opens 8 udev channels:** `/proc/udev_usb{,hid,mtp}{1,2}` and **`/proc/udev_usbctn1|2`**
  (the slot-connect channels). `FUN_002af4d0` opens usbctn1→struct[0], usbctn2→struct[1],
  returns the `{fd1,fd2}` ptr. Its only caller is **`FUN_00292d6c` = `UsbCautionManager` ctor**
  (RTTI `N2ui17Usb...`), which stores the fd-struct at `manager[0x18]` and has a task
  sub-object at `manager[0x10]`.

## 3. What I RULED OUT / corrected (don't repeat these)
- **The UsbCautionManager vtable methods are NOT the event handler.** A coordinated fresh-boot
  trace armed bps on all 7 methods BEFORE the first insert and **none fired**:
  - main vtable @`0x428598`: VAs `0x292c6c, 0x292d50, 0x292924, 0x292818`
  - sub-object vtable @`0x4285b0`: VAs `0x292c64, 0x292d48, 0x292810`
  So the `udev_usbctn2` read is in a **central poll/read loop or a thread not yet found** —
  likely a select()/poll() over the 8 udev fds that dispatches, not these methods.
- Full-speed enumeration is not the cause (above).
- Library format is not the cause: a genuine `baken` `export.pdb` fails identically to the
  old rbxport image; and a GOOD boot loads the same image fine.
- Holding the USB2 key (gpio 158) low during insert does NOT force accept.
- Guest-side `od -t`/`-A` don't exist (busybox); event-channel reads inconclusive so far.

## 4. Concrete next steps (in priority order)
1. **Find rbp's udev poll/read loop.** It holds the `udev_usbctn` fds and calls read()/select().
   Routes to try in REA: xref the `read`/`select`/`poll`/`epoll_wait` PLT imports and find the
   call site that references the caution manager's fd set (fds live at `UsbCautionManager+0x18`,
   a `{fd1,fd2}` malloc(8)); or `procedure_callers` upward from `FUN_002af4d0`/`FUN_00292d6c` to
   whoever starts the thread (look for `pthread_create` passing the manager). The loop reads a
   slot event and decides connect-vs-caution. bp it on a FRESH-boot first insert and read the
   event bytes + the branch.
2. **Determine if the kernel even posts the event on failing boots.** On a fresh failing boot,
   `timeout 2 hexdump -C /proc/udev_usbctn2` (racing rbp) — if it returns bytes, the kernel
   posted but rbp didn't consume (rbp thread race); if it blocks, the kernel didn't post
   (kernel/QEMU-USB timing race). My attempts were inconclusive (busybox `od`); use `hexdump`.
   The Pioneer kernel driver keys the post on devpath `2-1.1`/`2-1.2` string match.
3. **Characterise the race.** Vary the insert timing vs rbp init (touch `/tmp/usb2_insert` at
   e.g. T+20/40/60/90s after `pidof rbp`); find a window that reliably accepts. The one good
   boot (QEMU 87844) loaded on a fresh boot — timing/load difference is the likely variable.
   Keep host load low (kill stray helpers; hundreds of `.ppm` files trigger macOS Spotlight).
4. If it proves a QEMU-USB enumeration timing issue, consider the chipidea-EHCI PORTSC PSPD
   (speed) reporting — but a naive PSPD patch REGRESSED enumeration before (see git log around
   hcd-ehci.c), so measure first.

## 5. Gotchas
- **REA address quirk:** a bare hex arg to REA procedure/decompile tools is taken as a FILE
  OFFSET (+0x8000 bias), so REA "0x292818" → VA 0x29a818 (wrong function). The vtable VALUES
  above are real VAs; to decompile by address use `resolve_containing_procedure` with the VA,
  or pass file-offset = VA − 0x8000. gdb uses the real VAs directly.
- **Flaky:** expect to reboot several times; a result from a single boot is N=1.
- **iPodMgr** is in the USB-probe path and has a nondeterministic, load-sensitive startup
  SIGSEGV; a crashed/raced iPodMgr may be upstream of the accept failure — worth checking
  `dmesg`/thread state for it on a failing boot.
- **Do NOT patch rbp.** Prefer QEMU-side. Kernel/rootfs = last resort.

## 6. Pointers
- `docs/usb-slot-topology.md` — topology + current flaky status.
- `docs/xdj-rx-control-map.md` — MAIN-UCOM GPIO keys (incl. the two-MCU architecture).
- memory `usb-error-investigation.md` — the full RE trail (addresses, corrections).
- `~/Downloads/da43a7.pdf` — XDJ-RX **service manual** (the source that cracked the two-MCU
  split; useful for any hardware-level question).
