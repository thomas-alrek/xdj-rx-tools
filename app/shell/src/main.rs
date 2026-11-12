//! XDJ-RX desktop shell (standalone-app roadmap, Phase 2).
//!
//! A thin Tauri native window around the verified `xdj-host` orchestrator: on
//! startup it spawns xdj-host (which brings up QEMU + the panel sidecar + the
//! noVNC WebSocket bridge), waits for the panel HTTP port, then opens the window
//! on the live UI (panel skin + noVNC screen). On exit it stops xdj-host, which
//! tears down QEMU and cleans its per-instance sockets.
//!
//! The window loads the sidecar's page directly (same origin for its HTTP/WS
//! calls); the page uses no Tauri IPC. Firmware is never touched.

use std::process::{Child, Command};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use tauri::{Manager, RunEvent, WebviewUrl, WebviewWindowBuilder};
use url::Url;

/// The spawned orchestrator child, so we can stop it when the app exits.
struct HostChild(Mutex<Option<Child>>);

/// Repo root in dev: app/shell -> app -> repo. (Bundled builds will resolve
/// resources differently; that's Phase 5.)
fn repo_root() -> std::path::PathBuf {
    let md = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    md.parent()
        .and_then(|p| p.parent())
        .map(|p| p.to_path_buf())
        .unwrap_or(md)
}

/// Block until something is listening on `addr`, or the timeout elapses.
fn wait_port(addr: &str, timeout: Duration) -> bool {
    let start = Instant::now();
    while start.elapsed() < timeout {
        if std::net::TcpStream::connect(addr).is_ok() {
            return true;
        }
        std::thread::sleep(Duration::from_millis(250));
    }
    false
}

fn main() {
    let app = tauri::Builder::default()
        .manage(HostChild(Mutex::new(None)))
        .setup(|app| {
            let repo = repo_root();
            let bin = repo.join("app/host/target/debug/xdj-host");
            let child = Command::new(&bin)
                .current_dir(&repo)
                .env("XDJ_REPO", &repo)
                .spawn()
                .unwrap_or_else(|e| {
                    panic!("failed to start xdj-host ({}): {e}", bin.display())
                });
            app.state::<HostChild>().0.lock().unwrap().replace(child);

            // The sidecar binds its port within ~a second; QEMU keeps booting and
            // noVNC fills the screen in once the VNC socket exists (it retries).
            wait_port("127.0.0.1:8765", Duration::from_secs(60));

            let url = Url::parse("http://127.0.0.1:8765/?vnc=ws://127.0.0.1:6080/").unwrap();
            WebviewWindowBuilder::new(app, "main", WebviewUrl::External(url))
                .title("Pioneer XDJ-RX")
                .inner_size(1120.0, 760.0)
                .build()?;
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("failed to build the XDJ-RX shell");

    app.run(|handle, event| {
        if let RunEvent::ExitRequested { .. } = event {
            if let Some(mut c) = handle.state::<HostChild>().0.lock().unwrap().take() {
                stop_host(&mut c);
            }
        }
    });
}

/// Stop the orchestrator gracefully: SIGTERM lets xdj-host tear down QEMU + the
/// sidecar and clean its sockets (a plain kill() = SIGKILL would skip that and
/// orphan them). Fall back to a hard kill if it doesn't exit in time.
fn stop_host(c: &mut Child) {
    #[cfg(unix)]
    unsafe {
        libc::kill(c.id() as libc::pid_t, libc::SIGTERM);
    }
    for _ in 0..30 {
        match c.try_wait() {
            Ok(Some(_)) => return,
            _ => std::thread::sleep(Duration::from_millis(100)),
        }
    }
    let _ = c.kill();
    let _ = c.wait();
}
