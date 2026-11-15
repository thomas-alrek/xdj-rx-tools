//! The virtual-panel server, in-process: the Rust counterpart of
//! tools/xdj-panel/server.py's socket mode (server.py is the dev/debug-launcher server,
//! with its /tmp-file mode, HMP screendump and calibration tools). The two must behave
//! the same; tools/xdj-panel/test_panel_parity.py checks it.
//!
//! Serves panel.html / logs.html / noVNC / knobs and turns panel actions into the
//! QEMU harness's control protocol: the WHOLE desired sub-ucom state as one
//! NUL-terminated snapshot per write on the `xdjctrl` chardev socket (QEMU is the
//! server, we are its one client), the MAIN-UCOM key GPIO on `xdjkey`. rbp's LED
//! frames come back up `xdjctrl` as "T <hex>" lines and fan out to /leds (SSE).
//!
//!   POST /ctrl     "A 2c 3ff" / "B 12 08" / "b 5e 01" / "S 5a e0 20" (one per line)
//!   POST /release  "B 12 08" (or empty = all)     POST /jog  "1 move <deg> <sec>" | "1 touch 1"
//!   POST /input    "<0|1> DECK|LINE"              POST /browse "<signed detents>"
//!   POST /mainkey  "<gpio>" / /mainkey_release    POST /usb  "<usb1|usb2> <path>"
//!   POST /power    on|off|toggle [update|test]    GET  /power -> {available,state,update,pending}
//!   GET  /leds (SSE; ?once=1 = JSON)              GET  /log/<subucom|console> (SSE)
//!   POST /log/note                                GET/POST /cal, /savecal, /cal-ref (dev)
//!   POST /install-update "<path to .UPD/.zip>"  stick from it + cold boot into update mode

use std::collections::{BTreeMap, BTreeSet, HashMap, VecDeque};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use axum::body::{Body, Bytes};
use axum::http::{header, HeaderValue, Method, Response, StatusCode, Uri};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::UnixStream;
use tokio::sync::{mpsc, oneshot, watch};

use crate::PowerCmd;

type PowerTx = mpsc::Sender<(PowerCmd, oneshot::Sender<String>)>;

const LOG_MAX: usize = 5000;
const LED_SKIP: [usize; 8] = [0x00, 0x04, 0x08, 0x09, 0x0C, 0x0D, 0x0E, 0x0F]; // jog rings + meters
const TEST_MODE_KEYS: [&str; 2] = ["B 12 08", "B 13 04"];
const JOG_BASE: [i64; 2] = [0x00, 0x10]; // deck1, deck2
const JOG_TICKS_PER_REV: f64 = 1024.0;
const JOG_SPEED_K: f64 = 416.67; // rbp: speed = 416.67 / period (FUN_00283dc4)
const JOG_NORMAL_RPS: f64 = 100.0 / 3.0 / 60.0;
const JOG_IDLE_S: f64 = 0.12;
const INPUT_BIT: [(i64, i64); 2] = [(0x33, 0x80), (0x43, 0x80)]; // rbp[0x33].7 / [0x43].7 = DECK

pub struct PanelConfig {
    pub port: u16,
    pub panel_dir: PathBuf,
    pub novnc_dir: PathBuf,
    pub knobs_dir: PathBuf,
    pub ctrl_sock: PathBuf,
    pub key_sock: PathBuf,
    pub qmp_sock: PathBuf,
    pub console: PathBuf,
}

#[derive(Default, Clone, Copy)]
struct Jog {
    cnt: i64,
    per: i64,
    rev: bool,
    touch: bool,
    active: bool,
    t: f64,
}

/// The panel's complete control state (server.py's module globals under _browse_lock).
#[derive(Default)]
struct State {
    analog: BTreeMap<i64, i64>,
    held: BTreeSet<(i64, i64)>,
    switch: HashMap<(i64, i64), i64>,
    jog: [Jog; 2],
    browse_counter: i64,
    browse_active: bool,
    input_src: [bool; 2],
}

enum ConnMsg {
    Send(String),
    Reset,
}

struct Leds {
    hex: String,
    seq: u64,
    t: f64,
}

struct Logs {
    lines: HashMap<&'static str, VecDeque<(u64, String)>>,
    next: HashMap<&'static str, u64>,
}

pub struct Panel {
    cfg: PanelConfig,
    st: Mutex<State>,
    ctrl_tx: mpsc::UnboundedSender<ConnMsg>,
    key_tx: mpsc::UnboundedSender<ConnMsg>,
    leds: Mutex<Leds>,
    leds_w: watch::Sender<u64>,
    led_prev: Mutex<HashMap<usize, (u8, f64)>>,
    logs: Mutex<Logs>,
    logs_w: watch::Sender<u64>,
    power: PowerTx,
    pending_usb: Mutex<BTreeMap<String, String>>,
    usb_cur: Mutex<HashMap<String, (String, String)>>,
    /// rbp signalled standby (TX 'S') and we're cutting power; reset at power on.
    standby: AtomicBool,
}

fn now_f() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

fn hhmmss() -> String {
    #[cfg(unix)]
    unsafe {
        let t = libc::time(std::ptr::null_mut());
        let mut tm: libc::tm = std::mem::zeroed();
        if !libc::localtime_r(&t, &mut tm).is_null() {
            return format!("{:02}:{:02}:{:02}", tm.tm_hour, tm.tm_min, tm.tm_sec);
        }
    }
    String::from("00:00:00")
}

/// Python's int(s, 16): optional sign and 0x prefix, underscores allowed between digits.
fn hexint(s: &str) -> Option<i64> {
    let (neg, b) = match s.strip_prefix('-') {
        Some(r) => (true, r),
        None => (false, s.strip_prefix('+').unwrap_or(s)),
    };
    let b = b.strip_prefix("0x").or_else(|| b.strip_prefix("0X")).unwrap_or(b);
    if b.is_empty() || b.starts_with('_') || b.ends_with('_') || b.contains("__") {
        return None;
    }
    let v = i64::from_str_radix(&b.replace('_', ""), 16).ok()?;
    Some(if neg { -v } else { v })
}

/// Python's "%0Nx" for possibly-negative ints (the sign counts toward the width).
fn hx(v: i64, w: usize) -> String {
    if v < 0 { format!("-{:0w$x}", -v, w = w.saturating_sub(1)) } else { format!("{:0w$x}", v, w = w) }
}

impl State {
    fn jog_lines(&self) -> Vec<String> {
        let mut out = Vec::new();
        for (d, j) in self.jog.iter().enumerate() {
            if !j.active {
                continue;
            }
            let b = JOG_BASE[d];
            out.push(format!("A {} {}", hx(b + 0x0c, 2), hx(j.per & 0xffff, 0)));
            out.push(format!("A {} {}", hx(b + 0x0e, 2), hx(j.cnt & 0xffff, 0)));
            let flags = (if j.touch { 1 } else { 0 }) | (if j.rev { 2 } else { 0 });
            if flags != 0 {
                out.push(format!("B {} {}", hx(b + 0x10, 2), hx(flags, 2)));
            }
        }
        out
    }

    /// _compose_ctrl: persistent state + one-shot lines, re-sent whole on every write.
    fn compose(&self, shots: &[String]) -> String {
        let mut out: Vec<String> =
            self.analog.iter().map(|(o, v)| format!("A {} {}", hx(*o, 2), hx(v & 0xffff, 0))).collect();
        let mut merged: BTreeMap<i64, i64> = BTreeMap::new();
        for (off, mask) in &self.held {
            *merged.entry(*off).or_insert(0) |= mask;
        }
        for ((off, mask), bits) in &self.switch {
            if bits & mask != 0 {
                *merged.entry(*off).or_insert(0) |= bits & mask;
            }
        }
        out.extend(merged.iter().map(|(o, m)| format!("B {} {}", hx(*o, 2), hx(*m, 2))));
        out.extend(self.jog_lines());
        if self.browse_active {
            out.push(format!("A 54 {}", hx(self.browse_counter, 0)));
        }
        for (ch, on) in self.input_src.iter().enumerate() {
            if *on {
                let (o, m) = INPUT_BIT[ch];
                out.push(format!("B {} {}", hx(o, 2), hx(m, 2)));
            }
        }
        for ln in shots {
            let t = ln.trim();
            if t.starts_with('a') || t.starts_with('b') {
                out.push(t.to_string());
            }
        }
        out.join("\n") + "\n"
    }

    /// _apply_lines: fold A/B/S lines into the state; return the one-shot lines.
    fn apply(&mut self, lines: &[String]) -> Vec<String> {
        let mut shots = Vec::new();
        for ln in lines {
            let p: Vec<&str> = ln.split_whitespace().collect();
            if p.len() == 4 && p[0] == "S" {
                if let (Some(o), Some(m), Some(b)) = (hexint(p[1]), hexint(p[2]), hexint(p[3])) {
                    self.switch.insert((o, m), b);
                }
                continue;
            }
            if p.len() != 3 || !"ABab".contains(p[0]) {
                continue;
            }
            let (Some(off), Some(val)) = (hexint(p[1]), hexint(p[2])) else { continue };
            match p[0] {
                "A" => {
                    if off != 0x54 {
                        self.analog.insert(off, val); // 0x54 = the browse counter, owned by /browse
                    }
                }
                "B" => {
                    self.held.insert((off, val));
                }
                _ => shots.push(ln.clone()),
            }
        }
        shots
    }
}

impl Panel {
    pub fn new(cfg: PanelConfig, power: PowerTx) -> Arc<Panel> {
        let (ctrl_tx, ctrl_rx) = mpsc::unbounded_channel();
        let (key_tx, key_rx) = mpsc::unbounded_channel();
        let p = Arc::new(Panel {
            cfg,
            st: Mutex::new(State { input_src: [true, true], ..Default::default() }),
            ctrl_tx,
            key_tx,
            leds: Mutex::new(Leds { hex: String::new(), seq: 0, t: 0.0 }),
            leds_w: watch::channel(0).0,
            led_prev: Mutex::new(HashMap::new()),
            logs: Mutex::new(Logs {
                lines: HashMap::from([("subucom", VecDeque::new()), ("console", VecDeque::new())]),
                next: HashMap::from([("subucom", 0), ("console", 0)]),
            }),
            logs_w: watch::channel(0).0,
            power,
            pending_usb: Mutex::new(BTreeMap::new()),
            usb_cur: Mutex::new(HashMap::new()),
            standby: AtomicBool::new(false),
        });
        tokio::spawn(ctrl_conn(p.clone(), ctrl_rx));
        tokio::spawn(key_conn(p.cfg.key_sock.clone(), key_rx));
        tokio::spawn(jog_idle_loop(p.clone()));
        tokio::spawn(console_loop(p.clone()));
        p.push(); // default INPUT SELECT = DECK (no client yet: just sets the state)
        p
    }

    /// Compose + queue the snapshot under the state lock (keeps writes ordered).
    fn push_locked(&self, st: &State, shots: &[String]) {
        let _ = self.ctrl_tx.send(ConnMsg::Send(st.compose(shots)));
    }

    fn push(&self) {
        let st = self.st.lock().unwrap();
        self.push_locked(&st, &[]);
    }

    fn logline(&self, src: &'static str, text: &str) {
        let mut lg = self.logs.lock().unwrap();
        let Logs { lines, next } = &mut *lg;
        let buf = lines.get_mut(src).unwrap();
        let n = next.get_mut(src).unwrap();
        let parts: Vec<&str> = if text.is_empty() { vec![""] } else { text.lines().collect() };
        for ln in parts {
            let l = if src == "console" { ln.to_string() } else { format!("{}  {ln}", hhmmss()) };
            buf.push_back((*n, l));
            *n += 1;
        }
        while buf.len() > LOG_MAX {
            buf.pop_front();
        }
        drop(lg);
        self.logs_w.send_modify(|v| *v += 1);
    }

    fn led_diff(&self, old: &str, new: &str) {
        let dec = |s: &str| -> Option<Vec<u8>> {
            (0..s.len() / 2).map(|i| u8::from_str_radix(&s[2 * i..2 * i + 2], 16).ok()).collect()
        };
        let (Some(a), Some(b)) = (dec(old), dec(new)) else { return };
        let now = now_f();
        let mut prev = self.led_prev.lock().unwrap();
        let mut ch = Vec::new();
        for i in 0..a.len().min(b.len()) {
            if a[i] == b[i] || LED_SKIP.contains(&i) {
                continue;
            }
            let (pv, t) = prev.get(&i).copied().map(|(v, t)| (Some(v), t)).unwrap_or((None, 0.0));
            prev.insert(i, (a[i], now));
            if pv == Some(b[i]) && now - t < 1.0 {
                continue; // a blink, not a state change
            }
            ch.push(format!("{i:02x}:{:02x}>{:02x}", a[i], b[i]));
        }
        if b.len() > a.len() {
            ch.push(format!("{:02x}:->{:02x}", a.len(), b[a.len()]));
        }
        drop(prev);
        if !ch.is_empty() {
            self.logline("subucom", &format!("<- LED  {}", ch.join("  ")));
        }
    }

    /// TX payload byte 0x11 = 'S' (0x53): rbp's "ready for standby" to the panel MCU.
    fn tx_says_standby(hex: &str) -> bool {
        hex.get(34..36) == Some("53")
    }

    fn leds_update(self: &Arc<Self>, hexstr: &str) {
        let h = hexstr.trim().to_lowercase().replace(' ', "");
        if !(h.len() == 40 || h.len() == 42) || !h.chars().all(|c| c.is_ascii_hexdigit()) {
            return;
        }
        let mut l = self.leds.lock().unwrap();
        if h != l.hex {
            let old = std::mem::replace(&mut l.hex, h.clone());
            l.seq += 1;
            l.t = now_f();
            let seq = l.seq;
            drop(l);
            if !old.is_empty() && old != "off" {
                self.led_diff(&old, &h);
            }
            self.leds_w.send_replace(seq);
            if Self::tx_says_standby(&h) && !self.standby.swap(true, Ordering::SeqCst) {
                tokio::spawn(standby_cut(self.clone()));
            }
        }
    }

    fn leds_off(&self) {
        let mut l = self.leds.lock().unwrap();
        l.hex = "off".into();
        l.seq += 1;
        let seq = l.seq;
        drop(l);
        self.leds_w.send_replace(seq);
    }

    async fn power_cmd(&self, c: PowerCmd) -> String {
        let (tx, rx) = oneshot::channel();
        if self.power.send((c, tx)).await.is_err() {
            return "off".into();
        }
        rx.await.unwrap_or_else(|_| "off".into())
    }

    fn console_has(&self, marker: &str) -> bool {
        std::fs::read(&self.cfg.console)
            .map(|b| b.windows(marker.len()).any(|w| w == marker.as_bytes()))
            .unwrap_or(false)
    }
}

// ---- the QEMU chardev connections ----------------------------------------------------

/// xdjctrl: send snapshots in order, read back "T <hex>" LED lines, re-send the full
/// state 0.3 s after every (re)connect (a fresh QEMU has none). Unconnected snapshots
/// are dropped -- the reconnect push carries the state (one-shots are lost, as in .py).
async fn ctrl_conn(p: Arc<Panel>, mut rx: mpsc::UnboundedReceiver<ConnMsg>) {
    loop {
        let mut stream = match UnixStream::connect(&p.cfg.ctrl_sock).await {
            Ok(s) => s,
            Err(_) => {
                // not up: drain (drop) queued snapshots while we wait
                let deadline = tokio::time::sleep(Duration::from_millis(500));
                tokio::pin!(deadline);
                loop {
                    tokio::select! {
                        _ = &mut deadline => break,
                        m = rx.recv() => if m.is_none() { return },
                    }
                }
                continue;
            }
        };
        let p2 = p.clone();
        tokio::spawn(async move {
            tokio::time::sleep(Duration::from_millis(300)).await;
            p2.push();
        });
        let mut buf: Vec<u8> = Vec::new();
        let mut rd = [0u8; 4096];
        loop {
            tokio::select! {
                m = rx.recv() => match m {
                    None => return,
                    Some(ConnMsg::Reset) => break,
                    Some(ConnMsg::Send(s)) => {
                        let mut b = s.into_bytes();
                        b.push(0);
                        if stream.write_all(&b).await.is_err() { break; }
                    }
                },
                n = stream.read(&mut rd) => match n {
                    Ok(0) | Err(_) => break,
                    Ok(n) => {
                        buf.extend_from_slice(&rd[..n]);
                        while let Some(i) = buf.iter().position(|&c| c == b'\n') {
                            let ln: Vec<u8> = buf.drain(..=i).collect();
                            if let Some(h) = ln.strip_prefix(b"T ") {
                                p.leds_update(&String::from_utf8_lossy(h));
                            }
                        }
                    }
                },
            }
        }
        tokio::time::sleep(Duration::from_millis(500)).await;
    }
}

/// xdjkey: the MAIN-UCOM key GPIO (decimal, empty = released), one record per write.
async fn key_conn(path: PathBuf, mut rx: mpsc::UnboundedReceiver<ConnMsg>) {
    let mut conn: Option<UnixStream> = None;
    while let Some(m) = rx.recv().await {
        match m {
            ConnMsg::Reset => conn = None,
            ConnMsg::Send(s) => {
                let mut b = s.into_bytes();
                b.push(0);
                for _ in 0..2 {
                    if conn.is_none() {
                        conn = UnixStream::connect(&path).await.ok();
                    }
                    let Some(c) = conn.as_mut() else { break };
                    if c.write_all(&b).await.is_ok() {
                        break;
                    }
                    conn = None; // drop + retry a fresh connection once
                }
            }
        }
    }
}

// ---- background loops ------------------------------------------------------------------

/// A real jog reports period 0 once the platter stops: zero it JOG_IDLE_S after the last move.
async fn jog_idle_loop(p: Arc<Panel>) {
    loop {
        tokio::time::sleep(Duration::from_millis(40)).await;
        let now = now_f();
        let mut st = p.st.lock().unwrap();
        let mut dirty = false;
        for j in st.jog.iter_mut() {
            if j.per != 0 && now - j.t > JOG_IDLE_S {
                j.per = 0;
                dirty = true;
            }
        }
        if dirty {
            p.push_locked(&st, &[]);
        }
    }
}

/// Tail the guest console into the "console" log (it appears late / restarts per power-on).
async fn console_loop(p: Arc<Panel>) {
    use std::io::{Read, Seek, SeekFrom};
    #[cfg(unix)]
    use std::os::unix::fs::MetadataExt;
    let (mut pos, mut ino, mut part): (u64, u64, Vec<u8>) = (0, u64::MAX, Vec::new());
    loop {
        if let Ok(md) = std::fs::metadata(&p.cfg.console) {
            #[cfg(unix)]
            let cur_ino = md.ino();
            #[cfg(not(unix))]
            let cur_ino = 0;
            if cur_ino != ino || md.len() < pos {
                ino = cur_ino;
                pos = md.len().saturating_sub(200_000);
                part.clear();
            }
            if md.len() > pos {
                if let Ok(mut f) = std::fs::File::open(&p.cfg.console) {
                    let mut data = Vec::new();
                    if f.seek(SeekFrom::Start(pos)).is_ok() {
                        let _ = f.take(1_000_000).read_to_end(&mut data);
                    }
                    pos += data.len() as u64;
                    part.extend(data);
                    if let Some(last) = part.iter().rposition(|&c| c == b'\n') {
                        let rest = part.split_off(last + 1);
                        let text: Vec<String> = part[..last]
                            .split(|&c| c == b'\n')
                            .map(|l| String::from_utf8_lossy(l.strip_suffix(b"\r").unwrap_or(l)).into_owned())
                            .collect();
                        part = rest;
                        p.logline("console", &text.join("\n"));
                    }
                }
            }
        }
        tokio::time::sleep(Duration::from_millis(250)).await;
    }
}

/// Test mode: release MEMORY + TEMPO RANGE once rbp is certainly past its first-frame
/// boot-key check -- "ALSA: Resolve xrun" (see server.py _test_mode_boot for why the
/// earlier signals raced). Fallback: 3 min, or powered off.
async fn test_mode_boot(p: Arc<Panel>) {
    let t0 = Instant::now();
    while t0.elapsed() < Duration::from_secs(180) {
        if t0.elapsed() > Duration::from_secs(5) && p.power_cmd(PowerCmd::Status).await == "off" {
            break;
        }
        if p.console_has("Resolve xrun") {
            tokio::time::sleep(Duration::from_secs(2)).await;
            break;
        }
        tokio::time::sleep(Duration::from_millis(500)).await;
    }
    {
        let mut st = p.st.lock().unwrap();
        for ln in TEST_MODE_KEYS {
            let w: Vec<&str> = ln.split_whitespace().collect();
            if let (Some(o), Some(m)) = (hexint(w[1]), hexint(w[2])) {
                st.held.remove(&(o, m));
            }
        }
        p.push_locked(&st, &[]);
    }
    p.logline("subucom", "·  test mode: released MEMORY + TEMPO RANGE");
}

/// The panel MCU's half of the power-off handshake (docs/service-modes.md): once rbp
/// has prepared for standby it sends 'S' (TX byte 0x11) and the MCU cuts the i.MX6's
/// power. Covers a POWER-key press and rbp's own AUTO STANDBY alike.
async fn standby_cut(p: Arc<Panel>) {
    if !p.power_cmd(PowerCmd::Status).await.starts_with("on") {
        return;
    }
    p.logline("subucom", "·  rbp is ready for standby (TX 'S'): the panel MCU cuts power");
    p.logline("console", "──────── standby: power off ────────");
    p.power_cmd(PowerCmd::Off).await;
    p.leds_off();
    let _ = p.ctrl_tx.send(ConnMsg::Reset);
    let _ = p.key_tx.send(ConnMsg::Reset);
}

/// USB LOADs queued while off go in once the guest is ready (maybe_insert_usb's rules).
async fn insert_pending(p: Arc<Panel>, update: bool) {
    let (marker, settle) = if update { ("USB update mount", 2) } else { ("Resolve xrun", 6) };
    let t0 = Instant::now();
    loop {
        if t0.elapsed() >= Duration::from_secs(300) {
            p.logline("subucom", "·  USB insert: gave up waiting for the guest");
            return;
        }
        if p.power_cmd(PowerCmd::Status).await == "off" {
            return;
        }
        if p.console_has(marker) {
            break;
        }
        tokio::time::sleep(Duration::from_secs(1)).await;
    }
    tokio::time::sleep(Duration::from_secs(settle)).await;
    let items: Vec<(String, String)> = std::mem::take(&mut *p.pending_usb.lock().unwrap()).into_iter().collect();
    for (slot, img) in items {
        match mount_usb(&p, &slot, &img).await {
            Ok(m) => p.logline("subucom", &format!("·  inserting queued {slot}: {m}")),
            Err(e) => p.logline("subucom", &format!("·  queued {slot} insert failed: {e}")),
        }
    }
}

// ---- USB (QMP human-monitor-command, like the host's insert) ---------------------------

async fn mon_quiet(p: &Panel, cmds: &[String]) {
    let _ = crate::qmp_hmp(&p.cfg.qmp_sock, cmds).await;
}

async fn eject_usb(p: &Panel, slot: &str) {
    let fault = if slot == "usb1" { "/tmp/usb_insert" } else { "/tmp/usb2_insert" };
    let mut pairs = Vec::new();
    if let Some(c) = p.usb_cur.lock().unwrap().get(slot).cloned() {
        pairs.push(c);
    }
    pairs.push((format!("xdjusb_{slot}"), format!("xdjusb_{slot}_dev")));
    for (_, dev) in &pairs {
        mon_quiet(p, &[format!("device_del {dev}")]).await; // async unplug frees the port
    }
    tokio::time::sleep(Duration::from_millis(400)).await;
    for (drive, _) in &pairs {
        mon_quiet(p, &[format!("drive_del {drive}")]).await;
    }
    let _ = std::fs::remove_file(fault); // never leave the USB fault flag asserted
    p.usb_cur.lock().unwrap().remove(slot);
    tokio::time::sleep(Duration::from_millis(150)).await;
}

/// Insert `img` (file or /dev device) on usb1|usb2, ejecting what was there first.
async fn mount_usb(p: &Panel, slot: &str, img: &str) -> Result<String, String> {
    let (port, fault) = if slot == "usb1" { ("1.2", "/tmp/usb_insert") } else { ("1.1", "/tmp/usb2_insert") };
    let real = std::fs::canonicalize(img).unwrap_or_else(|_| PathBuf::from(img));
    if img.starts_with("/dev/") {
        crate::unmount_host_disk(img);
    }
    eject_usb(p, slot).await;
    // HMP drive_add doesn't quote paths: symlink to a space-free path.
    let link = PathBuf::from(format!("/tmp/xdjpanel_{slot}.img"));
    let _ = std::fs::remove_file(&link);
    #[cfg(unix)]
    std::os::unix::fs::symlink(&real, &link).map_err(|e| format!("cannot stage {}: {e}", link.display()))?;
    let ms = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_millis()).unwrap_or(0);
    let did = format!("xdjpanel_{slot}_{ms}");
    let dev = format!("{did}_dev");
    let cmds = vec![
        format!("drive_add 0 if=none,id={did},file={},format=raw,snapshot=on", link.display()),
        format!("device_add usb-storage,bus=usb-bus.1,port={port},drive={did},id={dev},removable=on"),
    ];
    let outs = crate::qmp_hmp(&p.cfg.qmp_sock, &cmds).await?;
    let blob = outs.join("\n").to_lowercase();
    if ["\"error\"", "error:", "failed", "duplicate", "in use", "extraneous", "can't find"].iter().any(|e| blob.contains(e)) {
        return Err(format!("monitor rejected insert: {}", outs.join(" ")));
    }
    p.usb_cur.lock().unwrap().insert(slot.into(), (did, dev));
    let _ = std::fs::remove_file(fault);
    let name = real.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
    Ok(format!("mounted {name} on {} (read-only)", slot.to_uppercase()))
}

// ---- HTTP -------------------------------------------------------------------------------

fn resp(code: u16, ctype: &str, body: impl Into<Body>) -> Response<Body> {
    let mut r = Response::new(body.into());
    *r.status_mut() = StatusCode::from_u16(code).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);
    let h = r.headers_mut();
    h.insert(header::CONTENT_TYPE, HeaderValue::from_str(ctype).unwrap());
    h.insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    r
}

fn text(code: u16, s: impl Into<String>) -> Response<Body> {
    resp(code, "text/plain", s.into())
}

fn ctype_of(p: &Path) -> &'static str {
    match p.extension().and_then(|e| e.to_str()).map(|e| e.to_lowercase()).as_deref() {
        Some("png") => "image/png",
        Some("jpg") | Some("jpeg") => "image/jpeg",
        Some("webp") => "image/webp",
        Some("json") => "application/json; charset=utf-8",
        Some("html") => "text/html; charset=utf-8",
        Some("css") => "text/css",
        Some("js") => "application/javascript",
        Some("svg") => "image/svg+xml",
        _ => "application/octet-stream",
    }
}

fn send_file(base: &Path, rel: &str) -> Response<Body> {
    let rel = rel.trim_start_matches('/');
    // normpath-style containment: no '..' components, no absolute paths
    if Path::new(rel).components().any(|c| !matches!(c, std::path::Component::Normal(_) | std::path::Component::CurDir)) {
        return text(403, "forbidden");
    }
    let fp = base.join(rel);
    if !fp.is_file() {
        return text(404, "not found");
    }
    match std::fs::read(&fp) {
        Ok(b) => resp(200, ctype_of(&fp), b),
        Err(e) => text(500, e.to_string()),
    }
}

fn sse_headers(body: Body) -> Response<Body> {
    let mut r = resp(200, "text/event-stream", body);
    r.headers_mut().insert(header::CONNECTION, HeaderValue::from_static("close"));
    r
}

fn json_str(s: &str) -> String {
    let mut o = String::from("\"");
    for c in s.chars() {
        match c {
            '"' => o.push_str("\\\""),
            '\\' => o.push_str("\\\\"),
            '\n' => o.push_str("\\n"),
            '\r' => o.push_str("\\r"),
            '\t' => o.push_str("\\t"),
            c if (c as u32) < 0x20 => o.push_str(&format!("\\u{:04x}", c as u32)),
            c if (c as u32) > 0x7e => {
                let mut b = [0u16; 2];
                for u in c.encode_utf16(&mut b) {
                    o.push_str(&format!("\\u{:04x}", u));
                }
            }
            c => o.push(c),
        }
    }
    o.push('"');
    o
}

/// Serve the panel until the process ends. Errors only if the port can't be bound.
pub async fn serve(p: Arc<Panel>) -> std::io::Result<()> {
    let listener = tokio::net::TcpListener::bind(("127.0.0.1", p.cfg.port)).await?;
    eprintln!("[panel] http://127.0.0.1:{}  (ctrl={} key={})", p.cfg.port,
              p.cfg.ctrl_sock.display(), p.cfg.key_sock.display());
    let app = axum::Router::new().fallback(move |method: Method, uri: Uri, body: Bytes| {
        let p = p.clone();
        async move { handle(p, method, uri, body).await }
    });
    axum::serve(listener, app).await
}

async fn handle(p: Arc<Panel>, method: Method, uri: Uri, body: Bytes) -> Response<Body> {
    let path = uri.path().to_string();
    let query = uri.query().unwrap_or("").to_string();
    if method == Method::GET || method == Method::HEAD {
        get(p, &path, &query).await
    } else if method == Method::POST {
        post(p, &path, String::from_utf8_lossy(&body).into_owned()).await
    } else {
        text(405, "method not allowed")
    }
}

async fn get(p: Arc<Panel>, path: &str, query: &str) -> Response<Body> {
    let pd = p.cfg.panel_dir.clone();
    let assets = pd.join("assets");
    match path {
        "/" | "/panel.html" => send_file(&pd, "panel.html"),
        "/logs.html" => send_file(&pd, "logs.html"),
        _ if path.starts_with("/assets/") => send_file(&assets, &path["/assets/".len()..]),
        _ if path.starts_with("/novnc/") => send_file(&p.cfg.novnc_dir, &path["/novnc/".len()..]),
        _ if path.starts_with("/knobs/") => send_file(&p.cfg.knobs_dir, &path["/knobs/".len()..]),
        "/cal-ref" => match std::env::var("XDJ_CAL_REF").ok().filter(|r| Path::new(r).is_file()) {
            Some(r) => match std::fs::read(&r) {
                Ok(b) => resp(200, "image/png", b),
                Err(e) => text(500, e.to_string()),
            },
            None => text(404, "no XDJ_CAL_REF"),
        },
        "/cal" => {
            if assets.join("calibration.json").is_file() {
                send_file(&assets, "calibration.json")
            } else {
                resp(404, "application/json", "{}")
            }
        }
        "/power" => {
            let st = p.power_cmd(PowerCmd::Status).await;
            let pend = p.pending_usb.lock().unwrap().clone();
            let pend_s: Vec<String> = pend.iter().map(|(k, v)| format!("{}: {}", json_str(k), json_str(v))).collect();
            let body = format!(
                "{{\"available\": true, \"state\": {}, \"update\": {}, \"pending\": {{{}}}}}",
                json_str(st.split_whitespace().next().unwrap_or("off")),
                st.ends_with("update"),
                pend_s.join(", ")
            );
            resp(200, "application/json", body)
        }
        "/log/subucom" | "/log/console" => {
            let src: &'static str = if path.ends_with("subucom") { "subucom" } else { "console" };
            let w = p.logs_w.subscribe();
            let stream = futures_util::stream::unfold((p, 0u64, w), move |(p, nxt, mut w)| async move {
                let cur = p.logs.lock().unwrap().next[src];
                if cur == nxt {
                    let _ = tokio::time::timeout(Duration::from_secs(15), w.changed()).await;
                }
                w.borrow_and_update();
                let (out, n) = {
                    let lg = p.logs.lock().unwrap();
                    let out: Vec<String> =
                        lg.lines[src].iter().filter(|(i, _)| *i >= nxt).map(|(_, l)| l.clone()).collect();
                    (out, lg.next[src])
                };
                let chunk = if out.is_empty() {
                    ": ping\n\n".to_string()
                } else {
                    let start = out.len().saturating_sub(LOG_MAX);
                    out[start..].iter().map(|l| format!("data: {}\n\n", l.replace('\r', ""))).collect()
                };
                Some((Ok::<_, std::io::Error>(Bytes::from(chunk)), (p, n, w)))
            });
            sse_headers(Body::from_stream(stream))
        }
        "/leds" => {
            if query.split('&').any(|kv| kv == "once=1") || query.contains("once=1") {
                let l = p.leds.lock().unwrap();
                let body = format!("{{\"hex\": {}, \"seq\": {}, \"t\": {}}}", json_str(&l.hex), l.seq, l.t);
                return resp(200, "application/json", body);
            }
            let w = p.leds_w.subscribe();
            let stream = futures_util::stream::unfold((p, u64::MAX, w), |(p, seen, mut w)| async move {
                let cur = p.leds.lock().unwrap().seq;
                if cur == seen {
                    let _ = tokio::time::timeout(Duration::from_secs(15), w.changed()).await;
                }
                let (seq, hexs) = {
                    let l = p.leds.lock().unwrap();
                    (l.seq, l.hex.clone())
                };
                let chunk = if seq != seen && !hexs.is_empty() { format!("data: {hexs}\n\n") } else { ": ping\n\n".into() };
                w.borrow_and_update();
                Some((Ok::<_, std::io::Error>(Bytes::from(chunk)), (p, seq, w)))
            });
            sse_headers(Body::from_stream(stream))
        }
        "/raw" => text(503, "no framebuffer (is QEMU on the monitor socket?)"),
        _ => text(404, "not found"),
    }
}

async fn post(p: Arc<Panel>, path: &str, body: String) -> Response<Body> {
    let lines = |b: &str| -> Vec<String> {
        b.trim().split('\n').map(|l| l.trim().to_string()).filter(|l| !l.is_empty()).collect()
    };
    if path == "/log/note" {
        let t: String = body.trim().chars().take(500).collect();
        p.logline("subucom", &format!("·  {t}"));
        return text(200, "ok");
    }
    if ["/ctrl", "/release", "/input", "/mainkey", "/mainkey_release", "/browse", "/usb"].contains(&path) {
        let joined = lines(&body).join(" | ");
        let shown = if joined.is_empty() { "(all)".to_string() } else { joined };
        p.logline("subucom", &format!("-> {:<16} {shown}", &path[1..]));
    } else if path == "/jog" && body.contains("touch") {
        p.logline("subucom", &format!("-> jog              {}", body.trim()));
    }
    match path {
        "/power" => {
            let w: Vec<&str> = body.split_whitespace().collect();
            let cur = p.power_cmd(PowerCmd::Status).await;
            let mut want = w.first().copied().unwrap_or("toggle").to_string();
            let upd = w.iter().skip(1).any(|x| *x == "update");
            let test = w.iter().skip(1).any(|x| *x == "test") && !upd;
            if want == "toggle" {
                want = if cur.starts_with("on") { "off".into() } else { "on".into() };
            }
            let st;
            if want == "on" {
                // fresh power cycle: the POWER key is up, no standby in progress
                p.standby.store(false, Ordering::SeqCst);
                {
                    let mut s = p.st.lock().unwrap();
                    if s.held.remove(&(0x5d, 0x01)) {
                        p.push_locked(&s, &[]);
                    }
                }
                if test {
                    // The keys must be down at rbp's first frame: cold boot with them held.
                    if cur.starts_with("on") {
                        p.power_cmd(PowerCmd::Off).await;
                        p.leds_off();
                    }
                    let mut s = p.st.lock().unwrap();
                    s.apply(&TEST_MODE_KEYS.iter().map(|x| x.to_string()).collect::<Vec<_>>());
                    p.push_locked(&s, &[]);
                }
                st = p.power_cmd(PowerCmd::On(upd)).await;
                if test {
                    tokio::spawn(test_mode_boot(p.clone()));
                }
                let mode = if upd { " (firmware update mode)" } else if test { " (test mode)" } else { "" };
                p.logline("subucom", &format!("·  POWER ON{mode}"));
                p.logline("console", &format!("──────── power on{mode} ────────"));
                if !p.pending_usb.lock().unwrap().is_empty() {
                    tokio::spawn(insert_pending(p.clone(), upd));
                }
            } else {
                // Like the unit: the POWER key goes to the panel MCU, which asks rbp to prepare
                // (code 0x8001) and cuts power when rbp answers 'S' (standby_cut). Only while
                // rbp runs a normal boot; otherwise (booting, update mode) or without an answer
                // in 10 s it's a plain power cut, as before.
                if cur == "on" && p.console_has("commandline read: rbp") {
                    p.logline("subucom", "·  POWER key: asking rbp to prepare for standby");
                    {
                        let mut s = p.st.lock().unwrap();
                        s.held.insert((0x5d, 0x01));
                        p.push_locked(&s, &[]);
                    }
                    let t0 = Instant::now();
                    while t0.elapsed() < Duration::from_secs(10) && p.power_cmd(PowerCmd::Status).await != "off" {
                        tokio::time::sleep(Duration::from_millis(200)).await;
                    }
                    {
                        let mut s = p.st.lock().unwrap();
                        s.held.remove(&(0x5d, 0x01));
                    }
                    if p.power_cmd(PowerCmd::Status).await == "off" {
                        return text(200, "off");
                    }
                    p.logline("subucom", "·  no standby answer from rbp in 10 s: cutting power");
                }
                st = p.power_cmd(PowerCmd::Off).await;
                p.leds_off();
                let _ = p.ctrl_tx.send(ConnMsg::Reset);
                let _ = p.key_tx.send(ConnMsg::Reset);
                p.logline("subucom", "·  POWER OFF");
                p.logline("console", "──────── power off ────────");
            }
            text(200, st)
        }
        "/ctrl" => {
            let ls = lines(&body);
            if ls.is_empty() {
                return text(400, "empty");
            }
            let mut s = p.st.lock().unwrap();
            let shots = s.apply(&ls);
            p.push_locked(&s, &shots);
            text(200, "ok")
        }
        "/release" => {
            let mut s = p.st.lock().unwrap();
            let names: Vec<Vec<String>> = lines(&body)
                .iter()
                .map(|l| l.split_whitespace().map(String::from).collect())
                .collect();
            if names.is_empty() {
                s.held.clear();
                for j in s.jog.iter_mut() {
                    j.touch = false;
                }
            }
            for parts in &names {
                if parts.len() == 3 && "Bb".contains(parts[0].as_str()) {
                    if let (Some(o), Some(m)) = (hexint(&parts[1]), hexint(&parts[2])) {
                        s.held.remove(&(o, m));
                    }
                }
            }
            p.push_locked(&s, &[]);
            text(200, "ok")
        }
        "/jog" => {
            let parts: Vec<&str> = body.split_whitespace().collect();
            let bad = || text(400, "use '<1|2> move <deg> <sec>' or '<1|2> touch <0|1>'");
            let d = match parts.first().and_then(|x| x.parse::<i64>().ok()) {
                Some(1) => 0,
                Some(2) => 1,
                _ => return bad(),
            };
            let mut s = p.st.lock().unwrap();
            match parts.get(1).copied() {
                Some("touch") => {
                    let Some(v) = parts.get(2) else { return bad() };
                    s.jog[d].touch = *v == "1";
                    s.jog[d].active = true;
                }
                Some("move") => {
                    let (Some(deg), Some(dt)) = (
                        parts.get(2).and_then(|x| x.parse::<f64>().ok()),
                        parts.get(3).and_then(|x| x.parse::<f64>().ok()),
                    ) else {
                        return bad();
                    };
                    let dt = dt.max(1e-3);
                    let ticks = (deg / 360.0 * JOG_TICKS_PER_REV).round_ties_even() as i64;
                    if ticks != 0 {
                        let speed = (deg.abs() / 360.0 / dt) / JOG_NORMAL_RPS;
                        let j = &mut s.jog[d];
                        j.cnt = (j.cnt + ticks) & 0xffff;
                        j.per = ((JOG_SPEED_K / speed.max(1e-3)).round_ties_even() as i64).clamp(1, 0xfffe);
                        j.rev = ticks < 0;
                        j.t = now_f();
                        j.active = true;
                    }
                }
                Some(_) => {} // unknown verb: server.py just re-pushes and answers ok
                None => return bad(),
            }
            p.push_locked(&s, &[]);
            text(200, "ok")
        }
        "/input" => {
            let b = body.trim().to_uppercase();
            let parts: Vec<&str> = b.split_whitespace().collect();
            if parts.len() == 2 && ["0", "1"].contains(&parts[0]) && ["DECK", "LINE"].contains(&parts[1]) {
                let mut s = p.st.lock().unwrap();
                s.input_src[if parts[0] == "0" { 0 } else { 1 }] = parts[1] == "DECK";
                p.push_locked(&s, &[]);
                text(200, "ok")
            } else {
                text(400, "use '<0|1> DECK|LINE'")
            }
        }
        "/mainkey" => {
            let g = body.trim();
            let digits = g.strip_prefix('-').unwrap_or(g);
            if !digits.is_empty() && digits.chars().all(|c| c.is_ascii_digit()) {
                let _ = p.key_tx.send(ConnMsg::Send(g.to_string()));
                text(200, "ok")
            } else {
                text(400, "bad gpio")
            }
        }
        "/mainkey_release" => {
            let _ = p.key_tx.send(ConnMsg::Send(String::new()));
            text(200, "ok")
        }
        "/browse" => {
            let t = body.trim();
            let n: i64 = match if t.is_empty() { Ok(0) } else { t.parse() } {
                Ok(v) => v,
                Err(_) => return text(400, "bad step"),
            };
            let n = n.clamp(-500, 500);
            if n != 0 {
                let p2 = p.clone();
                tokio::spawn(async move {
                    let step = if n > 0 { 1 } else { -1 };
                    p2.st.lock().unwrap().browse_active = true;
                    for _ in 0..n.abs() {
                        {
                            let mut s = p2.st.lock().unwrap();
                            s.browse_counter = (s.browse_counter + step) & 0xffff;
                            p2.push_locked(&s, &[]);
                        }
                        tokio::time::sleep(Duration::from_millis(45)).await;
                    }
                });
            }
            text(200, "ok")
        }
        "/usb" => {
            let b = body.trim();
            let (slot, img) = match b.split_once(' ') {
                Some((s, i)) => (s, i.trim()),
                None => (b, ""),
            };
            if !["usb1", "usb2"].contains(&slot) || img.is_empty() {
                return text(400, "usage: '<usb1|usb2> <path>'");
            }
            if !Path::new(img).exists() {
                return text(404, format!("no such path: {img}"));
            }
            if p.power_cmd(PowerCmd::Status).await == "off" {
                p.pending_usb.lock().unwrap().insert(slot.into(), img.into());
                return text(200, format!("{slot} queued: {img} (inserted after power on)"));
            }
            match mount_usb(&p, slot, img).await {
                Ok(m) => text(200, m),
                Err(e) => text(500, e),
            }
        }
        "/install-update" => {
            // File > Install Firmware Update: like the real procedure -- the .UPD on a USB
            // stick in USB1, power on in update mode. We build the stick (FAT16, XDJRX.UPD)
            // in the runtime dir, power-cycle into update mode and insert it when the
            // initramfs asks (insert_pending). The user finishes on screen and power-cycles,
            // as on the unit.
            let path = body.trim().to_string();
            if !Path::new(&path).is_file() {
                return text(404, format!("no such file: {path}"));
            }
            let stick = p.cfg.ctrl_sock.with_file_name("install-update.img");
            let st2 = stick.clone();
            let built = tokio::task::spawn_blocking(move || -> Result<String, String> {
                let upd = xdj_fw::firmware::read_upd_bytes(Path::new(&path))?;
                let (_, model, _) = xdj_fw::firmware::split_container(&upd)?;
                if !model.starts_with("XDJAERO") {
                    return Err(format!("{model:?} is not an XDJ-RX firmware (model must start with XDJAERO)"));
                }
                let img = xdj_fw::firmware::update_stick(&upd, SystemTime::now())?;
                std::fs::write(&st2, img).map_err(|e| format!("{}: {e}", st2.display()))?;
                Ok(model)
            })
            .await
            .unwrap_or_else(|e| Err(e.to_string()));
            let model = match built {
                Ok(m) => m,
                Err(e) => {
                    p.logline("subucom", &format!("·  install update: {e}"));
                    return text(400, e);
                }
            };
            if p.power_cmd(PowerCmd::Status).await.starts_with("on") {
                p.power_cmd(PowerCmd::Off).await;
                p.leds_off();
                let _ = p.ctrl_tx.send(ConnMsg::Reset);
                let _ = p.key_tx.send(ConnMsg::Reset);
            }
            p.pending_usb.lock().unwrap().insert("usb1".into(), stick.to_string_lossy().into_owned());
            let st = p.power_cmd(PowerCmd::On(true)).await;
            let ver = model.trim_start_matches("XDJAERO");
            p.logline("subucom", &format!("·  INSTALL FIRMWARE UPDATE {ver}: power on (firmware update mode), stick on USB1"));
            p.logline("console", &format!("──────── install firmware update {ver} (update mode) ────────"));
            tokio::spawn(insert_pending(p.clone(), true));
            text(200, format!("installing firmware {ver} ({st}): follow the update screen, then power off/on"))
        }
        "/savecal" => {
            let assets = p.cfg.panel_dir.join("assets");
            let ok = body.trim_start().starts_with('{') || body.trim_start().starts_with('[');
            if !ok {
                return text(400, "invalid JSON");
            }
            match std::fs::create_dir_all(&assets).and_then(|_| std::fs::write(assets.join("calibration.json"), &body)) {
                Ok(_) => text(200, "saved"),
                Err(e) => text(400, e.to_string()),
            }
        }
        _ => text(404, "not found"),
    }
}
