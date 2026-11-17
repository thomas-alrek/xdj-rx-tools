//! The firmware's AES key, derived from Pioneer's public GPL source release for the XDJ-RX
//! (feature `gpl-key`; used by `xdjrx-patcher` and the desktop app's key prompt).
//!
//! The release (v1.50, ~280 MB) is two deflate64 ZIPs wrapping `pioneer_xdj_rx.tar.gz.00/.01`;
//! the passphrase file `aes256.key` is inside its `initramfs.tar.gz`, and the key is derived
//! from it exactly as the firmware does (`firmware::key_from_passphrase`). Everything happens
//! in memory; nothing but the derived key is kept.

use crate::firmware;
use std::io::Read;

/// Pioneer's "GPL/LGPL Source Code" download for the XDJ-RX.
pub const GPL_PARTS: [&str; 2] = [
    "https://files.microcms-assets.io/assets/3b9e29ce734e49babfedb3f8d1e728e3/18acb46e94654aa1a0bb59265e67dcf9/E58B0262-B827-4181-A226-767EFC34A11E.zip",
    "https://files.microcms-assets.io/assets/3b9e29ce734e49babfedb3f8d1e728e3/3b9db487ff62463dae162b1e25c8c389/99510A92-C1F8-4290-935E-748361FF1782.zip",
];
/// Approximate size of the whole download, for messages.
pub const DOWNLOAD_MB: u64 = 280;
const INITRAMFS_MEMBER: &str = "pioneer_xdj_rx/initramfs.tar.gz";
const KEY_MEMBER: &str = "initramfs/usr/local/pioneer/aes256.key";

/// Download progress: part (1-based), number of parts, bytes so far, total bytes if known.
pub struct Progress {
    pub part: usize,
    pub parts: usize,
    pub done: u64,
    pub total: Option<u64>,
}

fn download(url: &str, part: usize, progress: &mut dyn FnMut(&Progress)) -> Result<Vec<u8>, String> {
    let resp = ureq::get(url)
        .set("User-Agent", "xdj-rx-tools")
        .call()
        .map_err(|e| format!("download failed ({e})"))?;
    let total: Option<u64> = resp.header("Content-Length").and_then(|v| v.parse().ok());
    let mut r = resp.into_reader();
    let mut data = Vec::with_capacity(total.unwrap_or(0) as usize);
    let mut buf = vec![0u8; 1 << 16];
    let mut last = 0u64;
    loop {
        let n = r.read(&mut buf).map_err(|e| format!("download failed ({e})"))?;
        if n == 0 {
            break;
        }
        data.extend_from_slice(&buf[..n]);
        let done = data.len() as u64;
        if done - last >= 1 << 20 {
            last = done;
            progress(&Progress { part, parts: GPL_PARTS.len(), done, total });
        }
    }
    progress(&Progress { part, parts: GPL_PARTS.len(), done: data.len() as u64, total: Some(data.len() as u64) });
    Ok(data)
}

fn tar_member(tgz: &[u8], member: &str) -> Result<Vec<u8>, String> {
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
}

/// Download Pioneer's GPL source release and derive the firmware key from it.
pub fn fetch_key(progress: &mut dyn FnMut(&Progress)) -> Result<[u8; 32], String> {
    let mut tgz = Vec::new();
    for (i, url) in GPL_PARTS.iter().enumerate() {
        let z = download(url, i + 1, progress)?;
        let mut ar = zip::ZipArchive::new(std::io::Cursor::new(z)).map_err(|e| format!("GPL part {}: {e}", i + 1))?;
        let mut f = ar.by_index(0).map_err(|e| format!("GPL part {}: {e}", i + 1))?;
        f.read_to_end(&mut tgz).map_err(|e| format!("GPL part {}: {e}", i + 1))?;
    }
    let initramfs = tar_member(&tgz, INITRAMFS_MEMBER)?;
    let pass = tar_member(&initramfs, KEY_MEMBER)?;
    Ok(firmware::key_from_passphrase(&pass))
}

/// The key as 64 lowercase hex characters (what `firmware::key_from_hex` reads back).
pub fn key_hex(key: &[u8; 32]) -> String {
    key.iter().map(|b| format!("{b:02x}")).collect()
}
