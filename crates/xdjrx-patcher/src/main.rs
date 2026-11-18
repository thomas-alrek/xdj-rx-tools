//! xdjrx-patcher: Pioneer's official XDJ-RX firmware 2.21 + a patch file (.xdjpatch) ->
//! the custom firmware release (.UPD), verified by MD5.
//!
//!   xdjrx-patcher ORIGINAL.UPD PATCH.xdjpatch [--key KEY] [-o OUT.UPD]
//!
//! ORIGINAL is the official XDJRX.UPD (or the zip Pioneer distributes it in). The patch holds
//! only the release's own files and line edits; the patcher rebuilds the release from the
//! original exactly as `tools/fw/build-release.sh` does and refuses to write anything whose
//! MD5 isn't the one the patch records. The firmware's AES key: --key (64 hex characters, or a
//! file: aes256.key or a file holding the hex key), else $XDJRX_AES_KEY, else the saved key
//! (shared with the XDJ-RX desktop app, `saved_key_path`) if it opens the firmware, else derived
//! from Pioneer's public GPL source release (downloaded). A key that built the release is saved
//! there. The work is in lib.rs (shared with the GUI).
//! SPDX-License-Identifier: GPL-2.0-or-later

use std::io::Write;
use std::path::{Path, PathBuf};
use xdjrx_patcher::{key_from_arg, key_from_gpl, save_key, saved_key, saved_key_path, Job, Msg, DOWNLOAD_MB};

const USAGE: &str = "\
xdjrx-patcher - make XDJ-RX custom firmware from Pioneer's official firmware

  xdjrx-patcher ORIGINAL.UPD PATCH.xdjpatch [--key KEY] [-o OUT.UPD]

  ORIGINAL.UPD    Pioneer's official XDJ-RX firmware 2.21 (XDJRX.UPD, or the downloaded .zip)
  PATCH.xdjpatch  the release's patch file
  --key KEY       optional: the firmware key (64 hex characters, or a key file). Without it,
                  the saved key is used (from an earlier run or the XDJ-RX emulator app), else
                  it is taken from Pioneer's public GPL source download (~280 MB) and saved.
  -o OUT.UPD      where to write the result (default: a folder next to ORIGINAL)

Run without arguments to be asked for the files.";

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let interactive = args.is_empty();
    let r = if interactive { run_interactive() } else { run_args(&args) };
    match r {
        Ok(()) => {
            if interactive {
                pause();
            }
        }
        Err(e) => {
            eprintln!("\nERROR: {e}");
            if interactive {
                pause();
            }
            std::process::exit(1);
        }
    }
}

fn pause() {
    print!("\nPress Enter to close.");
    let _ = std::io::stdout().flush();
    let mut s = String::new();
    let _ = std::io::stdin().read_line(&mut s);
}

fn ask(prompt: &str) -> String {
    print!("{prompt}");
    let _ = std::io::stdout().flush();
    let mut s = String::new();
    let _ = std::io::stdin().read_line(&mut s);
    // a path dropped onto a terminal may arrive quoted or with escaped spaces
    let s = s.trim().trim_matches(|c| c == '"' || c == '\'').replace("\\ ", " ");
    s
}

fn run_interactive() -> Result<(), String> {
    println!("xdjrx-patcher - XDJ-RX custom firmware from Pioneer's official firmware\n");
    println!("You need Pioneer's official XDJ-RX firmware 2.21 (XDJRX.UPD or the .zip) and the");
    println!("release's patch file (.xdjpatch). Drag each file into this window, then press Enter.\n");
    let orig = PathBuf::from(ask("Official firmware (XDJRX.UPD or .zip): "));
    let patch = PathBuf::from(ask("Patch file (.xdjpatch): "));
    patch_firmware(&orig, &patch, None, None)
}

fn run_args(args: &[String]) -> Result<(), String> {
    let (mut pos, mut key, mut out) = (Vec::new(), None, None);
    let mut it = args.iter();
    while let Some(a) = it.next() {
        match a.as_str() {
            "-h" | "--help" => {
                println!("{USAGE}");
                return Ok(());
            }
            "--key" => key = Some(it.next().ok_or("--key needs a value")?.clone()),
            "-o" | "--output" => out = Some(PathBuf::from(it.next().ok_or("-o needs a path")?)),
            _ if a.starts_with('-') => return Err(format!("unknown option {a}\n\n{USAGE}")),
            _ => pos.push(PathBuf::from(a)),
        }
    }
    if pos.len() != 2 {
        return Err(format!("expected ORIGINAL.UPD and PATCH.xdjpatch\n\n{USAGE}"));
    }
    patch_firmware(&pos[0], &pos[1], key.as_deref(), out.as_deref())
}

// ---- the key ----------------------------------------------------------------------

/// Progress lines for the key download and the build steps.
fn printer() -> impl FnMut(Msg) {
    let mut last_mb = u64::MAX;
    move |m| match m {
        Msg::Step(s) => println!("{s}"),
        Msg::Download(p) => {
            let mb = p.done >> 20;
            let end = Some(p.done) == p.total;
            if mb / 5 != last_mb / 5 || end {
                last_mb = mb;
                match p.total {
                    Some(t) => print!("\r  part {} of {}: {mb} of {} MB   ", p.part, p.parts, t >> 20),
                    None => print!("\r  part {} of {}: {mb} MB   ", p.part, p.parts),
                }
                if end {
                    println!();
                }
                let _ = std::io::stdout().flush();
            }
        }
    }
}

fn get_key(given: Option<&str>, job: &Job) -> Result<[u8; 32], String> {
    if let Some(k) = given {
        return key_from_arg(k).map_err(|e| format!("--key: {e}"));
    }
    if let Ok(k) = std::env::var("XDJRX_AES_KEY") {
        return key_from_arg(k.trim()).map_err(|e| format!("$XDJRX_AES_KEY: {e}"));
    }
    let path = saved_key_path().map(|p| p.display().to_string()).unwrap_or_default();
    if let Some(k) = saved_key() {
        if job.key_opens(&k) {
            println!("Firmware key: the saved one ({path})");
            return Ok(k);
        }
        println!("The saved key ({path}) doesn't open this firmware - getting it again.");
    }
    println!("(a one-time download of ~{DOWNLOAD_MB} MB; the key is saved for next time)");
    match key_from_gpl(&mut printer()) {
        Ok(k) => Ok(k),
        Err(e) => {
            println!("\nCouldn't get the key automatically: {e}");
            loop {
                let k = ask("Enter the key (64 hex characters) or drag the key file here (empty = give up): ");
                if k.is_empty() {
                    return Err("no firmware key".into());
                }
                match key_from_arg(&k) {
                    Ok(key) => return Ok(key),
                    Err(e) => println!("  {e}"),
                }
            }
        }
    }
}

// ---- patching ---------------------------------------------------------------------

fn patch_firmware(orig: &Path, patch_path: &Path, key: Option<&str>, out: Option<&Path>) -> Result<(), String> {
    let job = Job::open(orig, patch_path)?;
    println!("Patch: custom firmware {} for the XDJ-RX (from official {})", job.release(), job.input_release());
    println!("Official firmware: OK ({})", orig.display());
    let key = get_key(key, &job)?;
    let done = job.build(&key, out, &mut printer())?;
    match save_key(&key) {
        Ok(Some(p)) => println!("Saved the firmware key for next time: {}", p.display()),
        Ok(None) => {}
        Err(e) => println!("(couldn't save the firmware key: {e})"),
    }
    println!("\nDone: {}", done.path.display());
    println!("  custom firmware {}, MD5 {} (verified)", done.release, done.md5);
    println!("Copy XDJRX.UPD to the root of a FAT32 USB stick and update the XDJ-RX as described in the release notes.");
    Ok(())
}
