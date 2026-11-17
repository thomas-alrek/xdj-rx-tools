//! xdjrx-patcher: Pioneer's official XDJ-RX firmware 2.21 + a patch file (.xdjpatch) ->
//! the custom firmware release (.UPD), verified by MD5.
//!
//!   xdjrx-patcher ORIGINAL.UPD PATCH.xdjpatch [--key KEY] [-o OUT.UPD]
//!
//! ORIGINAL is the official XDJRX.UPD (or the zip Pioneer distributes it in). The patch holds
//! only the release's own files and line edits; the patcher rebuilds the release from the
//! original exactly as `tools/fw/build-release.sh` does and refuses to write anything whose
//! MD5 isn't the one the patch records. The firmware's AES key is derived from Pioneer's
//! public GPL source release (downloaded) unless given with --key (64 hex characters, or a
//! file: aes256.key or a file holding the hex key).
//! SPDX-License-Identifier: GPL-2.0-or-later

use md5::{Digest as _, Md5};
use sha2::Sha256;
use std::collections::BTreeMap;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use xdj_fw::{firmware, fwbuild};

/// Pioneer's "GPL/LGPL Source Code" release for the XDJ-RX (v1.50): two deflate64 ZIPs
/// wrapping pioneer_xdj_rx.tar.gz.00/.01; the key file is inside initramfs.tar.gz.
const GPL_PARTS: [&str; 2] = [
    "https://files.microcms-assets.io/assets/3b9e29ce734e49babfedb3f8d1e728e3/18acb46e94654aa1a0bb59265e67dcf9/E58B0262-B827-4181-A226-767EFC34A11E.zip",
    "https://files.microcms-assets.io/assets/3b9e29ce734e49babfedb3f8d1e728e3/3b9db487ff62463dae162b1e25c8c389/99510A92-C1F8-4290-935E-748361FF1782.zip",
];
const INITRAMFS_MEMBER: &str = "pioneer_xdj_rx/initramfs.tar.gz";
const KEY_MEMBER: &str = "initramfs/usr/local/pioneer/aes256.key";

const USAGE: &str = "\
xdjrx-patcher - make XDJ-RX custom firmware from Pioneer's official firmware

  xdjrx-patcher ORIGINAL.UPD PATCH.xdjpatch [--key KEY] [-o OUT.UPD]

  ORIGINAL.UPD    Pioneer's official XDJ-RX firmware 2.21 (XDJRX.UPD, or the downloaded .zip)
  PATCH.xdjpatch  the release's patch file
  --key KEY       optional: the firmware key (64 hex characters, or a key file). Without it,
                  the key is taken from Pioneer's public GPL source download (~280 MB).
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

fn md5_hex(b: &[u8]) -> String {
    hex(&Md5::digest(b))
}

fn sha256_hex(b: &[u8]) -> String {
    hex(&Sha256::digest(b))
}

fn hex(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02x}")).collect()
}

// ---- the patch file ---------------------------------------------------------------

struct Patch {
    m: serde_json::Value,
    files: BTreeMap<String, Vec<u8>>,
}

impl Patch {
    fn read(path: &Path) -> Result<Patch, String> {
        let f = std::fs::File::open(path).map_err(|e| format!("{}: {e}", path.display()))?;
        let mut ar = tar::Archive::new(f);
        let (mut m, mut files) = (None, BTreeMap::new());
        for e in ar.entries().map_err(|e| format!("{}: not a patch file ({e})", path.display()))? {
            let mut e = e.map_err(|e| e.to_string())?;
            let name = e.path().map_err(|e| e.to_string())?.to_string_lossy().into_owned();
            let mut data = Vec::new();
            e.read_to_end(&mut data).map_err(|e| e.to_string())?;
            if name == "manifest.json" {
                m = Some(serde_json::from_slice::<serde_json::Value>(&data).map_err(|e| format!("manifest: {e}"))?);
            } else {
                files.insert(name, data);
            }
        }
        let m = m.ok_or_else(|| format!("{}: not a patch file (no manifest)", path.display()))?;
        if m["format"].as_u64() != Some(1) {
            return Err("this patch needs a newer version of xdjrx-patcher".into());
        }
        Ok(Patch { m, files })
    }

    fn s(&self, k: &str) -> &str {
        self.m[k].as_str().unwrap_or("")
    }
}

/// Line edits (see tools/fw/make-patch.py): lines split after each '\n'; text is latin-1.
fn apply_edits(old: &[u8], ops: &[serde_json::Value]) -> Result<Vec<u8>, String> {
    let latin1 = |s: &str| -> Result<Vec<u8>, String> {
        s.chars().map(|c| u8::try_from(c as u32).map_err(|_| "edit text outside latin-1".to_string())).collect()
    };
    let mut lines: Vec<Vec<u8>> = Vec::new();
    let mut start = 0;
    for (i, &b) in old.iter().enumerate() {
        if b == b'\n' {
            lines.push(old[start..=i].to_vec());
            start = i + 1;
        }
    }
    if start < old.len() {
        lines.push(old[start..].to_vec());
    }
    let mut before: BTreeMap<usize, Vec<u8>> = BTreeMap::new();
    for op in ops {
        match op["op"].as_str() {
            Some("insert_lines") => {
                let at = op["before"].as_u64().ok_or("bad edit")? as usize;
                let e = before.entry(at).or_default();
                for l in op["lines"].as_array().ok_or("bad edit")? {
                    e.extend(latin1(l.as_str().ok_or("bad edit")?)?);
                }
            }
            Some("insert_text") => {
                let n = op["line"].as_u64().ok_or("bad edit")? as usize;
                let at = op["at"].as_u64().ok_or("bad edit")? as usize;
                let l = lines.get_mut(n).ok_or("edit beyond the end of the file")?;
                if at > l.len() {
                    return Err("edit beyond the end of a line".into());
                }
                let t = latin1(op["text"].as_str().ok_or("bad edit")?)?;
                l.splice(at..at, t);
            }
            _ => return Err("unknown edit (this patch needs a newer xdjrx-patcher)".into()),
        }
    }
    let mut out = Vec::new();
    for (i, l) in lines.iter().enumerate() {
        if let Some(b) = before.get(&i) {
            out.extend(b);
        }
        out.extend(l);
    }
    if let Some(b) = before.get(&lines.len()) {
        out.extend(b);
    }
    Ok(out)
}

// ---- the key ----------------------------------------------------------------------

fn key_from_arg(k: &str) -> Result<[u8; 32], String> {
    if let Some(key) = firmware::key_from_hex(k) {
        return Ok(key);
    }
    let p = Path::new(k);
    if p.is_file() {
        return firmware::key_from_file(p);
    }
    Err("--key: expected 64 hex characters or a key file".into())
}

fn download(url: &str, what: &str) -> Result<Vec<u8>, String> {
    let resp = ureq::get(url)
        .set("User-Agent", "xdjrx-patcher")
        .call()
        .map_err(|e| format!("download failed ({e})"))?;
    let total: Option<u64> = resp.header("Content-Length").and_then(|v| v.parse().ok());
    let mut r = resp.into_reader();
    let mut data = Vec::new();
    let mut buf = vec![0u8; 1 << 16];
    let mut last = 0;
    loop {
        let n = r.read(&mut buf).map_err(|e| format!("download failed ({e})"))?;
        if n == 0 {
            break;
        }
        data.extend_from_slice(&buf[..n]);
        let mb = data.len() >> 20;
        if mb >= last + 5 {
            last = mb;
            match total {
                Some(t) => print!("\r  {what}: {mb} of {} MB", t >> 20),
                None => print!("\r  {what}: {mb} MB"),
            }
            let _ = std::io::stdout().flush();
        }
    }
    println!("\r  {what}: done ({} MB)          ", data.len() >> 20);
    Ok(data)
}

/// The key, from Pioneer's public GPL source release.
fn key_from_gpl() -> Result<[u8; 32], String> {
    println!("Getting the firmware key from Pioneer's public GPL source download (~280 MB)...");
    let mut tgz = Vec::new();
    for (i, url) in GPL_PARTS.iter().enumerate() {
        let z = download(url, &format!("part {} of {}", i + 1, GPL_PARTS.len()))?;
        let mut ar = zip::ZipArchive::new(std::io::Cursor::new(z)).map_err(|e| format!("GPL part {}: {e}", i + 1))?;
        let mut f = ar.by_index(0).map_err(|e| format!("GPL part {}: {e}", i + 1))?;
        f.read_to_end(&mut tgz).map_err(|e| format!("GPL part {}: {e}", i + 1))?;
    }
    let find = |tgz: &[u8], member: &str| -> Result<Vec<u8>, String> {
        let mut ar = tar::Archive::new(flate2::read::GzDecoder::new(tgz));
        for e in ar.entries().map_err(|e| e.to_string())? {
            let mut e = e.map_err(|e| e.to_string())?;
            if e.path().map_err(|e| e.to_string())?.to_string_lossy() == member {
                let mut d = Vec::new();
                e.read_to_end(&mut d).map_err(|e| e.to_string())?;
                return Ok(d);
            }
        }
        Err(format!("{member} not found in the GPL source"))
    };
    let initramfs = find(&tgz, INITRAMFS_MEMBER)?;
    let pass = find(&initramfs, KEY_MEMBER)?;
    Ok(firmware::key_from_passphrase(&pass))
}

fn get_key(given: Option<&str>) -> Result<[u8; 32], String> {
    if let Some(k) = given {
        return key_from_arg(k);
    }
    if let Ok(k) = std::env::var("XDJRX_AES_KEY") {
        return key_from_arg(k.trim());
    }
    match key_from_gpl() {
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
    let patch = Patch::read(patch_path)?;
    let release = patch.s("release").to_string();
    // the release's fixed timestamp for new/changed files (xdj_fw::tree::now): byte-identical output
    let epoch = patch.m["source_date_epoch"].as_u64().ok_or("manifest: source_date_epoch")?;
    std::env::set_var("SOURCE_DATE_EPOCH", epoch.to_string());
    let input = &patch.m["input"];
    println!("Patch: custom firmware {release} for the XDJ-RX (from official {})", input["release"].as_str().unwrap_or("?"));

    // the official firmware
    let upd = firmware::read_upd_bytes(orig).map_err(|e| format!("{}: {e}", orig.display()))?;
    if md5_hex(&upd) != input["upd_md5"].as_str().unwrap_or("") {
        return Err(format!(
            "{} is not Pioneer's official XDJ-RX firmware {} (MD5 mismatch). Use the unmodified file from Pioneer's website.",
            orig.display(),
            input["release"].as_str().unwrap_or("")
        ));
    }
    println!("Official firmware: OK ({})", orig.display());

    let key = get_key(key)?;
    let (body, _model, _crc) = firmware::split_container(&upd)?;
    let iso = firmware::decrypt_body(body, &key).map_err(|e| format!("the key doesn't open this firmware ({e})"))?;
    println!("Firmware key: OK");

    // the overlay: our files, Pioneer's files with our edits
    let tmp = std::env::temp_dir().join(format!("xdjrx-patcher-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&tmp);
    let result = (|| -> Result<Vec<u8>, String> {
        let ov = tmp.join("overlay");
        let mut modes: BTreeMap<String, Vec<String>> = BTreeMap::new();
        for f in patch.m["files"].as_array().ok_or("manifest: files")? {
            let (part, path) = (f["part"].as_str().unwrap_or(""), f["path"].as_str().unwrap_or(""));
            let data = patch.files.get(&format!("files/{part}/{path}")).ok_or_else(|| format!("patch: {part}/{path} missing"))?;
            if sha256_hex(data) != f["sha256"].as_str().unwrap_or("") {
                return Err(format!("patch: {part}/{path} is damaged"));
            }
            write_file(&ov.join(part).join(path), data)?;
            modes.entry(part.to_string()).or_default().push(format!("{path} {}", f["mode"].as_str().unwrap_or("0644")));
        }
        for (part, lines) in &modes {
            write_file(&ov.join(part).join(".xdj-modes"), (lines.join("\n") + "\n").as_bytes())?;
        }
        if patch.m["edits"].as_array().map_or(false, |e| !e.is_empty()) {
            let unpacked = tmp.join("unpacked");
            fwbuild::unpack(&iso, &unpacked)?;
            for e in patch.m["edits"].as_array().unwrap() {
                let (part, path) = (e["part"].as_str().unwrap_or(""), e["path"].as_str().unwrap_or(""));
                let old = std::fs::read(unpacked.join(part).join(path)).map_err(|_| format!("official firmware: {part}/{path} missing"))?;
                if sha256_hex(&old) != e["base_sha256"].as_str().unwrap_or("") {
                    return Err(format!("official firmware: {part}/{path} differs from what the patch expects"));
                }
                let new = apply_edits(&old, e["ops"].as_array().ok_or("manifest: ops")?)?;
                if sha256_hex(&new) != e["result_sha256"].as_str().unwrap_or("") {
                    return Err(format!("patching {part}/{path} gave an unexpected result"));
                }
                write_file(&ov.join(part).join(path), &new)?;
            }
        }
        println!("Building custom firmware {release}...");
        let o = fwbuild::BuildOptions { overlay: Some(&ov), release: Some(&release), model: None };
        let (new_iso, model, _report) = fwbuild::build(&iso, &o)?;
        firmware::pack(&new_iso, &key, &model)
    })();
    let _ = std::fs::remove_dir_all(&tmp);
    let new_upd = result?;

    let md5 = md5_hex(&new_upd);
    let want = patch.s("output_md5");
    if md5 != want {
        return Err(format!("the result's MD5 is {md5}, expected {want} - nothing written"));
    }
    let out = match out {
        Some(p) => p.to_path_buf(),
        None => orig.parent().unwrap_or(Path::new(".")).join(format!("XDJ-RX {release}")).join("XDJRX.UPD"),
    };
    write_file(&out, &new_upd)?;
    println!("\nDone: {}", out.display());
    println!("  custom firmware {release}, MD5 {md5} (verified)");
    println!("Copy XDJRX.UPD to the root of a FAT32 USB stick and update the XDJ-RX as described in the release notes.");
    Ok(())
}

fn write_file(p: &Path, data: &[u8]) -> Result<(), String> {
    if let Some(d) = p.parent() {
        std::fs::create_dir_all(d).map_err(|e| format!("{}: {e}", d.display()))?;
    }
    std::fs::write(p, data).map_err(|e| format!("{}: {e}", p.display()))
}
