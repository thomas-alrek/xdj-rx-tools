//! The patching itself, shared by the `xdjrx-patcher` CLI and the `xdjrx-patcher-gui` app:
//! Pioneer's official XDJ-RX firmware 2.21 + a patch file (.xdjpatch) -> the custom firmware
//! release (.UPD), rebuilt exactly as `tools/fw/build-release.sh` does and verified by MD5.
//!
//!   let job = Job::open(original, patch)?;      // reads the patch, checks the official MD5
//!   let key = key_from_gpl(&mut on)?;            // or key_from_arg(hex or key file)
//!   let done = job.build(&key, out, &mut on)?;   // writes only on an MD5 match
//!
//! SPDX-License-Identifier: GPL-2.0-or-later

use md5::{Digest as _, Md5};
use sha2::Sha256;
use std::collections::BTreeMap;
use std::io::Read;
use std::path::{Path, PathBuf};
use xdj_fw::{firmware, fwbuild, gplkey};

pub use xdj_fw::gplkey::{Progress, DOWNLOAD_MB};

/// What a running step reports.
pub enum Msg<'a> {
    /// A step starts (one short sentence).
    Step(&'a str),
    /// The key download moved on.
    Download(&'a Progress),
}

/// A finished build.
pub struct Done {
    pub path: PathBuf,
    pub release: String,
    pub md5: String,
}

/// The patch file and the checked official firmware, ready to build.
pub struct Job {
    patch: Patch,
    upd: Vec<u8>,
    orig: PathBuf,
}

impl Job {
    /// Read the patch and the official firmware (.UPD or Pioneer's .zip) and check the
    /// firmware's MD5 against the one the patch was made for.
    pub fn open(orig: &Path, patch_path: &Path) -> Result<Job, String> {
        let patch = Patch::read(patch_path)?;
        let upd = firmware::read_upd_bytes(orig).map_err(|e| format!("{}: {e}", orig.display()))?;
        if md5_hex(&upd) != patch.m["input"]["upd_md5"].as_str().unwrap_or("") {
            return Err(format!(
                "{} is not Pioneer's official XDJ-RX firmware {} (MD5 mismatch). Use the unmodified file from Pioneer's website.",
                orig.display(),
                patch.input_release()
            ));
        }
        Ok(Job { patch, upd, orig: orig.to_path_buf() })
    }

    /// The release this patch builds (e.g. "3.00").
    pub fn release(&self) -> &str {
        self.patch.s("release")
    }

    /// The official release the patch applies to (e.g. "2.21").
    pub fn input_release(&self) -> &str {
        self.patch.input_release()
    }

    /// Where the result goes by default: a folder "XDJ-RX <release>" next to the original.
    pub fn default_output(&self) -> PathBuf {
        default_output(&self.orig, self.release())
    }

    /// Build the release with `key` and write it to `out` (default: `default_output()`),
    /// only if its MD5 is the one the patch records.
    pub fn build(&self, key: &[u8; 32], out: Option<&Path>, on: &mut dyn FnMut(Msg)) -> Result<Done, String> {
        let patch = &self.patch;
        let release = self.release().to_string();
        // the release's fixed timestamp for new/changed files (xdj_fw::tree::now): byte-identical output
        let epoch = patch.m["source_date_epoch"].as_u64().ok_or("manifest: source_date_epoch")?;
        std::env::set_var("SOURCE_DATE_EPOCH", epoch.to_string());

        on(Msg::Step("Opening the official firmware..."));
        let (body, _model, _crc) = firmware::split_container(&self.upd)?;
        let iso = firmware::decrypt_body(body, key).map_err(|e| format!("the key doesn't open this firmware ({e})"))?;

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
                on(Msg::Step("Applying the changes..."));
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
            on(Msg::Step(&format!("Building custom firmware {release}...")));
            let o = fwbuild::BuildOptions { overlay: Some(&ov), release: Some(&release), model: None };
            let (new_iso, model, _report) = fwbuild::build(&iso, &o)?;
            on(Msg::Step("Encrypting and verifying..."));
            firmware::pack(&new_iso, key, &model)
        })();
        let _ = std::fs::remove_dir_all(&tmp);
        let new_upd = result?;

        let md5 = md5_hex(&new_upd);
        let want = patch.s("output_md5");
        if md5 != want {
            return Err(format!("the result's MD5 is {md5}, expected {want} - nothing written"));
        }
        let path = out.map(Path::to_path_buf).unwrap_or_else(|| self.default_output());
        write_file(&path, &new_upd)?;
        Ok(Done { path, release, md5 })
    }
}

/// The default output for an original firmware file and a release.
pub fn default_output(orig: &Path, release: &str) -> PathBuf {
    orig.parent().unwrap_or(Path::new(".")).join(format!("XDJ-RX {release}")).join("XDJRX.UPD")
}

// ---- the key ----------------------------------------------------------------------

/// A key given by the user: 64 hex characters, or a key file (aes256.key or a file holding
/// the hex key).
pub fn key_from_arg(k: &str) -> Result<[u8; 32], String> {
    if let Some(key) = firmware::key_from_hex(k) {
        return Ok(key);
    }
    let p = Path::new(k);
    if p.is_file() {
        return firmware::key_from_file(p);
    }
    Err("expected 64 hex characters or a key file".into())
}

/// The key, from Pioneer's public GPL source release (~280 MB download).
pub fn key_from_gpl(on: &mut dyn FnMut(Msg)) -> Result<[u8; 32], String> {
    on(Msg::Step("Getting the firmware key from Pioneer's public GPL source code..."));
    gplkey::fetch_key(&mut |p| on(Msg::Download(p)))
}

// ---- helpers ------------------------------------------------------------------------

fn md5_hex(b: &[u8]) -> String {
    hex(&Md5::digest(b))
}

fn sha256_hex(b: &[u8]) -> String {
    hex(&Sha256::digest(b))
}

fn hex(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02x}")).collect()
}

fn write_file(p: &Path, data: &[u8]) -> Result<(), String> {
    if let Some(d) = p.parent() {
        std::fs::create_dir_all(d).map_err(|e| format!("{}: {e}", d.display()))?;
    }
    std::fs::write(p, data).map_err(|e| format!("{}: {e}", p.display()))
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
            let mut e = e.map_err(|e| format!("{}: not a patch file ({e})", path.display()))?;
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

    fn input_release(&self) -> &str {
        self.m["input"]["release"].as_str().unwrap_or("?")
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
