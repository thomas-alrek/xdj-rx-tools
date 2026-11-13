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
use sha2::{Digest, Sha256};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, UnixStream};
use tokio::process::{Child, Command};
use tokio_tungstenite::tungstenite::Message;

/// Resolved layout + knobs for one emulator instance.
struct Config {
    repo: PathBuf,
    runtime_dir: PathBuf,
    fw_dir: PathBuf, // prepared firmware dir (boot blobs + nand.img)
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
        // The prepared firmware (boot blobs + nand.img). Phase 3 sets XDJ_FW_DIR to a
        // per-firmware app-data dir; the dev default is the build/qemu layout.
        let fw_dir = std::env::var_os("XDJ_FW_DIR")
            .map(PathBuf::from)
            .unwrap_or_else(|| repo.join("build/qemu"));
        let qemu = std::env::var_os("XDJ_QEMU")
            .map(PathBuf::from)
            .unwrap_or_else(|| default_qemu(&repo));
        let nand = std::env::var_os("XDJ_NAND")
            .map(PathBuf::from)
            .unwrap_or_else(|| fw_dir.join("nand.img"));
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
            fw_dir,
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
    let q = cfg.fw_dir.clone();
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
        let p = cfg.fw_dir.join(b);
        if !p.exists() {
            return Err(format!(
                "boot blob missing: {} (prepare a firmware first: `xdj-host prep <firmware.UPD|.zip>`, or run qemu_prepare.py)",
                p.display()
            ));
        }
    }
    if !cfg.server_py.exists() {
        return Err(format!("panel sidecar missing: {}", cfg.server_py.display()));
    }
    Ok(())
}

// ---- Phase 3: firmware preparation (drag-drop a .UPD -> cached boot artifacts) ----

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

fn which(bin: &str) -> bool {
    std::process::Command::new("sh")
        .arg("-c")
        .arg(format!("command -v {bin}"))
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false)
}

/// A Python with the firmware-tool deps (pycdlib/pycryptodome): the repo venv that
/// run.sh builds, else `uv run`, else system python3. Returns (program, prefix-args).
fn python_cmd(repo: &Path) -> (String, Vec<String>) {
    let venv = repo.join("build/.venv/bin/python3");
    if venv.exists() {
        (venv.to_string_lossy().into_owned(), vec![])
    } else if which("uv") {
        ("uv".into(), vec!["run".into()])
    } else {
        ("python3".into(), vec![])
    }
}

/// Prepare (or reuse a cached) firmware: decrypt -> boot blobs -> nand.img into
/// app-data/<hash>/. Idempotent — a complete cache dir is reused instantly. Returns
/// the prepared dir (pass it back as XDJ_FW_DIR to run).
fn prep_firmware(repo: &Path, upd: &Path) -> Result<PathBuf, String> {
    let upd = upd
        .canonicalize()
        .map_err(|e| format!("firmware not found: {} ({e})", upd.display()))?;
    let hash = firmware_hash(&upd).map_err(|e| format!("cannot hash firmware: {e}"))?;
    let dir = app_data_root().join(&hash);
    let needed = ["vmlinux.bin", "tramp.bin", "atags.bin", "nand.img"];
    if needed.iter().all(|f| dir.join(f).exists()) {
        eprintln!("[prep] cache hit: {}", dir.display());
        return Ok(dir);
    }
    std::fs::create_dir_all(&dir).map_err(|e| format!("cannot create {}: {e}", dir.display()))?;

    let (py, pre) = python_cmd(repo);
    let iso = dir.join("fw.iso");
    let dirs = dir.to_string_lossy().into_owned();
    let run = |desc: &str, args: Vec<String>, envs: Vec<(&str, String)>| -> Result<(), String> {
        eprintln!("[prep] {desc}…");
        let mut c = std::process::Command::new(&py);
        c.args(&pre).args(&args).current_dir(repo);
        for (k, v) in &envs {
            c.env(k, v);
        }
        let st = c.status().map_err(|e| format!("{desc}: spawn failed: {e}"))?;
        if !st.success() {
            return Err(format!("{desc}: exited with {st}"));
        }
        Ok(())
    };
    let script = |rel: &str| repo.join(rel).to_string_lossy().into_owned();

    // 1. decrypt the .UPD/.zip -> ISO (xdjrx_upd.py has a built-in key)
    run(
        "decrypt firmware",
        vec![script("xdjrx_upd.py"), "decrypt".into(), upd.to_string_lossy().into_owned(), "-o".into(), iso.to_string_lossy().into_owned()],
        vec![],
    )?;
    // 2. kernel + SABRE-SD boot blobs -> dir
    run(
        "build boot blobs",
        vec![script("qemu_prepare.py"), iso.to_string_lossy().into_owned()],
        vec![("XDJ_OUT", dirs.clone())],
    )?;
    // 3. nand.img (rootfs + GUI + env + MBR) -> dir
    run(
        "build nand image",
        vec![script("qemu/build-nand.py")],
        vec![("XDJ_OUT", dirs.clone()), ("XDJ_ISO", iso.to_string_lossy().into_owned())],
    )?;
    eprintln!("[prep] done: {}", dir.display());
    Ok(dir)
}

#[tokio::main]
async fn main() {
    // Subcommand: `xdj-host prep <firmware.UPD|.zip>` prepares/caches boot artifacts
    // and prints the prepared dir (for use as XDJ_FW_DIR). No args = run the emulator.
    let argv: Vec<String> = std::env::args().collect();
    if argv.get(1).map(String::as_str) == Some("prep") {
        let repo = std::env::var_os("XDJ_REPO")
            .map(PathBuf::from)
            .unwrap_or_else(|| std::env::current_dir().expect("cwd"));
        let Some(upd) = argv.get(2) else {
            eprintln!("usage: xdj-host prep <firmware.UPD|.zip>");
            std::process::exit(2);
        };
        match prep_firmware(&repo, Path::new(upd)) {
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
        _ = terminated() => {
            eprintln!("[host] shutting down (SIGTERM)");
            let _ = qemu.kill().await;
            let _ = sidecar.kill().await;
        }
    }
    bridge.abort();
    clean_stale(&cfg);
}
