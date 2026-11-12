//! XDJ-RX emulator orchestrator (standalone-app roadmap, Phase 2).
//!
//! Supervises one emulator instance and wires the three per-instance channels the
//! roadmap calls for, out of the global `/tmp` world:
//!
//!   * **QEMU** spawned headless (`-display none`) with, in a per-instance runtime
//!     dir: `-qmp unix:qmp.sock` (lifecycle), `-vnc unix:vnc.sock` (the host-side
//!     scanout surface -- the same surface cocoa/screendump draw, so zero guest
//!     changes), and the Phase-1 control chardevs `id=xdjctrl` / `id=xdjkey`.
//!   * **The panel sidecar** (`tools/xdj-panel/server.py`) run as a child, pointed
//!     at `ctrl.sock`/`key.sock` -- the roadmap's "run server.py as a sidecar first,
//!     port to Rust later" path. It still serves panel.html + composes controls.
//!   * **A WebSocket<->VNC bridge**: noVNC speaks RFB-over-WebSocket while QEMU
//!     `-vnc` is raw RFB over the unix socket, so we accept WS clients and pump bytes
//!     to/from `vnc.sock`. This replaces panel.html's `/raw` screendump poll (the
//!     roadmap's main perf pain) with push + dirty-rects.
//!
//! This is the Rust host core; the Tauri shell will wrap it (webview instead of a
//! browser tab). For now, open the printed URL in a browser to drive the emulator.
//!
//! Firmware is never touched -- everything here is host-side orchestration.

use std::path::{Path, PathBuf};

use futures_util::{SinkExt, StreamExt};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, UnixStream};
use tokio::process::{Child, Command};
use tokio_tungstenite::tungstenite::Message;

/// Resolved layout + knobs for one emulator instance.
struct Config {
    repo: PathBuf,
    runtime_dir: PathBuf,
    qemu: PathBuf,
    nand: PathBuf,
    python: String,
    server_py: PathBuf,
    panel_port: u16,
    vnc_ws_port: u16,
    smp: String,
    audio: String, // "none" disables host audio
}

impl Config {
    fn from_env() -> std::io::Result<Config> {
        let repo = match std::env::var_os("XDJ_REPO") {
            Some(p) => PathBuf::from(p),
            None => std::env::current_dir()?,
        };
        let runtime_dir = std::env::var_os("XDJ_RUNTIME_DIR")
            .map(PathBuf::from)
            .unwrap_or_else(|| repo.join("build/run"));
        let qemu = std::env::var_os("XDJ_QEMU")
            .map(PathBuf::from)
            .unwrap_or_else(|| default_qemu(&repo));
        let nand = std::env::var_os("XDJ_NAND")
            .map(PathBuf::from)
            .unwrap_or_else(|| repo.join("build/qemu/nand.img"));
        let python = std::env::var("XDJ_PYTHON").unwrap_or_else(|_| "python3".into());
        let server_py = repo.join("tools/xdj-panel/server.py");
        let panel_port = env_u16("XDJ_PANEL_PORT", 8765);
        let vnc_ws_port = env_u16("XDJ_VNC_WS_PORT", 6080);
        let smp = std::env::var("XDJSMP").unwrap_or_else(|_| "2".into());
        // Match watch.sh: coreaudio on macOS, pa elsewhere, keep timer-period=1451.
        let audio = std::env::var("XDJAUDIO").unwrap_or_else(|_| {
            if cfg!(target_os = "macos") {
                "coreaudio,timer-period=1451".into()
            } else {
                "pa,timer-period=1451".into()
            }
        });
        Ok(Config {
            repo,
            runtime_dir,
            qemu,
            nand,
            python,
            server_py,
            panel_port,
            vnc_ws_port,
            smp,
            audio,
        })
    }

    fn rt(&self, name: &str) -> PathBuf {
        self.runtime_dir.join(name)
    }
}

fn default_qemu(repo: &Path) -> PathBuf {
    // Prefer the patched build; fall back to PATH's qemu-system-arm.
    let built = repo.join("build/qemu-src/qemu-11.1.0/build/qemu-system-arm");
    if built.exists() {
        built
    } else {
        PathBuf::from("qemu-system-arm")
    }
}

fn env_u16(key: &str, default: u16) -> u16 {
    std::env::var(key)
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(default)
}

fn qemu_args(cfg: &Config) -> Vec<String> {
    let q = cfg.repo.join("build/qemu");
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
    if cfg.audio != "none" {
        a.push("-audio".into());
        a.push(cfg.audio.clone());
    }
    a.extend([
        "-device".into(), format!("loader,file={},addr=0x10008000,force-raw=on", s(q.join("vmlinux.bin"))),
        "-device".into(), format!("loader,file={},addr=0x10000100,force-raw=on", s(q.join("atags.bin"))),
        "-device".into(), format!("loader,file={},addr=0x10000000,cpu-num=0", s(q.join("tramp.bin"))),
        "-drive".into(), format!("if=none,id=xdjsd,file={},format=raw", s(cfg.nand.clone())),
        "-nic".into(), "user".into(),
        // Phase-1 control channels (per-instance).
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

fn spawn_qemu(cfg: &Config) -> std::io::Result<Child> {
    let args = qemu_args(cfg);
    eprintln!("[host] qemu: {} {}", cfg.qemu.display(), args.join(" "));
    Command::new(&cfg.qemu)
        .args(&args)
        .current_dir(&cfg.repo)
        .kill_on_drop(true)
        .spawn()
}

fn spawn_sidecar(cfg: &Config) -> std::io::Result<Child> {
    let args = vec![
        cfg.server_py.to_string_lossy().into_owned(),
        "--port".into(), cfg.panel_port.to_string(),
        "--ctrl-sock".into(), cfg.rt("ctrl.sock").to_string_lossy().into_owned(),
        "--key-sock".into(), cfg.rt("key.sock").to_string_lossy().into_owned(),
    ];
    eprintln!("[host] sidecar: {} {}", cfg.python, args.join(" "));
    Command::new(&cfg.python)
        .args(&args)
        .current_dir(&cfg.repo)
        .kill_on_drop(true)
        .spawn()
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

fn clean_stale(cfg: &Config) {
    for s in ["qmp.sock", "vnc.sock", "ctrl.sock", "key.sock"] {
        let _ = std::fs::remove_file(cfg.rt(s));
    }
}

fn preflight(cfg: &Config) -> Result<(), String> {
    if !cfg.qemu.exists() && cfg.qemu.is_absolute() {
        return Err(format!("qemu not found: {} (build it with qemu/build-qemu.sh, or set XDJ_QEMU)", cfg.qemu.display()));
    }
    if !cfg.nand.exists() {
        return Err(format!("nand image not found: {} (build it with qemu/build-nand.sh, or set XDJ_NAND)", cfg.nand.display()));
    }
    for b in ["vmlinux.bin", "tramp.bin", "atags.bin"] {
        let p = cfg.repo.join("build/qemu").join(b);
        if !p.exists() {
            return Err(format!("boot blob missing: {} (run: python3 qemu_prepare.py)", p.display()));
        }
    }
    if !cfg.server_py.exists() {
        return Err(format!("panel sidecar missing: {}", cfg.server_py.display()));
    }
    Ok(())
}

#[tokio::main]
async fn main() {
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

    let mut qemu = match spawn_qemu(&cfg) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("[host] failed to start qemu: {e}");
            std::process::exit(1);
        }
    };
    let mut sidecar = match spawn_sidecar(&cfg) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("[host] failed to start panel sidecar: {e}");
            let _ = qemu.kill().await;
            std::process::exit(1);
        }
    };

    // VNC bridge in the background.
    let vnc_sock = cfg.rt("vnc.sock");
    let ws_port = cfg.vnc_ws_port;
    let bridge = tokio::spawn(run_vnc_ws_bridge(vnc_sock, ws_port));

    println!(
        "\n  XDJ-RX emulator up.\n  Open:  http://127.0.0.1:{}/?vnc=ws://127.0.0.1:{}/\n  (panel = sidecar :{}, screen = noVNC via the WS bridge :{})\n  Ctrl-C to stop.\n",
        cfg.panel_port, cfg.vnc_ws_port, cfg.panel_port, cfg.vnc_ws_port
    );

    // Supervise: exit when QEMU or the sidecar dies, or on Ctrl-C; take the rest down.
    tokio::select! {
        status = qemu.wait() => {
            eprintln!("[host] qemu exited: {status:?}");
            let _ = sidecar.kill().await;
        }
        status = sidecar.wait() => {
            eprintln!("[host] panel sidecar exited: {status:?}");
            let _ = qemu.kill().await;
        }
        _ = tokio::signal::ctrl_c() => {
            eprintln!("[host] shutting down (Ctrl-C)");
            let _ = qemu.kill().await;
            let _ = sidecar.kill().await;
        }
    }
    bridge.abort();
    clean_stale(&cfg);
}
