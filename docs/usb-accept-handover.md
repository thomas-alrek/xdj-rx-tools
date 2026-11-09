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

## 0b. ROOT CAUSE FOUND (2026-11-09, supersedes §4's "find the udev poll loop")
The accept is a **guest-userspace rendezvous-write race**, not a topology or caution-manager
bug. Proven by static RE of rbp + the live stock rootfs rules:

- rbp's mount-scanner thread = **`UsbMountManager`** running **`FUN_0024cb38`** (no static
  callers ⇒ thread entry). Loop: `ev = read_sf_rbp(chan); switch(ev){case 1 "mount": build
  <mnt>/PIONEER/rekordbox/export.pdb, access(), register lib; …}`.
- **`read_sf_rbp` = `FUN_00296628`** (perror "read_sf_rbp:poll/n"): `poll()`s a 3-fd set
  `{fd0,fd1,fd2}` — fd0/fd1 POLLIN (udev_usb1/2 queues), fd2 POLLPRI+lseek0+read (state file).
  Parses TEXT: `"mount"`→1, `umount`→2, `do_umount`→3, `connect`→4, `disconnect`→5.
- **The asymmetry:** `connect` → `/proc/udev_usbctn2` is a **persistent state file**
  (75-usb-caution.rules) re-read every poll ⇒ rbp ALWAYS knows a device is present ⇒ shows
  **"USB Error"**, never "NO USB". `"mount"` → `/proc/udev_usb2` is written by the stock udev
  rule `12-usb-memory-auto-mount.rules`:
  `mount /dev/%k /media/usb2/%k; …/decrypt_autoexec.sh; echo -n "mount /media/usb2/%k" > /proc/udev_usb2`.
- That proc write is a **BLOCKING RENDEZVOUS — it does not return until a reader reads it**
  (verified live: a manual `echo` hung the console until Ctrl-C; on a boot where rbp's scanner
  was live it returned instantly and rbp re-processed it — "PathDecider:: UNMOUNT was not
  executed before"). On **cold-plug** the device enumerates at t≈0, so udev runs this echo
  BEFORE rbp's scanner polls; **udev's worker times out and kills the blocked echo ⇒ "mount"
  lost, fs stays mounted ⇒ "USB Error".** The rare good boot = rbp was polling in time.
  Explains every symptom (always "USB Error" not "NO USB"; pre-scan; load-sensitive).

### Why post-boot re-plug can't rescue it (and the fix target)
`device_del`/`device_add` makes FSL Host1 bus-suspend, and QEMU can't resume it — guest floods
`host1, _is_usbh1_wakeup, invalid wake up`. Disassembled the guest kernel (`vmlinux.bin`,
`_is_usbh1_wakeup @ 0x803ed770`): it reads **USBNC_base + 0x804, tests bit 31 (OWIR =
wakeup-interrupt-request)**; if 0 it prints "invalid wake up" and refuses the resume. QEMU's
i.MX6 models usbphy + chipidea (176-line stub) but **NOT the USBNC non-core block** (USBOH3+0x800),
so +0x804 reads 0 ⇒ OWIR never set ⇒ Host1 never resumes.

### ✅ FIX B IMPLEMENTED & VERIFIED (2026-11-09): `patches/10-imx6-usbnc-wakeup.patch`
Models the USBNC block at USBOH3+0x800, deriving OWIR (bit31 of each controller's CTRL at
+0x04*n) live from the matching EHCI root port (FPRES|CSC). **Verified end-to-end:** booted
hub-only, waited for `UsbMountManager` == `poll_schedule_timeout`, then `device_add usb-storage`
on 2-1.1 + `touch /tmp/usb2_insert`. Result: `0` "invalid wake up" lines, `ehci_fsl_bus_resume
begins/ends, Host 1`, `usb 2-1.1: new ... device number 3`, auto-mount `/media/usb2/sda1`, rbp
opened `export.pdb` (fd), **no "USB Error" modal**, and the USB2 browser shows the live library
(ARTIST/ALBUM/TRACK… with real entries). Deterministic by construction (insert after rbp polls);
stock firmware untouched. Boot itself is still the separate load-sensitive `gui_task` SIGSEGV risk.

**UPDATE (session 2): OWIR must be FPRES-only, and BOTH slots verified.** The first patch-10
version derived OWIR from `FPRES | CSC`; `CSC` latches on every disconnect, so after the first
autosuspend it kept re-asserting the wakeup → resume → re-enumerate → disconnect → … churn (device
number climbing 3,4,5…). Fixed by deriving OWIR from **`PORTSC_FPRES` only** (transient; the guest
clears it on resume-complete, so Host1 suspends once and STAYS suspended with the device attached).
Re-verified on a fresh boot: insert USB2 (port 1.1) → accept; +45s later `disc=0`, `enum=2`,
suspend=1/resume=1, `export.pdb` still open. Then inserted **USB1** (2nd stick, hub port **1.2** →
`2-1.2` → `/media/usb1/sdb1`, detect `/tmp/usb_insert`): also accepted — rbp holds BOTH `export.pdb`
fds at once (usb1 + usb2), USB1 browser shows the library, still `disc=0`. The suspend-side
`usbh1_platform_rh_suspend_swfix "big error"` is BENIGN (guest polls a PHY line-state QEMU doesn't
model; present on every boot, continues anyway) — not the churn cause.

### Fix plan (B, hardware-accurate, chosen): model USBNC OWIR wakeup in QEMU
Add a USBNC register region at USBOH3+0x800 and set OWIR (bit31 of the per-controller CTRL at
+0x804 for Host1) + raise the USB IRQ when a device attaches to a suspended Host1 with wakeup
enabled, so the guest's `_is_usbh1_wakeup` passes and resume/enumeration completes. Then launch
hub-only, boot to rbp, and **insert the stick after rbp is polling** (device_add + drive the
detect GPIO) — exactly as real hardware does — so the stock udev mount rule + its blocking echo
run while rbp reads ⇒ deterministic accept. (Rejected alt A: cold-plug a keepalive device on an
unused hub port so Host1 never suspends — works but less hardware-faithful.)
Readiness gate for the late insert: poll `/proc/<rbp>/task/<tid>/wchan` for the `UsbMountManager`
thread == `poll_schedule_timeout`.

NB boot is load-sensitive: `gui_task` SIGSEGV (sig 11, PC ~0x2da5xxxx = stubbed-GPU/DirectFB
path) crashes rbp before UI when host load is high (seen at host load avg 6+). Keep load low.

## 0c. ⚠️ IN-PLACE device_del/device_add CYCLING DOES NOT WORK — 2026-11-09 session 2
Patch 10's deterministic insert was verified on a **fresh hub-only boot** (§Fix B). This session
tried to *re-insert in place* on an already-running instance (to restore a library view that was
lost) and it **fails repeatably** — three distinct, reproducible reasons the next agent must know:

1. **`device_del xdjusb` tears down the `-drive if=none,id=usbstick` backend too.** After a
   `device_del`, `info block` shows no `usbstick`, and a re-`device_add ...,drive=usbstick` fails
   with `Property 'usb-storage.drive' can't find value 'usbstick'`. You must recreate it first:
   `drive_add 0 if=none,id=usbstick,file=<ABS>/test/usb/rekordbox_new.img,format=raw,snapshot=on`
   THEN `device_add usb-storage,bus=usb-bus.1,port=1.1,drive=usbstick,id=xdjusb,removable=on`.
   (The hub's child bus is NOT `xdjhub.0`; attach on `usb-bus.1` port `1.1`.)
2. **Every `device_del` drops Host1 into a bus-suspend churn** that patch 10 does not recover from
   in-place: guest dmesg repeats `ehci_fsl_bus_suspend begins/ends, Host 1` +
   `usbh1_platform_rh_suspend_swfix big error`. After a few cycles Host1's wake state is wedged and
   fresh `device_add`+insert either never enumerates or enumerates then immediately
   `USB disconnect`. So the patch-10 wakeup path is only reliable on a **clean boot**, not after
   del/add churn. **RESOLVED (session 2):** the churn was patch 10 OR-ing `PORTSC_CSC` into OWIR —
   CSC latches on disconnect and kept re-asserting the wakeup. Fixed to **FPRES-only** (see §Fix B
   UPDATE); a clean-boot insert is now stable (`disc=0`, one suspend/resume). `swfix` did NOT need
   modeling — its "big error" is benign. In-place `device_del`/`device_add` is still best avoided
   (problem 1's drive-backend teardown is an inherent QEMU-monitor behavior), but the autosuspend
   churn that also hit fresh boots is gone.
3. **Kernel mount succeeds but rbp latches "USB Error".** On a good-looking insert the guest DOES
   mount: `/dev/sda1 on /media/usb2/sda1 type vfat`, `PIONEER/` readable — yet rbp shows
   `USB Error. Remove the device.` The udev "mount" rendezvous echo raced past rbp's reader (§0b).
   **Re-posting it manually does NOT clear a latched error:** running
   `timeout 3 sh -c 'echo -n "mount /media/usb2/sda1" > /proc/udev_usb2'` on the guest (while rbp
   thread is at `poll_schedule_timeout`) left the modal up. Once rbp shows USB Error it appears to
   need a clean *disconnect→reconnect with correct timing*, not just a re-sent mount event.

**Diagnostics that worked this session (reuse them):**
- rbp pid: `pidof rbp`; its UsbMountManager poll thread is the one whose
  `/proc/<pid>/task/<tid>/wchan` == `poll_schedule_timeout` (this session: pid 2346, tid 2347).
- `info usb` / `info block` over the monitor socket show device + drive-backend state. NOTE the
  monitor echoes each typed char back (readline) — strip with `sed 's/\x1b\[[0-9;]*[a-zA-Z]//g' | tr -d '\010'`.
- Guest console helper `con4.py` (§1) for `dmesg | tail`, `mount | grep media`, wchan, the echo test.

**Bottom line for the next agent:** do NOT try to rescue USB by cycling device_del/add on a live
instance — it wedges Host1. Reproduce from a **fresh hub-only boot** per §Fix B, and treat the two
real open problems as (a) does patch 10 cover post-suspend re-attach / the `swfix` path, and (b) the
rbp-side latched-"USB Error" recovery (what clean disconnect timing clears it). The static firmware
side (`UsbMountManager` `FUN_0024cb38`, `read_sf_rbp` `FUN_00296628`, scanner bp `0x24cf14`) is in §2.

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
