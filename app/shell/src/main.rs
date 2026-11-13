//! XDJ-RX desktop shell (standalone-app roadmap, Phase 2 + 3).
//!
//! A Tauri native window around the `xdj-host` orchestrator.
//!
//! * Startup: if a firmware is already prepared (XDJ_FW_DIR, the last-used one
//!   recorded in app-data, or the dev build/qemu layout) it boots straight into it.
//!   Otherwise it shows a drop target.
//! * Drag-drop a `.UPD`/`.zip` onto the window -> the host prepares it (decrypt ->
//!   boot blobs -> nand.img, cached in app-data by firmware hash; `xdj-host prep`)
//!   with progress, remembers it as last-used, then boots it.
//! * On exit it SIGTERMs `xdj-host` so QEMU + the sidecar + sockets tear down.
//!
//! The emulator window loads the sidecar page directly (same origin for its HTTP/WS
//! calls). Firmware is never touched and never shipped — it's generated from the
//! user's own file.

use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use tauri::{DragDropEvent, Manager, RunEvent, WebviewUrl, WebviewWindowBuilder, WindowEvent};
use url::Url;

const PANEL_PORT: u16 = 8765;
const UI_URL: &str = "http://127.0.0.1:8765/?vnc=ws://127.0.0.1:6080/";

struct AppState {
    repo: PathBuf,
    host: Mutex<Option<Child>>,
    busy: Mutex<bool>, // a prep/boot is in progress; ignore further drops
}

/// Repo root in dev: app/shell -> app -> repo. (Bundled builds: Phase 5.)
fn repo_root() -> PathBuf {
    let md = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    md.parent().and_then(|p| p.parent()).map(|p| p.to_path_buf()).unwrap_or(md)
}

fn app_data_root() -> PathBuf {
    let home = std::env::var_os("HOME").map(PathBuf::from).unwrap_or_else(|| PathBuf::from("."));
    if cfg!(target_os = "macos") {
        home.join("Library/Application Support/XDJ-RX")
    } else {
        std::env::var_os("XDG_DATA_HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|| home.join(".local/share"))
            .join("xdj-rx")
    }
}

fn last_fw_file() -> PathBuf {
    app_data_root().join("last-firmware.txt")
}

/// A firmware dir is usable if it has the boot blobs + nand.img.
fn fw_complete(dir: &Path) -> bool {
    ["vmlinux.bin", "tramp.bin", "atags.bin", "nand.img"]
        .iter()
        .all(|f| dir.join(f).exists())
}

/// What to boot at startup, if anything: XDJ_FW_DIR, else the last-used firmware,
/// else the dev build/qemu layout. None -> show the drop target.
fn resolve_startup_fw(repo: &Path) -> Option<PathBuf> {
    if std::env::var_os("XDJ_FORCE_SETUP").is_some() {
        return None; // testing: always show the drop screen
    }
    if let Some(d) = std::env::var_os("XDJ_FW_DIR").map(PathBuf::from) {
        if fw_complete(&d) {
            return Some(d);
        }
    }
    if let Ok(s) = std::fs::read_to_string(last_fw_file()) {
        let d = PathBuf::from(s.trim());
        if fw_complete(&d) {
            return Some(d);
        }
    }
    // Dev convenience only: the repo's build/qemu layout. NEVER in the packaged app —
    // there `repo` is the compile-time dev path, which would wrongly auto-boot the dev
    // firmware instead of showing the drop/key prompt.
    if app_resources().is_none() {
        let dev = repo.join("build/qemu");
        if fw_complete(&dev) {
            return Some(dev);
        }
    }
    None
}

/// The bundled resources dir (Contents/Resources) when running from a .app, else None.
fn app_resources() -> Option<PathBuf> {
    let exe = std::env::current_exe().ok()?;
    let res = exe.parent()?.parent()?.join("Resources");
    if res.join("xdj-host").exists() {
        Some(res)
    } else {
        None
    }
}

/// Build a Command for xdj-host, bundle-aware: the bundled binary + XDJ_RESOURCES when
/// packaged, else the dev build + XDJ_REPO. `repo` is the dev fallback root.
fn host_command(repo: &Path) -> Command {
    let mut c = if let Some(res) = app_resources() {
        let mut c = Command::new(res.join("xdj-host"));
        c.env("XDJ_RESOURCES", &res).current_dir(&res);
        c
    } else {
        let mut c = Command::new(repo.join("app/host/target/debug/xdj-host"));
        c.env("XDJ_REPO", repo).current_dir(repo);
        c
    };
    // So xdj-host can tear QEMU down if we (the shell) die without a clean exit.
    c.env("XDJ_PARENT_PID", std::process::id().to_string());
    c
}

/// Run `xdj-host prep <upd>`; returns the prepared firmware dir (its stdout).
fn prep_firmware(repo: &Path, upd: &Path) -> Result<PathBuf, String> {
    let out = host_command(repo)
        .arg("prep")
        .arg(upd)
        .output()
        .map_err(|e| format!("could not run xdj-host prep: {e}"))?;
    if !out.status.success() {
        let err = String::from_utf8_lossy(&out.stderr);
        let last = err.lines().last().unwrap_or("prep failed").trim();
        return Err(last.to_string());
    }
    let dir = String::from_utf8_lossy(&out.stdout).trim().to_string();
    if dir.is_empty() {
        return Err("prep produced no output dir".into());
    }
    Ok(PathBuf::from(dir))
}

/// Spawn `xdj-host` for `fw_dir` and wait for the panel port. Replaces any running host.
fn start_host(state: &AppState, fw_dir: &Path) -> Result<(), String> {
    stop_host(state);
    // Hardcoded ports (8765/6080) ⇒ one instance at a time. A leftover instance would
    // otherwise make our sidecar fail to bind and surface as a confusing 60s timeout.
    if std::net::TcpStream::connect(("127.0.0.1", PANEL_PORT)).is_ok() {
        return Err("another XDJ-RX instance appears to be running (port 8765 is busy). Quit it and try again.".into());
    }
    // Capture xdj-host's output to app-data/host.log so failures are diagnosable even
    // when launched from Finder (where stderr would otherwise go to the system log).
    let mut cmd = host_command(&state.repo);
    cmd.env("XDJ_FW_DIR", fw_dir);
    let _ = std::fs::create_dir_all(app_data_root());
    if let Ok(f) = std::fs::File::create(app_data_root().join("host.log")) {
        if let Ok(f2) = f.try_clone() {
            cmd.stdout(Stdio::from(f)).stderr(Stdio::from(f2));
        }
    }
    let child = cmd
        .spawn()
        .map_err(|e| format!("could not start xdj-host: {e}"))?;
    *state.host.lock().unwrap() = Some(child);
    if !wait_port(("127.0.0.1", PANEL_PORT), Duration::from_secs(60)) {
        return Err("the emulator panel didn’t come up — see host.log in the app data folder".into());
    }
    Ok(())
}

fn wait_port(addr: (&str, u16), timeout: Duration) -> bool {
    let start = Instant::now();
    while start.elapsed() < timeout {
        if std::net::TcpStream::connect(addr).is_ok() {
            return true;
        }
        std::thread::sleep(Duration::from_millis(250));
    }
    false
}

/// Stop the orchestrator gracefully: SIGTERM lets xdj-host tear down QEMU + the
/// sidecar and clean its sockets (SIGKILL would orphan them). Hard-kill as a backstop.
fn stop_host(state: &AppState) {
    if let Some(mut c) = state.host.lock().unwrap().take() {
        #[cfg(unix)]
        unsafe {
            libc::kill(c.id() as libc::pid_t, libc::SIGTERM);
        }
        for _ in 0..30 {
            if matches!(c.try_wait(), Ok(Some(_))) {
                return;
            }
            std::thread::sleep(Duration::from_millis(100));
        }
        let _ = c.kill();
        let _ = c.wait();
    }
}

/// Create (or reload) the emulator window on the live UI, and close the setup window.
fn show_emulator(app: &tauri::AppHandle) {
    let url = Url::parse(UI_URL).unwrap();
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.navigate(url);
    } else if let Err(e) =
        WebviewWindowBuilder::new(app, "main", WebviewUrl::External(url))
            .title("Pioneer XDJ-RX")
            .inner_size(1480.0, 900.0)
            .min_inner_size(1000.0, 680.0)
            .build()
    {
        eprintln!("[shell] could not open emulator window: {e}");
        return;
    }
    if let Some(s) = app.get_webview_window("setup") {
        let _ = s.close();
    }
}

/// Handle a dropped firmware file: prep (with progress) then boot it.
fn on_firmware_dropped(app: &tauri::AppHandle, path: PathBuf) {
    if !has_key() {
        if let Some(w) = app.get_webview_window("setup") {
            let _ = w.eval(&format!(
                "show({}, true)",
                js_str("Add your firmware decryption key first — drop your aes256.key file, or paste the 64-hex key below.")
            ));
        }
        return;
    }
    let state = app.state::<AppState>();
    {
        let mut busy = state.busy.lock().unwrap();
        if *busy {
            return;
        }
        *busy = true;
    }
    let app = app.clone();
    std::thread::spawn(move || {
        let state = app.state::<AppState>();
        let setup = app.get_webview_window("setup");
        let say = |msg: &str, err: bool| {
            if let Some(w) = &setup {
                let _ = w.eval(&format!("show({}, {})", js_str(msg), err));
            }
            eprintln!("[shell] {msg}");
        };

        say("Preparing firmware… this can take a minute on first use.", false);
        let result = prep_firmware(&state.repo, &path).and_then(|dir| {
            say("Booting the emulator…", false);
            start_host(&state, &dir).map(|_| dir)
        });

        match result {
            Ok(dir) => {
                let _ = std::fs::create_dir_all(app_data_root());
                let _ = std::fs::write(last_fw_file(), dir.to_string_lossy().as_bytes());
                let app2 = app.clone();
                let _ = app.run_on_main_thread(move || show_emulator(&app2));
            }
            Err(e) => say(&format!("Couldn’t load that firmware: {e}"), true),
        }
        *state.busy.lock().unwrap() = false;
    });
}

/// Minimal JS string literal for eval.
fn js_str(s: &str) -> String {
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

fn is_firmware(p: &Path) -> bool {
    matches!(
        p.extension().and_then(|e| e.to_str()).map(|e| e.to_ascii_lowercase()).as_deref(),
        Some("upd") | Some("zip")
    )
}

// ---- firmware AES key (§5bis-A): user-supplied, stored in app-data, never bundled ----

fn key_dir() -> PathBuf {
    app_data_root().join("key")
}
/// The key is always stored as `aes256.key`; its content is sniffed at use (64-hex key
/// vs a passphrase file). One filename, both formats.
fn key_path() -> PathBuf {
    key_dir().join("aes256.key")
}
fn has_key() -> bool {
    key_path().exists()
}
fn is_hex_key(s: &str) -> bool {
    let t = s.trim();
    t.len() == 64 && t.bytes().all(|b| b.is_ascii_hexdigit())
}
fn store_key_hex(hex: &str) -> Result<(), String> {
    let t = hex.trim();
    if !is_hex_key(t) {
        return Err("the key must be 64 hex characters".into());
    }
    std::fs::create_dir_all(key_dir()).map_err(|e| e.to_string())?;
    std::fs::write(key_path(), t).map_err(|e| e.to_string())
}
/// A dropped key file: a text file of 64 hex is stored as the hex key; anything else is
/// kept as a passphrase file. Either way the stored file is `aes256.key`.
fn store_key_file(path: &Path) -> Result<(), String> {
    let content = std::fs::read_to_string(path).unwrap_or_default();
    if is_hex_key(&content) {
        return store_key_hex(&content);
    }
    std::fs::create_dir_all(key_dir()).map_err(|e| e.to_string())?;
    std::fs::copy(path, key_path()).map(|_| ()).map_err(|e| e.to_string())
}

#[tauri::command]
fn set_key_hex(hex: String) -> Result<(), String> {
    store_key_hex(&hex)
}
#[tauri::command]
fn key_present() -> bool {
    has_key()
}

/// Native file picker for the panel's USB LOAD buttons. Opens an OS file dialog and
/// returns the chosen path (or None if cancelled); the panel then POSTs it to the
/// sidecar's /usb endpoint, which does the drive_add/device_add insert over QMP.
#[tauri::command]
fn pick_usb_image(app: tauri::AppHandle, slot: String) -> Option<String> {
    use tauri_plugin_dialog::DialogExt;
    let title = format!("Select a USB disk image for {}", slot.to_uppercase());
    app.dialog()
        .file()
        .set_title(title)
        .add_filter("Disk images", &["img", "dmg", "iso", "bin", "raw"])
        .add_filter("All files", &["*"])
        .blocking_pick_file()
        .and_then(|f| f.into_path().ok())
        .map(|p| p.to_string_lossy().into_owned())
}

/// A dropped non-firmware file is treated as the firmware key.
fn on_key_dropped(app: &tauri::AppHandle, path: PathBuf) {
    let setup = app.get_webview_window("setup");
    match store_key_file(&path) {
        Ok(()) => {
            if let Some(w) = &setup {
                let _ = w.eval("keySaved&&keySaved()");
                let _ = w.eval(&format!("show({}, false)", js_str("Firmware key saved — now drop a firmware .UPD.")));
            }
        }
        Err(e) => {
            if let Some(w) = &setup {
                let _ = w.eval(&format!("show({}, true)", js_str(&format!("That didn’t look like a key: {e}"))));
            }
        }
    }
}

fn main() {
    let repo = repo_root();
    let startup_fw = resolve_startup_fw(&repo);

    let app = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .manage(AppState { repo: repo.clone(), host: Mutex::new(None), busy: Mutex::new(false) })
        .invoke_handler(tauri::generate_handler![set_key_hex, key_present, pick_usb_image])
        .setup(move |app| {
            match &startup_fw {
                Some(dir) => {
                    // A firmware is ready: boot straight into it.
                    let state = app.state::<AppState>();
                    if let Err(e) = start_host(&state, dir) {
                        eprintln!("[shell] {e}; falling back to the drop screen");
                        open_setup(app.handle())?;
                    } else {
                        show_emulator(app.handle());
                    }
                }
                None => {
                    open_setup(app.handle())?;
                }
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            match event {
                WindowEvent::DragDrop(DragDropEvent::Drop { paths, .. }) => {
                    if let Some(p) = paths.first() {
                        if is_firmware(p) {
                            on_firmware_dropped(window.app_handle(), p.clone());
                        } else {
                            on_key_dropped(window.app_handle(), p.clone());
                        }
                    }
                }
                // Closing the emulator window stops the whole emulator stack now
                // (don't leave a headless QEMU running).
                WindowEvent::CloseRequested { .. } | WindowEvent::Destroyed => {
                    if window.label() == "main" {
                        stop_host(&window.app_handle().state::<AppState>());
                    }
                }
                _ => {}
            }
        })
        .build(tauri::generate_context!())
        .expect("failed to build the XDJ-RX shell");

    app.run(|handle, event| {
        if let RunEvent::ExitRequested { .. } = event {
            stop_host(&handle.state::<AppState>());
        }
    });
}

fn open_setup(app: &tauri::AppHandle) -> tauri::Result<()> {
    WebviewWindowBuilder::new(app, "setup", WebviewUrl::App("index.html".into()))
        .title("Pioneer XDJ-RX")
        .inner_size(760.0, 520.0)
        .build()?;
    Ok(())
}
