//! XDJ-RX emulator orchestrator (`xdj-host`).
//!
//! Supervises one emulator instance and wires three per-instance channels, out of
//! the global `/tmp` world:
//!
//!   * **QEMU** spawned headless (`-display none`) with, in a per-instance runtime
//!     dir: `-qmp unix:qmp.sock` (lifecycle), `-vnc unix:vnc.sock` (the host-side
//!     scanout surface -- the same surface cocoa/screendump draw, so zero guest
//!     changes), and the control chardevs `id=xdjctrl` / `id=xdjkey`.
//!   * **The virtual panel** (src/panel.rs, in-process; the Rust counterpart of
//!     tools/xdj-panel/server.py) serving panel.html, composing controls and driving
//!     `ctrl.sock`/`key.sock`.
//!   * **A WebSocket<->VNC bridge**: noVNC speaks RFB-over-WebSocket while QEMU
//!     `-vnc` is raw RFB over the unix socket, so we accept WS clients and pump bytes
//!     to/from `vnc.sock`: the screen is pushed with dirty-rects instead of polled as
//!     screendumps.
//!
//! The Tauri shell (app/shell) wraps it in a webview; run standalone, open the
//! printed URL in a browser to drive the emulator.
//!
//! Firmware is never touched -- everything here is host-side orchestration.

mod mcp;
mod panel;

use xdj_fw::{firmware, nand};

use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::{Duration, Instant};

use futures_util::{SinkExt, StreamExt};
use sha2::{Digest, Sha256};
use tokio::io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader};
use tokio::net::{TcpListener, UnixStream};
use tokio::process::{Child, Command};
use tokio_tungstenite::tungstenite::Message;

/// Resolved layout + knobs for one emulator instance.
struct Config {
    repo: PathBuf,
    runtime_dir: PathBuf,
    fw: std::sync::Arc<std::sync::RwLock<FwPaths>>, // the active firmware (switchable: MCP load_firmware)
    qemu: PathBuf,
    panel_dir: PathBuf,            // panel.html / logs.html / assets
    scripts_dir: PathBuf,          // bundled Resources/scripts (else the repo); searched for a dev .env
    novnc_dir: PathBuf,            // noVNC (served at /novnc/)
    knobs_dir: PathBuf,            // Knobs (audio-knobs) dist (served at /knobs/)
    qemu_data: Option<PathBuf>,    // bundled qemu data dir (keymaps); passed via -L when packaged
    panel_port: u16,
    vnc_ws_port: u16,
    smp: String,
    audio: String, // "none" disables host audio
}

/// The bundled resources dir, if we're running packaged: XDJ_RESOURCES, else next to
/// the executable when it looks like a bundle (holds qemu-system-arm / scripts/).
/// None in the dev tree, where paths resolve against the repo instead.
fn resources_root() -> Option<PathBuf> {
    if let Some(r) = std::env::var_os("XDJ_RESOURCES") {
        let p = PathBuf::from(r);
        if p.is_dir() {
            return Some(p);
        }
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            if dir.join("qemu-system-arm").exists() || dir.join("scripts").is_dir() {
                return Some(dir.to_path_buf());
            }
        }
    }
    None
}

/// The active firmware: its prepared dir and the unit's flash images inside it.
#[derive(Clone)]
pub struct FwPaths {
    pub fw_dir: PathBuf,
    pub nand: PathBuf,
    pub panelcom: PathBuf,
}

impl Config {
    fn fw_dir(&self) -> PathBuf {
        self.fw.read().unwrap().fw_dir.clone()
    }
    fn nand(&self) -> PathBuf {
        self.fw.read().unwrap().nand.clone()
    }
    fn panelcom(&self) -> PathBuf {
        self.fw.read().unwrap().panelcom.clone()
    }
    /// Switch to another prepared firmware dir (its own NAND unit + MCU flash).
    fn set_fw_dir(&self, dir: PathBuf) {
        let mut f = self.fw.write().unwrap();
        f.nand = dir.join(FW_NAND);
        f.panelcom = dir.join(FW_PANELCOM);
        f.fw_dir = dir;
    }
    fn from_env() -> std::io::Result<Config> {
        let repo = match std::env::var_os("XDJ_REPO") {
            Some(p) => PathBuf::from(p),
            None => std::env::current_dir()?,
        };
        let res = resources_root();
        // Resolve a resource: the bundle copy if present, else the dev repo path.
        let pick = |bundle_rel: &str, repo_rel: &str| -> PathBuf {
            if let Some(r) = &res {
                let p = r.join(bundle_rel);
                if p.exists() {
                    return p;
                }
            }
            repo.join(repo_rel)
        };
        // Per-instance sockets/console. Packaged, the repo isn't writable (the .app may
        // live in /Applications), so use app-data; dev uses build/run.
        let runtime_dir = std::env::var_os("XDJ_RUNTIME_DIR")
            .map(PathBuf::from)
            .unwrap_or_else(|| {
                if res.is_some() {
                    app_data_root().join("run")
                } else {
                    repo.join("build/run")
                }
            });
        // The prepared firmware + the unit's flash. The shell sets XDJ_FW_DIR to a
        // per-firmware app-data dir; the dev default is the build/qemu layout.
        let fw_dir = std::env::var_os("XDJ_FW_DIR")
            .map(PathBuf::from)
            .unwrap_or_else(|| repo.join("build/qemu"));
        let qemu = std::env::var_os("XDJ_QEMU")
            .map(PathBuf::from)
            .unwrap_or_else(|| {
                res.as_ref()
                    .map(|r| r.join("qemu-system-arm"))
                    .filter(|p| p.exists())
                    .unwrap_or_else(|| default_qemu(&repo))
            });
        let nand = std::env::var_os("XDJ_NAND")
            .map(PathBuf::from)
            .unwrap_or_else(|| fw_dir.join(FW_NAND));
        let panelcom = fw_dir.join(FW_PANELCOM);
        let panel_dir = pick("panel", "tools/xdj-panel");
        let scripts_dir = res
            .as_ref()
            .map(|r| r.join("scripts"))
            .filter(|p| p.is_dir())
            .unwrap_or_else(|| repo.clone());
        let novnc_dir = pick("ui/novnc", "app/ui/novnc");
        let knobs_dir = pick("ui/knobs", "app/ui/node_modules/audio-knobs/dist");
        let qemu_data = res.as_ref().map(|r| r.join("qemu-bios")).filter(|p| p.is_dir());
        let panel_port = env_u16("XDJ_PANEL_PORT", 8765);
        let vnc_ws_port = env_u16("XDJ_VNC_WS_PORT", 6080);
        let smp = std::env::var("XDJSMP").unwrap_or_else(|_| "2".into());
        let audio = pick_audio();
        Ok(Config {
            repo,
            runtime_dir,
            fw: std::sync::Arc::new(std::sync::RwLock::new(FwPaths { fw_dir, nand, panelcom })),
            qemu,
            panel_dir,
            scripts_dir,
            novnc_dir,
            knobs_dir,
            qemu_data,
            panel_port,
            vnc_ws_port,
            smp,
            audio,
        })
    }

    fn rt(&self, name: &str) -> PathBuf {
        self.runtime_dir.join(name)
    }



    /// Is firmware installed on the unit's NAND? `provisioned.stamp` caches the answer;
    /// without it, ask the NAND itself, as the real unit's u-boot would: the ALL update's
    /// last step (the updater's finEnv) writes `release=` into the u-boot env, which the
    /// factory env (fw_env.txt) lacks. That catches a provisioning whose console success
    /// line never came: the updater sits on "Firmware update is complete. Turn off the
    /// power" until the user does, and the power cut ends the init script before
    /// it prints "### USB update success.".
    fn provisioned(&self) -> bool {
        let stamp = self.fw_dir().join(FW_PROVISIONED);
        if stamp.exists() {
            return true;
        }
        let installed = nand::installed(&self.nand());
        if installed {
            eprintln!("[host] NAND holds an installed firmware (env has release=); marking provisioned");
            let _ = std::fs::write(&stamp, b"");
        }
        installed
    }

}

// ---- The prepared firmware dir (app-data/<hash>/) ----
// prep writes the first four; the host creates the rest when it provisions/boots.
const FW_ISO: &str = "fw.iso";                 // the decrypted firmware (fw_env.txt for init)
const FW_STICK: &str = "update-stick.img";     // FAT16 stick with XDJRX.UPD (provisioning)
// + vmlinux.bin / tramp.bin / atags.bin: the firmware's own kernel, host-loaded to
//   provision (update mode needs no SD card and no installed kernel)
const FW_NAND: &str = "nand-flash.img";        // the unit's NAND (XDJ_NAND_IMG)
const FW_PANELCOM: &str = "panelcom-flash.bin"; // the panel MCU flash (XDJ_PANELCOM_IMG)
const FW_PROVISIONED: &str = "provisioned.stamp"; // the ALL update completed
const FW_NAND_BOOT: &str = "nand-boot";        // per-power-on blobs from the NAND kernel

fn default_qemu(repo: &Path) -> PathBuf {
    // Prefer the patched build; fall back to PATH's qemu-system-arm.
    let built = repo.join("build/qemu-src/qemu-11.1.0/build/qemu-system-arm");
    if built.exists() {
        built
    } else {
        PathBuf::from("qemu-system-arm")
    }
}

/// Pick QEMU's `-audio` backend by OS, keeping `timer-period=1451` (without it the
/// guest ALSA ring starves and audio is slow/silent — see qemu/watch.sh). On Linux,
/// prefer whatever's actually running (pipewire → pulse → alsa). XDJAUDIO overrides
/// the whole value (incl. `none` to disable host audio).
fn pick_audio() -> String {
    if let Ok(a) = std::env::var("XDJAUDIO") {
        return a;
    }
    let backend = if cfg!(target_os = "macos") {
        "coreaudio"
    } else if cfg!(target_os = "windows") {
        "wasapi"
    } else if runtime_sock_exists("pipewire-0") {
        "pipewire"
    } else if runtime_sock_exists("pulse/native") {
        "pa"
    } else {
        "alsa"
    };
    format!("{backend},timer-period=1451")
}

/// True if $XDG_RUNTIME_DIR/<rel> exists (a running audio daemon's socket).
fn runtime_sock_exists(rel: &str) -> bool {
    std::env::var_os("XDG_RUNTIME_DIR")
        .map(|d| Path::new(&d).join(rel).exists())
        .unwrap_or(false)
}

fn env_u16(key: &str, default: u16) -> u16 {
    std::env::var(key)
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(default)
}

/// The backend behind the unit's Ethernet port (the i.MX6 FEC, guest `eth0`).
#[derive(Debug, Clone, PartialEq)]
enum NetBackend {
    /// QEMU user-mode NAT: always available, but the unit sits on a private 10.0.2.0/24
    /// behind the host and is invisible to the LAN.
    User,
    /// A socket_vmnet daemon's unix socket: bridged mode puts the unit on the host's LAN,
    /// shared mode on a network with the Mac only (192.168.105.0/24); either way it
    /// gets its own DHCP lease and sees broadcasts. The root daemon owns the vmnet interface, so
    /// QEMU itself runs as the user (README "Networking").
    Vmnet(PathBuf),
}

impl NetBackend {
    fn qemu_args(&self) -> Vec<String> {
        match self {
            NetBackend::User => vec!["-nic".into(), "user".into()],
            // `-net nic` binds the board's on-board FEC to the netdev. reconnect-ms keeps
            // the link retrying if the daemon restarts while the unit runs.
            NetBackend::Vmnet(p) => vec![
                "-netdev".into(),
                format!(
                    "stream,id=xdjnet,server=off,reconnect-ms=1000,addr.type=unix,addr.path={}",
                    p.display()
                ),
                "-net".into(), "nic,netdev=xdjnet".into(),
            ],
        }
    }
}

/// Where socket_vmnet keeps its sockets: the upstream launchd install, then Homebrew's.
const VMNET_SOCK_DIRS: &[&str] = &["/var/run", "/opt/homebrew/var/run", "/usr/local/var/run"];

/// Pick eth0's backend for this power-on from `XDJ_NET`:
/// - unset / `auto`: a bridged socket_vmnet socket if one is installed (preferring the
///   default-route interface), else the Mac-only shared socket, else user NAT;
/// - `user`: user NAT;
/// - an absolute path: that socket_vmnet socket.
fn net_backend() -> NetBackend {
    let want = std::env::var("XDJ_NET").unwrap_or_default();
    match want.as_str() {
        "user" => return NetBackend::User,
        "" | "auto" => {}
        p if p.starts_with('/') => return NetBackend::Vmnet(PathBuf::from(p)),
        other => eprintln!("[host] XDJ_NET={other:?} not understood (auto|user|/path/to/socket); using auto"),
    }
    match find_vmnet_bridge(default_route_ifname().as_deref()).or_else(find_vmnet_shared) {
        Some(p) => NetBackend::Vmnet(p),
        None => NetBackend::User,
    }
}

/// A bridged socket_vmnet socket (`socket_vmnet.bridged.<ifname>`): the one for `pref`
/// if present, else the first found.
fn find_vmnet_bridge(pref: Option<&str>) -> Option<PathBuf> {
    let is_sock = |p: &Path| {
        use std::os::unix::fs::FileTypeExt;
        std::fs::metadata(p).map(|m| m.file_type().is_socket()).unwrap_or(false)
    };
    if let Some(ifn) = pref {
        for d in VMNET_SOCK_DIRS {
            let p = Path::new(d).join(format!("socket_vmnet.bridged.{ifn}"));
            if is_sock(&p) {
                return Some(p);
            }
        }
    }
    for d in VMNET_SOCK_DIRS {
        let Ok(rd) = std::fs::read_dir(d) else { continue };
        let mut found: Vec<PathBuf> = rd
            .flatten()
            .map(|e| e.path())
            .filter(|p| {
                p.file_name()
                    .and_then(|n| n.to_str())
                    .is_some_and(|n| n.starts_with("socket_vmnet.bridged."))
                    && is_sock(p)
            })
            .collect();
        found.sort();
        if let Some(p) = found.into_iter().next() {
            return Some(p);
        }
    }
    None
}

/// The Mac-only (vmnet shared mode) socket_vmnet socket, `socket_vmnet`.
fn find_vmnet_shared() -> Option<PathBuf> {
    use std::os::unix::fs::FileTypeExt;
    VMNET_SOCK_DIRS.iter().map(|d| Path::new(d).join("socket_vmnet")).find(|p| {
        std::fs::metadata(p).map(|m| m.file_type().is_socket()).unwrap_or(false)
    })
}

/// The host interface carrying the default route (macOS `route`), e.g. `en0`.
fn default_route_ifname() -> Option<String> {
    let out = std::process::Command::new("/sbin/route")
        .args(["-n", "get", "default"])
        .output()
        .ok()?;
    String::from_utf8_lossy(&out.stdout)
        .lines()
        .find_map(|l| l.trim().strip_prefix("interface:").map(|s| s.trim().to_string()))
}

fn qemu_args(cfg: &Config, boot_dir: &Path) -> Vec<String> {
    let q = boot_dir.to_path_buf();
    let s = |p: PathBuf| p.to_string_lossy().into_owned();
    let mut a: Vec<String> = vec![
        "-M".into(), "sabrelite".into(),
        "-smp".into(), cfg.smp.clone(),
        "-m".into(), "256".into(),
        "-no-reboot".into(),
        "-name".into(), "Pioneer XDJ-RX".into(),
        // Headless: the screen reaches the UI via VNC, not a native window.
        "-display".into(), "none".into(),
    ];
    // Packaged: qemu is relocated away from its build tree, so point it at the bundled
    // data dir (keymaps) — else `-vnc` fails with "could not find keymap file".
    if let Some(d) = &cfg.qemu_data {
        a.push("-L".into());
        a.push(d.to_string_lossy().into_owned());
    }
    if cfg.audio != "none" {
        a.push("-audio".into());
        a.push(cfg.audio.clone());
    }
    let net = net_backend();
    match &net {
        NetBackend::User => eprintln!(
            "[host] network: user NAT (unit not visible on the LAN; optional LAN bridge: tools/net/install-vmnet-bridge.sh)"
        ),
        NetBackend::Vmnet(p) => eprintln!("[host] network: vmnet via {}", p.display()),
    }
    a.extend(net.qemu_args());
    a.extend([
        "-device".into(), format!("loader,file={},addr=0x10008000,force-raw=on", s(q.join("vmlinux.bin"))),
        "-device".into(), format!("loader,file={},addr=0x10000100,force-raw=on", s(q.join("atags.bin"))),
        "-device".into(), format!("loader,file={},addr=0x10000000,cpu-num=0", s(q.join("tramp.bin"))),
        // No SD card: like the real unit, everything lives in NAND (XDJ_NAND_IMG).
        // USB-2.0 hub on HOST1 (guest bus 2 = usb-bus.1), hub-only boot: the front
        // slots live behind this hub at devpaths 2-1.1 (USB2) / 2-1.2 (USB1). Storage
        // is inserted AFTER rbp is polling (see maybe_insert_usb) — cold-plugging at
        // boot hits a mount-rendezvous race ("USB Error"). See docs/usb.md.
        "-device".into(), "usb-hub,bus=usb-bus.1,port=1,id=xdjhub".into(),
        // Control channels (per-instance).
        "-chardev".into(), format!("socket,id=xdjctrl,path={},server=on,wait=off", s(cfg.rt("ctrl.sock"))),
        "-chardev".into(), format!("socket,id=xdjkey,path={},server=on,wait=off", s(cfg.rt("key.sock"))),
        // Lifecycle + screen, per-instance.
        "-qmp".into(), format!("unix:{},server=on,wait=off", s(cfg.rt("qmp.sock"))),
        "-vnc".into(), format!("unix:{}", s(cfg.rt("vnc.sock"))),
        // Console to a log file; second UART unused.
        "-serial".into(), format!("file:{}", s(cfg.rt("console.log"))),
        "-serial".into(), "null".into(),
    ]);
    a
}

/// stdout+stderr redirected to a log file in the runtime dir (so a child's errors are
/// captured even when the app is launched from Finder). Falls back to inherited stdio.
fn log_stdio(cfg: &Config, name: &str) -> (Stdio, Stdio) {
    if let Ok(f) = std::fs::File::create(cfg.rt(name)) {
        if let Ok(f2) = f.try_clone() {
            return (Stdio::from(f), Stdio::from(f2));
        }
    }
    (Stdio::inherit(), Stdio::inherit())
}

/// What a power-on boots. The unit always boots from NAND (`XDJ_BOOT_MEDIA=nand`, the
/// SBMR1 straps); a blank unit is first provisioned with the firmware's own ALL update.
#[derive(Clone, Copy, PartialEq, Debug)]
enum BootKind {
    /// Blank unit: erase the NAND (+ seed the factory u-boot env), boot the firmware's
    /// kernel host-loaded in update mode with the MCU reporting ALL, insert the update
    /// stick -> the updater installs u-boot/kernel/rootfs/data/gui/PANELCOM + its env.
    Provision,
    /// Normal boot of what is installed (u-boot's bootcmd_nand, done host-side).
    Normal,
    /// Firmware-update mode on an installed unit (File > Enter Firmware Update Mode).
    Update,
}

/// Prepare the flash + boot blobs for `kind` (blocking; well under a second).
fn prepare_boot(cfg: &Config, kind: BootKind) -> Result<PathBuf, String> {
    match kind {
        BootKind::Provision => {
            // A factory-fresh unit: erased NAND with u-boot's default env, erased MCU flash.
            let _ = std::fs::remove_file(cfg.fw_dir().join(FW_PROVISIONED));
            let _ = std::fs::remove_file(&cfg.panelcom());
            let iso = std::fs::read(cfg.fw_dir().join(FW_ISO)).map_err(|e| format!("{FW_ISO}: {e}"))?;
            let fw_env = firmware::env_lines(&firmware::iso_read(&iso, "/images/fw_env.txt")?);
            nand::init(&cfg.nand(), &fw_env)?;
            eprintln!("[host] NAND erased, factory env ({} vars) written", fw_env.len());
            Ok(cfg.fw_dir().clone())
        }
        BootKind::Normal | BootKind::Update => {
            // Every power-on re-reads kernel_bank + that bank's uImage, so a firmware
            // update done inside the emulator boots the new kernel, as on the unit.
            let dir = cfg.fw_dir().join(FW_NAND_BOOT);
            nand::boot(&cfg.nand(), &dir)?;
            Ok(dir)
        }
    }
}

/// Start QEMU for `kind` (see BootKind). Update/Provision hold the sub-ucom's
/// update-request line GPIO3_IO30 HIGH from reset (xdj-board.c).
fn spawn_qemu(cfg: &Config, kind: BootKind) -> Result<Child, String> {
    let boot_dir = prepare_boot(cfg, kind)?;
    let args = qemu_args(cfg, &boot_dir);
    eprintln!("[host] qemu ({kind:?}): {} {}", cfg.qemu.display(), args.join(" "));
    // A fresh console.log per power-on (the sidecar's System log and the USB-insert
    // wait both read it), and no stale sockets from the previous run.
    for s in ["qmp.sock", "vnc.sock", "ctrl.sock", "key.sock", "console.log"] {
        let _ = std::fs::remove_file(cfg.rt(s));
    }
    let (out, err) = log_stdio(cfg, "qemu.log");
    let mut c = Command::new(&cfg.qemu);
    c.args(&args).current_dir(&cfg.repo).stdout(out).stderr(err).kill_on_drop(true);
    c.env("XDJ_BOOT_MEDIA", "nand")
        .env("XDJ_NAND_IMG", &cfg.nand())
        .env("XDJ_PANELCOM_IMG", &cfg.panelcom())
        .env_remove("XDJ_BOOT_UPDATE")
        .env_remove("XDJ_UPDATE_TYPE");
    match kind {
        BootKind::Provision => {
            // 0x20 = ALL: the factory install (writes u-boot and creates the data volume;
            // a user update 0x10 would leave rbp hanging on the splash).
            c.env("XDJ_BOOT_UPDATE", "1").env("XDJ_UPDATE_TYPE", "20");
        }
        BootKind::Update => {
            c.env("XDJ_BOOT_UPDATE", "1");
        }
        BootKind::Normal => {}
    }
    c.spawn().map_err(|e| format!("spawn {}: {e}", cfg.qemu.display()))
}

/// Power control channel: a line protocol on `<runtime>/power.sock` used by the panel
/// sidecar (POST /power). Commands: `on`, `on update`, `off`, `status`; each answers
/// with one line `on` / `on update` / `off`.
pub(crate) enum PowerCmd {
    On(bool),
    Off,
    Status,
    /// Prepare a .UPD/.zip, make it the active firmware and power it on (MCP load_firmware).
    LoadFirmware(PathBuf),
}

async fn power_listener(path: PathBuf, tx: tokio::sync::mpsc::Sender<(PowerCmd, tokio::sync::oneshot::Sender<String>)>) {
    let _ = std::fs::remove_file(&path);
    let listener = match tokio::net::UnixListener::bind(&path) {
        Ok(l) => l,
        Err(e) => {
            eprintln!("[host] power socket {}: {e}", path.display());
            return;
        }
    };
    loop {
        let Ok((stream, _)) = listener.accept().await else { continue };
        let tx = tx.clone();
        tokio::spawn(async move {
            let (rd, mut wr) = stream.into_split();
            let mut lines = BufReader::new(rd).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                let w: Vec<&str> = line.split_whitespace().collect();
                let cmd = match w.first().copied() {
                    Some("on") => PowerCmd::On(w.get(1) == Some(&"update")),
                    Some("off") => PowerCmd::Off,
                    _ => PowerCmd::Status,
                };
                let (rtx, rrx) = tokio::sync::oneshot::channel();
                if tx.send((cmd, rtx)).await.is_err() {
                    return;
                }
                let reply = rrx.await.unwrap_or_else(|_| "off".into());
                if wr.write_all(format!("{reply}\n").as_bytes()).await.is_err() {
                    return;
                }
            }
        });
    }
}

/// Wait for a child that may not exist (powered off = pending forever).
async fn wait_opt(c: &mut Option<Child>) -> std::io::Result<std::process::ExitStatus> {
    match c {
        Some(ch) => ch.wait().await,
        None => std::future::pending().await,
    }
}

/// Accept WS clients and bridge each to QEMU's VNC unix socket (byte-transparent:
/// noVNC and QEMU negotiate RFB end-to-end through us).
async fn run_vnc_ws_bridge(cfg_vnc_sock: PathBuf, ws_port: u16) {
    let listener = match TcpListener::bind(("127.0.0.1", ws_port)).await {
        Ok(l) => l,
        Err(e) => {
            eprintln!("[host] VNC-WS bridge: cannot bind :{ws_port}: {e}");
            return;
        }
    };
    eprintln!("[host] VNC-WS bridge: ws://127.0.0.1:{ws_port}/  ->  {}", cfg_vnc_sock.display());
    loop {
        let (tcp, peer) = match listener.accept().await {
            Ok(x) => x,
            Err(e) => {
                eprintln!("[host] VNC-WS accept error: {e}");
                continue;
            }
        };
        let vnc_sock = cfg_vnc_sock.clone();
        tokio::spawn(async move {
            if let Err(e) = bridge_one(tcp, &vnc_sock).await {
                eprintln!("[host] VNC-WS client {peer} ended: {e}");
            }
        });
    }
}

async fn bridge_one(tcp: tokio::net::TcpStream, vnc_sock: &Path) -> std::io::Result<()> {
    let ws = tokio_tungstenite::accept_async(tcp)
        .await
        .map_err(|e| std::io::Error::new(std::io::ErrorKind::Other, e))?;
    let unix = UnixStream::connect(vnc_sock).await?;
    let (mut ws_tx, mut ws_rx) = ws.split();
    let (mut ux_rd, mut ux_wr) = unix.into_split();

    // WS (noVNC) -> VNC socket.
    let to_vnc = async move {
        while let Some(msg) = ws_rx.next().await {
            match msg {
                Ok(Message::Binary(b)) => {
                    if ux_wr.write_all(&b).await.is_err() {
                        break;
                    }
                }
                Ok(Message::Close(_)) | Err(_) => break,
                _ => {} // ignore text/ping/pong; browsers send binary for RFB
            }
        }
        let _ = ux_wr.shutdown().await;
    };

    // VNC socket -> WS (noVNC).
    let to_ws = async move {
        let mut buf = vec![0u8; 64 * 1024];
        loop {
            match ux_rd.read(&mut buf).await {
                Ok(0) => break,
                Ok(n) => {
                    if ws_tx.send(Message::Binary(buf[..n].to_vec())).await.is_err() {
                        break;
                    }
                }
                Err(_) => break,
            }
        }
        let _ = ws_tx.close().await;
    };

    tokio::join!(to_vnc, to_ws);
    Ok(())
}

/// Resolves when the process is asked to terminate (SIGTERM). On non-unix it never
/// resolves (Ctrl-C still covers interactive use). The shell sends us SIGTERM on
/// exit so we get a chance to tear QEMU + the sidecar down instead of orphaning them.
#[cfg(unix)]
async fn terminated() {
    use tokio::signal::unix::{signal, SignalKind};
    if let Ok(mut s) = signal(SignalKind::terminate()) {
        s.recv().await;
    } else {
        std::future::pending::<()>().await;
    }
}
#[cfg(not(unix))]
async fn terminated() {
    std::future::pending::<()>().await;
}

/// Resolves when our parent process (the Tauri shell, which passes XDJ_PARENT_PID)
/// is no longer alive — so we tear QEMU down however the app was killed (window
/// close, force-quit, crash), not only on a clean SIGTERM. Never resolves if the
/// var is unset (standalone/dev use), so `nohup xdj-host` isn't affected.
#[cfg(unix)]
async fn parent_gone() {
    let pid: i32 = match std::env::var("XDJ_PARENT_PID").ok().and_then(|v| v.parse().ok()) {
        Some(p) => p,
        None => return std::future::pending::<()>().await,
    };
    loop {
        // kill(pid, 0) == 0 while the parent is alive; -1/ESRCH once it's gone.
        if unsafe { libc::kill(pid, 0) } != 0 {
            return;
        }
        tokio::time::sleep(Duration::from_secs(1)).await;
    }
}
#[cfg(not(unix))]
async fn parent_gone() {
    std::future::pending::<()>().await;
}

fn clean_stale(cfg: &Config) {
    for s in ["qmp.sock", "vnc.sock", "ctrl.sock", "key.sock", "power.sock"] {
        let _ = std::fs::remove_file(cfg.rt(s));
    }
    for slot in ["usb1", "usb2"] {
        let _ = std::fs::remove_file(usb_link(slot));
    }
    // The xdj-panel.c USB fault-flag hooks are still global /tmp paths (not yet
    // per-instance) — clear them so a stale fault doesn't carry across runs.
    for f in ["/tmp/usb_insert", "/tmp/usb2_insert"] {
        let _ = std::fs::remove_file(f);
    }
}

fn preflight(cfg: &Config) -> Result<(), String> {
    if !cfg.qemu.exists() && cfg.qemu.is_absolute() {
        return Err(format!("qemu not found: {} (build it with qemu/build-qemu.sh, or set XDJ_QEMU)", cfg.qemu.display()));
    }
    // Provisioning needs the firmware's kernel blobs, the ISO and the update stick; an
    // installed unit boots from its NAND alone.
    if !cfg.provisioned() {
        for f in [FW_ISO, FW_STICK] {
            let p = cfg.fw_dir().join(f);
            if !p.exists() {
                return Err(format!(
                    "{} missing (prepare a firmware first: `xdj-host prep <firmware.UPD|.zip>`)",
                    p.display()
                ));
            }
        }
    }
    for b in ["vmlinux.bin", "tramp.bin", "atags.bin"] {
        let p = cfg.fw_dir().join(b);
        if !p.exists() {
            return Err(format!(
                "boot blob missing: {} (prepare a firmware first: `xdj-host prep <firmware.UPD|.zip>`, or `xdj prepare`)",
                p.display()
            ));
        }
    }
    if !cfg.panel_dir.join("panel.html").exists() {
        return Err(format!("panel page missing: {}", cfg.panel_dir.join("panel.html").display()));
    }
    Ok(())
}

// ---- firmware preparation (drag-drop a .UPD -> cached boot artifacts) ----

/// App-data root where prepared firmwares are cached, keyed by firmware hash.
/// macOS: ~/Library/Application Support/XDJ-RX ; else $XDG_DATA_HOME/xdj-rx (or
/// ~/.local/share/xdj-rx). Never ships firmware — it's generated from the user's .UPD.
fn app_data_root() -> PathBuf {
    let home = std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."));
    if cfg!(target_os = "macos") {
        home.join("Library/Application Support/XDJ-RX")
    } else {
        std::env::var_os("XDG_DATA_HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|| home.join(".local/share"))
            .join("xdj-rx")
    }
}

/// Where the user-supplied firmware key lives (the shell's key prompt writes it here).
/// Never bundled. Always the file `aes256.key`, whose content is sniffed: a 64-hex key
/// goes via XDJRX_AES_KEY, anything else is treated as a passphrase file (--key-file).
fn firmware_key_dir() -> PathBuf {
    app_data_root().join("key")
}


/// Resolve the firmware decryption key for the decrypt step. Prefers the app-data key;
/// falls back to $XDJRX_AES_KEY or a dev .env; errors only if no key is available
/// anywhere.
fn resolve_firmware_key(cfg: &Config) -> Result<[u8; 32], String> {
    let bad = |p: &Path| format!("{}: not a usable key (64 hex chars, or a passphrase file)", p.display());
    let kf = firmware_key_dir().join("aes256.key");
    if kf.exists() {
        let raw = std::fs::read(&kf).map_err(|e| format!("{}: {e}", kf.display()))?;
        return Ok(firmware::key_from_hex(&String::from_utf8_lossy(&raw))
            .unwrap_or_else(|| firmware::key_from_passphrase(&raw)));
    }
    // dev: XDJRX_AES_KEY in the environment or a repo .env (as the `xdj` CLI reads it)
    if let Ok(v) = std::env::var("XDJRX_AES_KEY") {
        return firmware::key_from_hex(&v).ok_or_else(|| bad(Path::new("$XDJRX_AES_KEY")));
    }
    for envf in [cfg.repo.join(".env"), cfg.scripts_dir.join(".env")] {
        if let Ok(t) = std::fs::read_to_string(&envf) {
            for l in t.lines() {
                if let Some((k, v)) = l.trim().split_once('=') {
                    if k.trim() == "XDJRX_AES_KEY" {
                        let v = v.trim().trim_matches('"').trim_matches('\'');
                        return firmware::key_from_hex(v).ok_or_else(|| bad(&envf));
                    }
                }
            }
        }
    }
    Err("firmware decryption key required — add your aes256.key in the app first".into())
}

/// First 16 hex chars of the firmware file's SHA-256 — the cache key.
fn firmware_hash(path: &Path) -> std::io::Result<String> {
    use std::io::Read;
    let mut f = std::fs::File::open(path)?;
    let mut hasher = Sha256::new();
    let mut buf = [0u8; 1 << 16];
    loop {
        let n = f.read(&mut buf)?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
    }
    Ok(hasher.finalize().iter().take(8).map(|b| format!("{b:02x}")).collect())
}



/// Marks a firmware dir prepared by the current prep pipeline. Bump it when the
/// prepared output changes so caches from an older build are rebuilt (keep in sync
/// with the shell's `fw_complete`). v3: NAND unit -- an update stick instead of the
/// SD image; the host provisions the NAND with the firmware's own ALL update.
const PREP_STAMP: &str = "prep-v3.stamp";

/// Prepare (or reuse a cached) firmware: decrypt -> boot blobs -> update stick into
/// app-data/<hash>/. Idempotent — a complete cache dir is reused instantly. Returns
/// the prepared dir (pass it back as XDJ_FW_DIR to run).
fn prep_firmware(cfg: &Config, upd: &Path) -> Result<PathBuf, String> {
    let upd = upd
        .canonicalize()
        .map_err(|e| format!("firmware not found: {} ({e})", upd.display()))?;
    let hash = firmware_hash(&upd).map_err(|e| format!("cannot hash firmware: {e}"))?;
    let dir = app_data_root().join(&hash);
    let needed = ["vmlinux.bin", "tramp.bin", "atags.bin", FW_ISO, FW_STICK, PREP_STAMP];
    if needed.iter().all(|f| dir.join(f).exists()) {
        eprintln!("[prep] cache hit: {}", dir.display());
        return Ok(dir);
    }
    std::fs::create_dir_all(&dir).map_err(|e| format!("cannot create {}: {e}", dir.display()))?;

    // 1. decrypt the .UPD/.zip -> ISO. The AES key is USER-SUPPLIED, never bundled
    // (§5bis-A): the key saved in app-data (the shell's key prompt stores it there),
    // else the dev .env. No key anywhere -> a clear error the shell turns into "add
    // your firmware key".
    let key = resolve_firmware_key(cfg)?;
    eprintln!("[prep] decrypt firmware…");
    let (iso, model) = firmware::decrypt_firmware(&upd, &key)?;
    let w = |n: &str, b: &[u8]| std::fs::write(dir.join(n), b).map_err(|e| format!("{n}: {e}"));
    w(FW_ISO, &iso)?;
    eprintln!("[prep] model {model:?}: {} bytes, ISO9660 verified", iso.len());
    // 2. the firmware's kernel + SABRE-SD boot blobs (what provisioning boots)
    eprintln!("[prep] build boot blobs…");
    firmware::write_boot_blobs(&iso, &dir)?;
    // 3. the USB stick the provisioning update boots from (XDJRX.UPD on FAT16)
    eprintln!("[prep] build update stick…");
    let upd_bytes = firmware::read_upd_bytes(&upd)?;
    w(FW_STICK, &firmware::update_stick(&upd_bytes, std::time::SystemTime::now())?)?;
    std::fs::write(dir.join(PREP_STAMP), b"")
        .map_err(|e| format!("cannot write {}: {e}", dir.join(PREP_STAMP).display()))?;
    eprintln!("[prep] done: {}", dir.display());
    Ok(dir)
}

// ---- mount a USB (disk image OR physical device) after boot ----

/// Minimal JSON string literal (for the QMP human-monitor-command command-line).
fn json_str(s: &str) -> String {
    let mut out = String::from("\"");
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            _ => out.push(c),
        }
    }
    out.push('"');
    out
}

/// Connect to the QMP socket, do the capabilities handshake, then run each HMP
/// command via `human-monitor-command` (the proven drive_add/device_add path).
/// Returns each command's HMP output text. Loosely parsed — no serde.
pub(crate) async fn qmp_hmp(sock: &Path, cmds: &[String]) -> Result<Vec<String>, String> {
    let stream = UnixStream::connect(sock)
        .await
        .map_err(|e| format!("QMP connect {}: {e}", sock.display()))?;
    let (rd, mut wr) = stream.into_split();
    let mut lines = BufReader::new(rd).lines();

    // Read lines until a QMP response ("return"/"error"); skip the greeting + events.
    async fn response(
        lines: &mut tokio::io::Lines<BufReader<tokio::net::unix::OwnedReadHalf>>,
    ) -> Result<String, String> {
        loop {
            match lines.next_line().await.map_err(|e| e.to_string())? {
                Some(l) if l.contains("\"return\"") => return Ok(l),
                Some(l) if l.contains("\"error\"") => return Err(l),
                Some(_) => continue, // greeting / async event
                None => return Err("QMP connection closed".into()),
            }
        }
    }

    wr.write_all(b"{\"execute\":\"qmp_capabilities\"}\n")
        .await
        .map_err(|e| e.to_string())?;
    response(&mut lines).await?;

    let mut outs = Vec::new();
    for c in cmds {
        let req = format!(
            "{{\"execute\":\"human-monitor-command\",\"arguments\":{{\"command-line\":{}}}}}\n",
            json_str(c)
        );
        wr.write_all(req.as_bytes()).await.map_err(|e| e.to_string())?;
        outs.push(response(&mut lines).await?);
    }
    Ok(outs)
}

/// Wait until the guest console shows `marker` (our proxy for "rbp is up and
/// polling"), or the timeout elapses.
async fn wait_console_marker(path: &Path, marker: &str, timeout: Duration) -> bool {
    let start = Instant::now();
    while start.elapsed() < timeout {
        if let Ok(s) = std::fs::read_to_string(path) {
            if s.contains(marker) {
                return true;
            }
        }
        tokio::time::sleep(Duration::from_millis(500)).await;
    }
    false
}

/// Make a USB backing ready for a read/write attach (every slot is read/write, like a
/// real stick: MASTER REC, rekordbox exports and the updater write to it).
/// - A host device (`/dev/…`) is unmounted on the host, so the guest is its only writer.
///   macOS makes a removable disk's node the console user's, so no root is needed.
/// - An image that macOS has attached (opened in Finder) is ejected there first: its
///   disk-image service holds the file, and QEMU needs it exclusively to write.
/// - Either must open read/write.
pub(crate) fn prepare_usb_backing(src: &str) -> Result<(), String> {
    let path = Path::new(src);
    std::fs::metadata(path).map_err(|e| format!("{src}: {e}"))?;
    if src.starts_with("/dev/") {
        #[cfg(target_os = "macos")]
        let st = std::process::Command::new("diskutil").arg("unmountDisk").arg(src).status();
        #[cfg(not(target_os = "macos"))]
        let st = std::process::Command::new("umount").arg(src).status();
        // Linux umount fails on an unmounted device; diskutil succeeds then. Only a
        // failed diskutil means a volume is still in use on the host.
        if cfg!(target_os = "macos") && !st.map(|s| s.success()).unwrap_or(false) {
            return Err(format!("could not unmount {src} on the host (a volume on it is in use?)"));
        }
    } else if let Some(disk) = attached_image_disk(path) {
        let ok = std::process::Command::new("diskutil")
            .args(["eject", &disk])
            .status()
            .map(|s| s.success())
            .unwrap_or(false);
        if !ok {
            return Err(format!(
                "{src} is open in macOS ({disk}) and could not be ejected; eject it in Finder and load it again"
            ));
        }
    }
    std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(path)
        .map(drop)
        .map_err(|e| format!("{src} is not writable: {e}"))
}

/// The whole-disk node (`/dev/diskN`) of a macOS-attached disk image backed by `img`.
fn attached_image_disk(img: &Path) -> Option<String> {
    if !cfg!(target_os = "macos") {
        return None;
    }
    let want = std::fs::canonicalize(img).ok()?;
    let out = std::process::Command::new("hdiutil").arg("info").output().ok()?;
    let text = String::from_utf8_lossy(&out.stdout);
    text.split("\n====").find_map(|block| {
        let path = block.lines().find_map(|l| {
            let (k, v) = l.split_once(':')?;
            (k.trim() == "image-path").then(|| v.trim().to_string())
        })?;
        if std::fs::canonicalize(&path).ok()? != want {
            return None;
        }
        block.lines().find_map(|l| {
            let d = l.split_whitespace().next()?;
            let n = d.strip_prefix("/dev/disk")?;
            n.chars().all(|c| c.is_ascii_digit()).then(|| d.to_string())
        })
    })
}

/// If XDJ_USB is set, mount it into the guest AFTER rbp is polling, reproducing the
/// hardware insert choreography (hub-only boot -> post-boot device_add; no GPIO —
/// the xdj-panel.c lines are USB fault flags, see the note at the insert below).
///   XDJ_USB=<image.img|/dev/diskN>   the backing: a disk image, or a host block device
///   XDJ_USB_SLOT=usb1|usb2           front slot (default usb2 = hub port 1.1)
/// Attached read/write; a physical device is unmounted on the host first. See docs/usb.md.
async fn maybe_insert_usb(runtime_dir: PathBuf) {
    let Some(src) = std::env::var_os("XDJ_USB") else {
        return;
    };
    let src = src.to_string_lossy().into_owned();
    let slot = std::env::var("XDJ_USB_SLOT").unwrap_or_else(|_| "usb2".into());
    let rt = |n: &str| runtime_dir.join(n);

    // Insert only once rbp's UsbMountManager is polling, or an early insert races to
    // "USB Error" (unrecoverable without reboot). rbp's audio xrun-recovery ("Resolve
    // xrun") only prints once rbp is fully up and running — a reliable late signal
    // (generic "ALSA" matches early kernel codec init, far too soon). Then settle.
    if !wait_console_marker(&rt("console.log"), "Resolve xrun", Duration::from_secs(240)).await {
        eprintln!("[usb] gave up waiting for boot; not inserting");
        return;
    }
    // Settle so rbp's UsbMountManager is firmly in its /proc/udev_usb2 poll before the
    // device enumerates — else the udev "mount" rendezvous is lost and rbp shows
    // "USB Error" (docs/usb.md). Tunable via XDJ_USB_DELAY (seconds).
    let settle = std::env::var("XDJ_USB_DELAY").ok().and_then(|v| v.parse().ok()).unwrap_or(6);
    tokio::time::sleep(Duration::from_secs(settle)).await;

    if let Err(e) = insert_usb(&runtime_dir, &src, &slot).await {
        eprintln!("[usb] insert failed: {e}");
    }
}

/// Space-free staging path for a slot's backing (see insert_usb), per instance.
fn usb_link(slot: &str) -> PathBuf {
    PathBuf::from(format!("/tmp/xdjhost{}_{slot}.img", std::process::id()))
}

/// Run a USB slot's `drive_add` + `device_add` over QMP. Right after a host unmount,
/// macOS's disk arbitration holds the device for a moment and the open fails with
/// "Resource busy": retry the drive_add for a few seconds. If the device_add fails,
/// the drive `did` is removed again: an orphan drive would keep the image's write lock
/// and make every later load of it fail.
pub(crate) async fn attach_usb_drive(qmp: &Path, did: &str, drive_add: String, device_add: String) -> Result<Vec<String>, String> {
    let mut outs = Vec::new();
    for _ in 0..20 {
        outs = qmp_hmp(qmp, std::slice::from_ref(&drive_add)).await?;
        if !outs.iter().any(|o| o.to_ascii_lowercase().contains("resource busy")) {
            break;
        }
        tokio::time::sleep(Duration::from_millis(250)).await;
    }
    // QEMU locks its images: a write lock it can't get means another process has the
    // file (another emulator, Disk Utility, …). Say so instead of QEMU's lock error.
    if outs.iter().any(|o| o.to_ascii_lowercase().contains("failed to lock")) {
        return Err("the image is in use by another program (another emulator, or opened in macOS); close it there and load it again".into());
    }
    let dev = qmp_hmp(qmp, &[device_add]).await?;
    if dev.iter().any(|o| o.to_ascii_lowercase().contains("error")) {
        let _ = qmp_hmp(qmp, &[format!("drive_del {did}")]).await;
    }
    outs.extend(dev);
    Ok(outs)
}

/// Hot-plug a disk image / host device into a front USB slot via QMP, read/write.
/// See maybe_insert_usb for the timing rules.
async fn insert_usb(runtime_dir: &Path, src: &str, slot: &str) -> Result<(), String> {
    let rt = |n: &str| runtime_dir.join(n);
    prepare_usb_backing(src)?;
    // HMP drive_add doesn't quote paths, and the packaged runtime dir is under
    // "Application Support": stage a symlink at a space-free /tmp path (as the sidecar's
    // USB LOAD does, with its own name).
    let link = usb_link(slot);
    let _ = std::fs::remove_file(&link);
    #[cfg(unix)]
    std::os::unix::fs::symlink(src, &link).map_err(|e| format!("cannot stage backing symlink: {e}"))?;

    let (port, fault) = if slot == "usb1" {
        ("1.2", "/tmp/usb_insert")
    } else {
        ("1.1", "/tmp/usb2_insert")
    };
    let did = format!("xdjusb_{slot}");
    eprintln!("[usb] inserting {src} at {slot} (hub port {port}, read/write)");
    // HMP reports its errors as text inside a successful QMP "return": check it.
    let outs = attach_usb_drive(
        &rt("qmp.sock"),
        &did,
        format!("drive_add 0 if=none,id={did},file={},format=raw", link.to_string_lossy()),
        format!("device_add usb-storage,bus=usb-bus.1,port={port},drive={did},id={did}_dev,removable=on"),
    )
    .await?;
    for o in &outs {
        let t = o.trim();
        if !t.is_empty() && t != "{\"return\": \"\"}" {
            eprintln!("[usb] qmp: {t}");
            let l = t.to_ascii_lowercase();
            if ["error", "extraneous", "failed", "duplicate", "in use", "can't find"]
                .iter()
                .any(|e| l.contains(e))
            {
                return Err(format!("the monitor rejected the insert: {t}"));
            }
        }
    }
    // Do NOT create the xdj-panel.c file here: GPIO4_IO30 / GPIO7_IO12 are rbp's
    // active-low USB *fault* flags, not insert-detect lines, and holding one low
    // raises "USB Error. Remove the device." once the slot mounts. The attach
    // itself wakes HOST1 (xdj-usbnc.c, USBNC OWIR). Clear any stale fault file.
    let _ = std::fs::remove_file(fault);
    eprintln!("[usb] attached; the slot should mount shortly");
    Ok(())
}

/// Drive a provisioning boot (BootKind::Provision): insert the update stick when the
/// initramfs asks for it, then report how the updater ended. The console lines are the
/// update initramfs' own (`/init`): "### USB update mount. ###" (it loops mounting
/// /dev/sd[a-e]{1,} until XDJRX.UPD shows up), then "### USB update error. (...)
/// error_code = N ###" on failure. Success = the NAND env gets `release=` (finEnv).
async fn provision_watch(runtime_dir: PathBuf, stick: PathBuf, nand_img: PathBuf) -> Result<(), String> {
    let console = runtime_dir.join("console.log");
    if !wait_console_marker(&console, "### USB update mount.", Duration::from_secs(300)).await {
        return Err("the update initramfs never asked for the USB stick".into());
    }
    tokio::time::sleep(Duration::from_secs(2)).await;
    // USB1 = the top-panel slot the update screen asks for (hub port 1.2).
    insert_usb(&runtime_dir, &stick.to_string_lossy(), "usb1").await?;
    // The ALL update writes ~150 MiB of NAND + the MCU flash under TCG: allow plenty.
    let start = Instant::now();
    while start.elapsed() < Duration::from_secs(3600) {
        if let Ok(b) = std::fs::read(&console) {
            let t = String::from_utf8_lossy(&b);
            if t.contains("### USB update success.") {
                return Ok(());
            }
            // The installer's last write is the NAND env (finEnv: release=, update_status);
            // after it come only the D007 "complete" screen and `umount /mnt/usb`, which stays
            // busy (the .UPD is still loop-mounted), so the success line above never prints.
            if nand::installed(&nand_img) {
                tokio::time::sleep(Duration::from_secs(10)).await;
                return Ok(());
            }
            if let Some(i) = t.find("### USB update error.") {
                let line = t[i..].lines().next().unwrap_or("").trim().to_string();
                return Err(format!("the firmware updater failed: {line}"));
            }
        }
        tokio::time::sleep(Duration::from_secs(1)).await;
    }
    Err("the firmware update did not finish within an hour".into())
}

#[tokio::main]
async fn main() {
    // Subcommand: `xdj-host prep <firmware.UPD|.zip>` prepares/caches boot artifacts
    // and prints the prepared dir (for use as XDJ_FW_DIR). No args = run the emulator.
    let argv: Vec<String> = std::env::args().collect();
    if argv.get(1).map(String::as_str) == Some("prep") {
        let Some(upd) = argv.get(2) else {
            eprintln!("usage: xdj-host prep <firmware.UPD|.zip>");
            std::process::exit(2);
        };
        let cfg = Config::from_env().expect("config");
        match prep_firmware(&cfg, Path::new(upd)) {
            Ok(dir) => println!("{}", dir.display()),
            Err(e) => {
                eprintln!("[prep] failed: {e}");
                std::process::exit(1);
            }
        }
        return;
    }

    let cfg = match Config::from_env() {
        Ok(c) => c,
        Err(e) => {
            eprintln!("[host] config error: {e}");
            std::process::exit(2);
        }
    };
    if let Err(e) = preflight(&cfg) {
        eprintln!("[host] preflight failed: {e}");
        std::process::exit(2);
    }
    if let Err(e) = std::fs::create_dir_all(&cfg.runtime_dir) {
        eprintln!("[host] cannot create runtime dir {}: {e}", cfg.runtime_dir.display());
        std::process::exit(2);
    }
    clean_stale(&cfg);

    // Power: the emulator starts powered off when XDJ_AUTOSTART=0 (the desktop app:
    // the panel's power button / File > Power, or its Power On at Launch pref, turns it
    // on); otherwise on, as before. A blank unit (a firmware just dropped) is powered on
    // into provisioning straight away either way, and boots normally once it's done.
    let autostart = std::env::var("XDJ_AUTOSTART").map(|v| v != "0").unwrap_or(true);
    let mut qemu: Option<Child> = None;
    let mut kind = BootKind::Normal;
    let mut usb: Option<tokio::task::JoinHandle<()>> = None;
    let (prov_tx, mut prov_rx) = tokio::sync::mpsc::channel::<Result<(), String>>(1);
    let mut prov: Option<tokio::task::JoinHandle<()>> = None;
    // Power on for `k`: spawn QEMU and the task that goes with that boot.
    let power_on = |k: BootKind,
                    usb: &mut Option<tokio::task::JoinHandle<()>>,
                    prov: &mut Option<tokio::task::JoinHandle<()>>|
     -> Result<Child, String> {
        let c = spawn_qemu(&cfg, k)?;
        match k {
            BootKind::Provision => {
                let (rt, stick, tx) = (cfg.runtime_dir.clone(), cfg.fw_dir().join(FW_STICK), prov_tx.clone());
                let nand_img = cfg.nand();
                *prov = Some(tokio::spawn(async move {
                    let _ = tx.send(provision_watch(rt, stick, nand_img).await).await;
                }));
            }
            // If XDJ_USB is set, mount it (disk image or host device) once rbp polls.
            BootKind::Normal => *usb = Some(tokio::spawn(maybe_insert_usb(cfg.runtime_dir.clone()))),
            BootKind::Update => {}
        }
        Ok(c)
    };
    if !cfg.provisioned() || autostart {
        kind = if cfg.provisioned() { BootKind::Normal } else { BootKind::Provision };
        match power_on(kind, &mut usb, &mut prov) {
            Ok(c) => qemu = Some(c),
            Err(e) => {
                eprintln!("[host] failed to start qemu: {e}");
                std::process::exit(1);
            }
        }
    }
    // VNC bridge in the background (connects to vnc.sock per client, so it survives
    // power cycles; noVNC just reconnects).
    let vnc_sock = cfg.rt("vnc.sock");
    let ws_port = cfg.vnc_ws_port;
    let bridge = tokio::spawn(run_vnc_ws_bridge(vnc_sock, ws_port));

    let (ptx, mut prx) = tokio::sync::mpsc::channel(8);
    let power = tokio::spawn(power_listener(cfg.rt("power.sock"), ptx.clone()));

    // The virtual panel (in-process). Bind failure (port taken) ends the host: the shell
    // reports "panel didn't come up" rather than talking to someone else's server.
    let panel = panel::Panel::new(
        panel::PanelConfig {
            port: cfg.panel_port,
            panel_dir: cfg.panel_dir.clone(),
            novnc_dir: cfg.novnc_dir.clone(),
            knobs_dir: cfg.knobs_dir.clone(),
            ctrl_sock: cfg.rt("ctrl.sock"),
            key_sock: cfg.rt("key.sock"),
            qmp_sock: cfg.rt("qmp.sock"),
            console: cfg.rt("console.log"),
            fw: cfg.fw.clone(),
        },
        ptx,
    );
    let mut panel_srv = tokio::spawn(panel::serve(panel));

    println!(
        "\n  XDJ-RX emulator {}.\n  Open:  http://127.0.0.1:{}/?vnc=ws://127.0.0.1:{}/\n  (panel :{}, screen = noVNC via the WS bridge :{})\n  Ctrl-C to stop.\n",
        if qemu.is_some() { "up" } else { "ready (powered off — use the panel's power button)" },
        cfg.panel_port, cfg.vnc_ws_port, cfg.panel_port, cfg.vnc_ws_port
    );

    // Supervise: power commands; QEMU exiting = powered off (the guest shut down or
    // crashed); the panel server failing, Ctrl-C, SIGTERM or the shell vanishing end it all.
    loop {
        tokio::select! {
            Some((cmd, reply)) = prx.recv() => {
                match cmd {
                    PowerCmd::On(upd) => {
                        if qemu.is_none() {
                            // Not installed yet (or a provisioning was cut short): provision.
                            let k = if !cfg.provisioned() { BootKind::Provision }
                                    else if upd { BootKind::Update } else { BootKind::Normal };
                            match power_on(k, &mut usb, &mut prov) {
                                Ok(c) => { qemu = Some(c); kind = k; }
                                Err(e) => eprintln!("[host] power on failed: {e}"),
                            }
                        }
                    }
                    PowerCmd::Off => {
                        if let Some(mut q) = qemu.take() {
                            eprintln!("[host] power off");
                            let _ = q.kill().await;   // instant power cut, like the switch
                        }
                        if let Some(u) = usb.take() { u.abort(); }
                        if let Some(p) = prov.take() { p.abort(); }
                    }
                    PowerCmd::Status => {}
                    PowerCmd::LoadFirmware(upd) => {
                        // Power off, prepare (cached by hash, like a drop on the app), switch to
                        // that firmware's dir -- its own NAND unit -- and power on: a blank unit
                        // provisions itself, an installed one boots.
                        if let Some(mut q) = qemu.take() { let _ = q.kill().await; }
                        if let Some(u) = usb.take() { u.abort(); }
                        if let Some(p) = prov.take() { p.abort(); }
                        eprintln!("[host] load firmware {}", upd.display());
                        match prep_firmware(&cfg, &upd) {
                            Ok(dir) => {
                                cfg.set_fw_dir(dir.clone());
                                let _ = std::fs::write(app_data_root().join("last-firmware.txt"), dir.to_string_lossy().as_bytes());
                                let k = if cfg.provisioned() { BootKind::Normal } else { BootKind::Provision };
                                match power_on(k, &mut usb, &mut prov) {
                                    Ok(c) => { qemu = Some(c); kind = k; }
                                    Err(e) => { let _ = reply.send(format!("error: power on: {e}")); continue; }
                                }
                            }
                            Err(e) => { let _ = reply.send(format!("error: {e}")); continue; }
                        }
                    }
                }
                let state = match (&qemu, kind) {
                    (Some(_), BootKind::Normal) => "on",
                    (Some(_), _) => "on update",
                    (None, _) => "off",
                };
                let _ = reply.send(state.into());
            }
            Some(res) = prov_rx.recv() => {
                prov = None;
                match res {
                    Ok(()) => {
                        // Installed. Cut power (the updater is done; the initramfs has
                        // nothing left to run) and boot the unit from NAND.
                        eprintln!("[host] provisioning complete; booting from NAND");
                        if let Some(mut q) = qemu.take() { let _ = q.kill().await; }
                        let _ = std::fs::write(cfg.fw_dir().join(FW_PROVISIONED), b"");
                        match power_on(BootKind::Normal, &mut usb, &mut prov) {
                            Ok(c) => { qemu = Some(c); kind = BootKind::Normal; }
                            Err(e) => eprintln!("[host] power on after provisioning failed: {e}"),
                        }
                    }
                    // Leave it running: the screen shows the updater's own error. The
                    // next power on starts the provisioning over.
                    Err(e) => eprintln!("[host] provisioning failed: {e}"),
                }
            }
            status = wait_opt(&mut qemu) => {
                eprintln!("[host] qemu exited: {status:?} (powered off)");
                qemu = None;
                if let Some(p) = prov.take() { p.abort(); }
            }
            r = &mut panel_srv => {
                eprintln!("[host] panel server ended: {r:?}");
                break;
            }
            _ = tokio::signal::ctrl_c() => {
                eprintln!("[host] shutting down (Ctrl-C)");
                break;
            }
            _ = terminated() => {
                eprintln!("[host] shutting down (SIGTERM)");
                break;
            }
            _ = parent_gone() => {
                eprintln!("[host] parent (shell) exited; shutting down");
                break;
            }
        }
    }
    if let Some(mut q) = qemu.take() {
        let _ = q.kill().await;
    }
    power.abort();
    if let Some(u) = usb.take() { u.abort(); }
    if let Some(p) = prov.take() { p.abort(); }
    bridge.abort();
    clean_stale(&cfg);
}
