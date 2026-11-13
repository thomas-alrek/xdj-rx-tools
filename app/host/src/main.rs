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
    fw_dir: PathBuf, // prepared firmware dir (boot blobs + nand.img)
    qemu: PathBuf,
    nand: PathBuf,
    python: String,                // sidecar (server.py) interpreter
    python_bin: Option<PathBuf>,   // bundled python with the prep deps, if packaged
    server_py: PathBuf,
    scripts_dir: PathBuf,          // dir holding xdjrx_upd.py / qemu_prepare.py / build-nand.py
    e2fsprogs_dir: Option<PathBuf>, // bundled mke2fs/debugfs dir, prepended to PATH for prep
    novnc_dir: Option<PathBuf>,    // bundled noVNC dir, passed to the sidecar
    knobs_dir: Option<PathBuf>,    // bundled Knobs (audio-knobs) dist, passed to the sidecar
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

impl Config {
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
        // The prepared firmware (boot blobs + nand.img). Phase 3 sets XDJ_FW_DIR to a
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
            .unwrap_or_else(|| fw_dir.join("nand.img"));
        let python_bin = res
            .as_ref()
            .map(|r| r.join("python/bin/python3"))
            .filter(|p| p.exists());
        let python = std::env::var("XDJ_PYTHON").unwrap_or_else(|_| {
            python_bin
                .as_ref()
                .map(|p| p.to_string_lossy().into_owned())
                .unwrap_or_else(|| "python3".into())
        });
        let server_py = pick("panel/server.py", "tools/xdj-panel/server.py");
        let scripts_dir = res
            .as_ref()
            .map(|r| r.join("scripts"))
            .filter(|p| p.is_dir())
            .unwrap_or_else(|| repo.clone());
        let e2fsprogs_dir = res.as_ref().map(|r| r.join("e2fsprogs")).filter(|p| p.is_dir());
        let novnc_dir = res.as_ref().map(|r| r.join("ui/novnc")).filter(|p| p.is_dir());
        let knobs_dir = res.as_ref().map(|r| r.join("ui/knobs")).filter(|p| p.is_dir());
        let qemu_data = res.as_ref().map(|r| r.join("qemu-bios")).filter(|p| p.is_dir());
        let panel_port = env_u16("XDJ_PANEL_PORT", 8765);
        let vnc_ws_port = env_u16("XDJ_VNC_WS_PORT", 6080);
        let smp = std::env::var("XDJSMP").unwrap_or_else(|_| "2".into());
        let audio = pick_audio();
        Ok(Config {
            repo,
            runtime_dir,
            fw_dir,
            qemu,
            nand,
            python,
            python_bin,
            server_py,
            scripts_dir,
            e2fsprogs_dir,
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

    /// Python for the prep scripts: the bundled interpreter (deps installed) if
    /// packaged, else the dev venv / uv / system python.
    fn prep_python(&self) -> (String, Vec<String>) {
        if let Some(p) = &self.python_bin {
            (p.to_string_lossy().into_owned(), vec![])
        } else {
            python_cmd(&self.repo)
        }
    }

    fn script(&self, name: &str) -> PathBuf {
        self.scripts_dir.join(name)
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
    a.extend([
        "-device".into(), format!("loader,file={},addr=0x10008000,force-raw=on", s(q.join("vmlinux.bin"))),
        "-device".into(), format!("loader,file={},addr=0x10000100,force-raw=on", s(q.join("atags.bin"))),
        "-device".into(), format!("loader,file={},addr=0x10000000,cpu-num=0", s(q.join("tramp.bin"))),
        "-drive".into(), format!("if=none,id=xdjsd,file={},format=raw", s(cfg.nand.clone())),
        "-nic".into(), "user".into(),
        // USB-2.0 hub on HOST1 (guest bus 2 = usb-bus.1), hub-only boot: the front
        // slots live behind this hub at devpaths 2-1.1 (USB2) / 2-1.2 (USB1). Storage
        // is inserted AFTER rbp is polling (see maybe_insert_usb) — cold-plugging at
        // boot hits a mount-rendezvous race ("USB Error"). See docs/usb-slot-topology.md.
        "-device".into(), "usb-hub,bus=usb-bus.1,port=1,id=xdjhub".into(),
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

fn spawn_qemu(cfg: &Config) -> std::io::Result<Child> {
    let args = qemu_args(cfg);
    eprintln!("[host] qemu: {} {}", cfg.qemu.display(), args.join(" "));
    let (out, err) = log_stdio(cfg, "qemu.log");
    Command::new(&cfg.qemu)
        .args(&args)
        .current_dir(&cfg.repo)
        .stdout(out)
        .stderr(err)
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
    let (out, err) = log_stdio(cfg, "sidecar.log");
    let mut c = Command::new(&cfg.python);
    c.args(&args).current_dir(&cfg.repo).stdout(out).stderr(err).kill_on_drop(true);
    // Packaged: tell the sidecar where the bundled noVNC lives (its default is a
    // repo-relative path that doesn't exist in the .app).
    if let Some(n) = &cfg.novnc_dir {
        c.env("XDJ_NOVNC_DIR", n);
    }
    if let Some(k) = &cfg.knobs_dir {
        c.env("XDJ_KNOBS_DIR", k);
    }
    c.spawn()
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
    for s in ["qmp.sock", "vnc.sock", "ctrl.sock", "key.sock", "usb_usb1.img", "usb_usb2.img"] {
        let _ = std::fs::remove_file(cfg.rt(s));
    }
    // The USB detect-GPIO hooks (patch 06) are still global /tmp paths (not yet
    // per-instance) — clear them so a stale insert doesn't carry across runs.
    for f in ["/tmp/usb_insert", "/tmp/usb2_insert"] {
        let _ = std::fs::remove_file(f);
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

/// Where the user-supplied firmware key lives (the shell's key prompt writes it here).
/// Never bundled. Always the file `aes256.key`, whose content is sniffed: a 64-hex key
/// goes via XDJRX_AES_KEY, anything else is treated as a passphrase file (--key-file).
fn firmware_key_dir() -> PathBuf {
    app_data_root().join("key")
}

fn is_hex_key(s: &str) -> bool {
    let t = s.trim();
    t.len() == 64 && t.bytes().all(|b| b.is_ascii_hexdigit())
}

/// Resolve the firmware decryption key for the decrypt step: returns extra
/// xdjrx_upd.py args + env. Prefers the app-data key; falls back to the dev .env that
/// xdjrx_upd reads; errors only if no key is available anywhere.
fn resolve_firmware_key(cfg: &Config) -> Result<(Vec<String>, Vec<(&'static str, String)>), String> {
    let kf = firmware_key_dir().join("aes256.key");
    if kf.exists() {
        let content = std::fs::read_to_string(&kf).unwrap_or_default();
        if is_hex_key(&content) {
            return Ok((vec![], vec![("XDJRX_AES_KEY", content.trim().to_string())]));
        }
        return Ok((vec!["--key-file".into(), kf.to_string_lossy().into_owned()], vec![]));
    }
    if cfg.repo.join(".env").exists() || cfg.scripts_dir.join(".env").exists() {
        return Ok((vec![], vec![])); // dev: xdjrx_upd reads .env
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
fn prep_firmware(cfg: &Config, upd: &Path) -> Result<PathBuf, String> {
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

    let (py, pre) = cfg.prep_python();
    // Prepend the bundled e2fsprogs (if packaged) so build-nand.py finds mke2fs/debugfs.
    let path_env = cfg.e2fsprogs_dir.as_ref().map(|d| {
        let cur = std::env::var("PATH").unwrap_or_default();
        format!("{}:{}", d.to_string_lossy(), cur)
    });
    let iso = dir.join("fw.iso");
    let dirs = dir.to_string_lossy().into_owned();
    let run = |desc: &str, args: Vec<String>, envs: Vec<(&str, String)>| -> Result<(), String> {
        eprintln!("[prep] {desc}…");
        let mut c = std::process::Command::new(&py);
        c.args(&pre).args(&args).current_dir(&cfg.repo);
        if let Some(p) = &path_env {
            c.env("PATH", p);
        }
        for (k, v) in &envs {
            c.env(k, v);
        }
        let st = c.status().map_err(|e| format!("{desc}: spawn failed: {e}"))?;
        if !st.success() {
            return Err(format!("{desc}: exited with {st}"));
        }
        Ok(())
    };
    let script = |rel: &str| cfg.script(rel).to_string_lossy().into_owned();

    // 1. decrypt the .UPD/.zip -> ISO. The AES key is USER-SUPPLIED, never bundled
    // (§5bis-A): prefer the key saved in app-data (the shell's key prompt stores it
    // there), else fall back to the dev .env that xdjrx_upd reads. No key anywhere ->
    // a clear error the shell turns into "add your firmware key".
    let (key_args, key_env) = resolve_firmware_key(cfg)?;
    let mut dargs = vec![script("xdjrx_upd.py")];
    dargs.extend(key_args);
    dargs.extend([
        "decrypt".into(),
        upd.to_string_lossy().into_owned(),
        "-o".into(),
        iso.to_string_lossy().into_owned(),
    ]);
    run("decrypt firmware", dargs, key_env)?;
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

// ---- Phase 3b: mount a USB (disk image OR physical device) after boot ----

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
async fn qmp_hmp(sock: &Path, cmds: &[String]) -> Result<Vec<String>, String> {
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

/// Unmount a host disk so the guest is the only writer (snapshot=on already keeps
/// the backing read-only, but a mounted host volume is a second writer).
fn unmount_host_disk(dev: &str) {
    #[cfg(target_os = "macos")]
    let _ = std::process::Command::new("diskutil").arg("unmountDisk").arg(dev).status();
    #[cfg(target_os = "linux")]
    let _ = std::process::Command::new("umount").arg(dev).status();
}

/// If XDJ_USB is set, mount it into the guest AFTER rbp is polling, reproducing the
/// hardware insert choreography (hub-only boot -> post-boot device_add + detect GPIO).
///   XDJ_USB=<image.img|/dev/diskN>   the backing: a disk image, or a host block device
///   XDJ_USB_SLOT=usb1|usb2           front slot (default usb2 = hub port 1.1)
///   XDJ_USB_WRITE=1                   persist writes (default: snapshot=on, discarded —
///                                     the safe default, and what protects a real stick)
/// A physical device is unmounted on the host first. See docs/usb-mount-howto.md.
async fn maybe_insert_usb(runtime_dir: PathBuf) {
    let Some(src) = std::env::var_os("XDJ_USB") else {
        return;
    };
    let src = src.to_string_lossy().into_owned();
    let slot = std::env::var("XDJ_USB_SLOT").unwrap_or_else(|_| "usb2".into());
    let write = std::env::var_os("XDJ_USB_WRITE").is_some();
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
    // "USB Error" (docs/usb-slot-topology.md). Tunable via XDJ_USB_DELAY (seconds).
    let settle = std::env::var("XDJ_USB_DELAY").ok().and_then(|v| v.parse().ok()).unwrap_or(6);
    tokio::time::sleep(Duration::from_secs(settle)).await;

    let is_dev = src.starts_with("/dev/");
    if is_dev {
        eprintln!("[usb] unmounting host disk {src} (snapshot keeps it read-only)");
        unmount_host_disk(&src);
    }
    // HMP drive_add doesn't quote paths; symlink to a space-free path in the runtime dir.
    let link = rt(&format!("usb_{slot}.img"));
    let _ = std::fs::remove_file(&link);
    #[cfg(unix)]
    if let Err(e) = std::os::unix::fs::symlink(&src, &link) {
        eprintln!("[usb] cannot stage backing symlink: {e}");
        return;
    }

    let (port, detect) = if slot == "usb1" {
        ("1.2", "/tmp/usb_insert")
    } else {
        ("1.1", "/tmp/usb2_insert")
    };
    let snap = if write { "" } else { ",snapshot=on" };
    let did = format!("xdjusb_{slot}");
    let cmds = vec![
        format!("drive_add 0 if=none,id={did},file={},format=raw{snap}", link.to_string_lossy()),
        format!("device_add usb-storage,bus=usb-bus.1,port={port},drive={did},id={did}_dev,removable=on"),
    ];
    eprintln!(
        "[usb] inserting {src} at {slot} (hub port {port}, {})",
        if write { "writable" } else { "snapshot" }
    );
    match qmp_hmp(&rt("qmp.sock"), &cmds).await {
        Ok(outs) => {
            for o in outs {
                let t = o.trim();
                if !t.is_empty() && t != "{\"return\": \"\"}" {
                    eprintln!("[usb] qmp: {t}");
                }
            }
            // Assert the slot's detect GPIO (patch 06) so the firmware resumes HOST1.
            if let Err(e) = std::fs::write(detect, b"") {
                eprintln!("[usb] could not assert {detect}: {e}");
            } else {
                eprintln!("[usb] asserted {detect}; the slot should mount shortly");
            }
        }
        Err(e) => eprintln!("[usb] insert failed: {e}"),
    }
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

    // Phase 3b: if XDJ_USB is set, mount it (disk image or host device) once rbp polls.
    let usb = tokio::spawn(maybe_insert_usb(cfg.runtime_dir.clone()));

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
    usb.abort();
    clean_stale(&cfg);
}
