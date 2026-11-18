//! XDJ-RX Firmware Patcher - the desktop app for xdjrx-patcher (Tauri, like the emulator's
//! shell; the page in ui/index.html is styled like the emulator's setup screen).
//!
//! The work is crates/xdjrx-patcher's library (the CLI does the same): Pioneer's official
//! firmware 2.21 + a patch file (.xdjpatch) -> the release's XDJRX.UPD, verified by MD5. The key
//! is the user's if given, else the saved one (shared with the emulator app) if it opens the
//! firmware, else it comes from Pioneer's public GPL source code; a key that built the release
//! is saved.
//!
//! Page <- Rust (window.eval): `render(state)` after every change, `progress(pct, step, detail)`
//! while building, `done(result)`, `failed(message)`.
//! Page -> Rust (invoke): `ready`, `browse(kind)`, `build(key)`, `reveal`.
//! SPDX-License-Identifier: GPL-2.0-or-later
#![cfg_attr(windows, windows_subsystem = "windows")]

use serde_json::json;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use tauri::{DragDropEvent, Manager, WebviewUrl, WebviewWindowBuilder, WindowEvent};
use xdjrx_patcher::{key_from_arg, key_from_gpl, save_key, saved_key, Job, Msg, DOWNLOAD_MB};

#[derive(Default)]
struct State {
    firmware: Option<PathBuf>,
    patch: Option<PathBuf>,
    output: Option<PathBuf>, // None = the default next to the firmware
    job: Option<Result<Arc<Job>, String>>,
    checking: bool,
    busy: bool,
    key: Option<[u8; 32]>, // the saved key (shared with the emulator app), or one fetched this session
    result: Option<PathBuf>,
}

type Shared = Arc<Mutex<State>>;

fn js_str(s: &str) -> String {
    serde_json::to_string(s).unwrap_or_else(|_| "\"\"".into())
}

fn eval(app: &tauri::AppHandle, js: &str) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.eval(js);
    }
}

fn name_of(p: &Path) -> String {
    p.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_else(|| p.display().to_string())
}

/// Send the page the whole state.
fn render(app: &tauri::AppHandle) {
    let shared = app.state::<Shared>();
    let s = shared.lock().unwrap();
    let (job_ok, release, from, error) = match &s.job {
        Some(Ok(j)) => (true, j.release().to_string(), j.input_release().to_string(), None),
        Some(Err(e)) => (false, String::new(), String::new(), Some(e.clone())),
        None => (false, String::new(), String::new(), None),
    };
    let patch_error = error.as_deref().map_or(false, |e| e.contains("patch"));
    let output = s.output.clone().or_else(|| match &s.job {
        Some(Ok(j)) => Some(j.default_output()),
        _ => None,
    });
    let st = json!({
        "firmware": s.firmware.as_deref().map(name_of),
        "patch": s.patch.as_deref().map(name_of),
        "checking": s.checking,
        "ok": job_ok,
        "release": release,
        "from": from,
        "error": error,
        "errorIn": if error.is_none() { "" } else if patch_error { "patch" } else { "firmware" },
        "output": output.map(|p| p.display().to_string()),
        "busy": s.busy,
        "downloadMb": DOWNLOAD_MB,
        "haveKey": s.key.is_some(),
    });
    drop(s);
    eval(app, &format!("render({st})"));
}

/// A dropped or chosen file, sorted into its slot by extension (a key file goes to the key box).
fn take_file(app: &tauri::AppHandle, p: PathBuf) {
    let ext = p.extension().map(|e| e.to_string_lossy().to_lowercase()).unwrap_or_default();
    {
        let shared = app.state::<Shared>();
        let mut s = shared.lock().unwrap();
        if s.busy {
            return;
        }
        match ext.as_str() {
            "xdjpatch" => s.patch = Some(p),
            "upd" | "zip" => {
                s.firmware = Some(p);
                s.output = None;
            }
            "key" => {
                drop(s);
                eval(app, &format!("setKeyFile({})", js_str(&p.display().to_string())));
                return;
            }
            _ => {
                drop(s);
                eval(
                    app,
                    &format!("failed({})", js_str(&format!("{}: not a firmware (.UPD or .zip) or a patch file (.xdjpatch)", name_of(&p)))),
                );
                return;
            }
        }
        s.result = None;
    }
    eval(app, "clearStatus()");
    check(app);
}

/// Open the job (patch + the official firmware's MD5) in the background once both are set.
fn check(app: &tauri::AppHandle) {
    let (fw, patch) = {
        let shared = app.state::<Shared>();
        let mut s = shared.lock().unwrap();
        s.job = None;
        match (s.firmware.clone(), s.patch.clone()) {
            (Some(f), Some(p)) => {
                s.checking = true;
                (f, p)
            }
            _ => {
                drop(s);
                render(app);
                return;
            }
        }
    };
    render(app);
    let app = app.clone();
    std::thread::spawn(move || {
        let r = Job::open(&fw, &patch).map(Arc::new);
        {
            let shared = app.state::<Shared>();
            let mut s = shared.lock().unwrap();
            // a newer pick replaced these files meanwhile: drop this result
            if s.firmware.as_deref() != Some(&fw) || s.patch.as_deref() != Some(&patch) {
                return;
            }
            s.job = Some(r);
            s.checking = false;
        }
        render(&app);
    });
}

#[tauri::command]
fn ready(app: tauri::AppHandle) {
    render(&app);
}

/// Native file pickers: "firmware", "patch", "key", "output".
#[tauri::command]
fn browse(app: tauri::AppHandle, kind: String) {
    use tauri_plugin_dialog::DialogExt;
    let d = app.dialog().file();
    let a = app.clone();
    match kind.as_str() {
        "firmware" => d
            .set_title("Pioneer's official XDJ-RX firmware 2.21 (XDJ-RX_v221.zip or XDJRX.UPD)")
            .add_filter("XDJ-RX firmware", &["zip", "UPD", "upd"])
            .pick_file(move |f| {
                if let Some(p) = f.and_then(|f| f.into_path().ok()) {
                    take_file(&a, p);
                }
            }),
        "patch" => d.set_title("The release's patch file (.xdjpatch)").add_filter("Patch file", &["xdjpatch"]).pick_file(move |f| {
            if let Some(p) = f.and_then(|f| f.into_path().ok()) {
                take_file(&a, p);
            }
        }),
        "key" => d
            .set_title("Your firmware key (aes256.key)")
            .add_filter("Key", &["key", "txt"])
            .add_filter("All files", &["*"])
            .pick_file(move |f| {
                if let Some(p) = f.and_then(|f| f.into_path().ok()) {
                    eval(&a, &format!("setKeyFile({})", js_str(&p.display().to_string())));
                }
            }),
        "output" => {
            let cur = {
                let shared = app.state::<Shared>();
                let s = shared.lock().unwrap();
                s.output.clone().or_else(|| match &s.job {
                    Some(Ok(j)) => Some(j.default_output()),
                    _ => None,
                })
            };
            let mut d = d.set_title("Save the custom firmware as").set_file_name("XDJRX.UPD").add_filter("Firmware", &["UPD"]);
            if let Some(dir) = cur.as_ref().and_then(|p| p.parent()).filter(|d| d.is_dir()) {
                d = d.set_directory(dir);
            }
            d.save_file(move |f| {
                if let Some(p) = f.and_then(|f| f.into_path().ok()) {
                    a.state::<Shared>().lock().unwrap().output = Some(p);
                    render(&a);
                }
            })
        }
        _ => {}
    }
}

/// Build with the user's key (hex or a key file path), or the downloaded one when `key` is empty.
#[tauri::command]
fn build(app: tauri::AppHandle, key: String) -> Result<(), String> {
    let given = if key.trim().is_empty() { None } else { Some(key_from_arg(key.trim()).map_err(|e| format!("Your key: {e}."))?) };
    let (job, out, cached) = {
        let shared = app.state::<Shared>();
        let mut s = shared.lock().unwrap();
        let job = match &s.job {
            Some(Ok(j)) if !s.busy => j.clone(),
            _ => return Err("Choose both files first.".into()),
        };
        s.busy = true;
        s.result = None;
        (job, s.output.clone(), s.key)
    };
    render(&app);
    std::thread::spawn(move || {
        let progress = |pct: f64, step: &str, detail: &str| {
            eval(&app, &format!("progress({pct:.3}, {}, {})", js_str(step), js_str(detail)));
        };
        // a saved key is used only if it opens this firmware (else it is fetched again)
        let key = given.or(cached.filter(|k| job.key_opens(k)));
        // the download is most of the time: 0-70 %, the build 70-100 %
        let (base, span) = if key.is_some() { (0.0, 1.0) } else { (0.7, 0.3) };
        progress(0.0, "Starting…", "");
        let key = match key {
            Some(k) => Ok(k),
            None => {
                let step = "Downloading the firmware key from Pioneer's GPL source code";
                let mut last = -1i64;
                key_from_gpl(&mut |m| {
                    if let Msg::Download(p) = m {
                        let part = p.total.map_or(0.0, |t| p.done as f64 / t.max(1) as f64);
                        let pct = ((p.part - 1) as f64 + part) / p.parts as f64 * base;
                        if (pct * 1000.0) as i64 != last {
                            last = (pct * 1000.0) as i64;
                            let mb = |b: u64| b >> 20;
                            let detail = match p.total {
                                Some(t) => format!("part {} of {}: {} of {} MB", p.part, p.parts, mb(p.done), mb(t)),
                                None => format!("part {} of {}: {} MB", p.part, p.parts, mb(p.done)),
                            };
                            progress(pct, step, &detail);
                        }
                    }
                })
                
                .map_err(|e| format!("Couldn't download the firmware key ({e}). Enter it under \"I have the key\" instead."))
            }
        };
        let steps = ["Opening the official firmware", "Applying the changes", "Building", "Encrypting and verifying"];
        let key_used = key.clone();
        let result = key.and_then(|k| {
            job.build(&k, out.as_deref(), &mut |m| {
                if let Msg::Step(s) = m {
                    let i = steps.iter().position(|x| s.starts_with(x)).unwrap_or(0);
                    progress(base + span * (i as f64 + 0.5) / steps.len() as f64, s.trim_end_matches("..."), "");
                }
            })
        });
        {
            let shared = app.state::<Shared>();
            let mut s = shared.lock().unwrap();
            s.busy = false;
            if let Ok(d) = &result {
                s.result = Some(d.path.clone());
            }
            if let (Ok(_), Ok(k)) = (&result, &key_used) {
                s.key = Some(*k);                // kept for this session and saved for the next
                let _ = save_key(k);
            }
        }
        render(&app);
        match result {
            Ok(d) => eval(
                &app,
                &format!(
                    "done({})",
                    json!({"path": d.path.display().to_string(), "release": d.release, "md5": d.md5})
                ),
            ),
            Err(e) => eval(&app, &format!("failed({})", js_str(&e))),
        }
    });
    Ok(())
}

/// Show the built file in the file manager.
#[tauri::command]
fn reveal(app: tauri::AppHandle) {
    let Some(path) = app.state::<Shared>().lock().unwrap().result.clone() else { return };
    #[cfg(target_os = "macos")]
    let _ = std::process::Command::new("open").arg("-R").arg(&path).spawn();
    #[cfg(windows)]
    let _ = std::process::Command::new("explorer").arg(format!("/select,{}", path.display())).spawn();
    #[cfg(all(unix, not(target_os = "macos")))]
    let _ = std::process::Command::new("xdg-open").arg(path.parent().unwrap_or(Path::new("."))).spawn();
}

fn main() {
    let shared: Shared = Arc::new(Mutex::new(State { key: saved_key(), ..State::default() }));
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .manage(shared)
        .invoke_handler(tauri::generate_handler![ready, browse, build, reveal])
        .setup(|app| {
            WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                .title("XDJ-RX Firmware Patcher")
                .inner_size(640.0, 780.0)
                .min_inner_size(520.0, 700.0)
                .build()?;
            Ok(())
        })
        .on_window_event(|window, event| {
            // the page's drag highlight comes from here (HTML drag events don't fire while
            // the window takes file drops)
            match event {
                WindowEvent::DragDrop(DragDropEvent::Enter { .. }) => eval(window.app_handle(), "dragging(true)"),
                WindowEvent::DragDrop(DragDropEvent::Leave) => eval(window.app_handle(), "dragging(false)"),
                WindowEvent::DragDrop(DragDropEvent::Drop { paths, .. }) => {
                    eval(window.app_handle(), "dragging(false)");
                    for p in paths {
                        take_file(window.app_handle(), p.clone());
                    }
                }
                _ => {}
            }
        })
        .run(tauri::generate_context!())
        .expect("failed to start the XDJ-RX Firmware Patcher");
}
