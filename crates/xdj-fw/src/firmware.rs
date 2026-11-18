//! Firmware preparation, in-process: `.UPD` decrypt/pack, kernel boot blobs and the
//! update stick (`xdj fw …` / `xdj prepare` on the command line).
//!
//!   * `.UPD` container: AES-256-CBC per 512-byte sector, IV = LE32(sector) || 0*12,
//!     + a 16-byte footer (12-byte model string, LE32 CRC32 of the body). A Pioneer
//!     `.zip` holds the `.UPD` (deflate or deflate64).
//!   * the plaintext is an ISO9660 image (names stored as-is, `;1` stripped).
//!   * boot blobs for QEMU's loader: the zImage's gzip payload (vmlinux.bin), the
//!     SABRE-SD trampoline (tramp.bin) and the ATAG list (atags.bin).

use std::io::Read;
use std::path::{Path, PathBuf};

use aes::cipher::{BlockDecryptMut, KeyIvInit};

pub const SECTOR: usize = 512;

// ---- the .UPD ---------------------------------------------------------------------

/// The `.UPD` bytes of a firmware file: the file itself, or the single `.UPD` inside a
/// Pioneer `.zip` (any lone file if none is named `.UPD`).
pub fn read_upd_bytes(path: &Path) -> Result<Vec<u8>, String> {
    let data = std::fs::read(path).map_err(|e| format!("{}: {e}", path.display()))?;
    if !data.starts_with(b"PK") {
        return Ok(data);
    }
    let mut zip = zip::ZipArchive::new(std::io::Cursor::new(data))
        .map_err(|e| format!("{}: bad zip: {e}", path.display()))?;
    let files: Vec<String> = zip
        .file_names()
        .filter_map(|n| n.ok().map(|c| c.into_owned()))
        .filter(|n| !n.ends_with('/'))
        .collect();
    let mut cand: Vec<&String> = files.iter().filter(|n| n.to_lowercase().ends_with(".upd")).collect();
    if cand.is_empty() {
        cand = files.iter().collect();
    }
    match cand.len() {
        0 => return Err(format!("no .UPD found inside {}", path.display())),
        1 => {}
        _ => {
            let names: Vec<&str> = cand.iter().map(|s| s.as_str()).collect();
            return Err(format!("multiple candidate files in {}: {}", path.display(), names.join(", ")));
        }
    }
    let name = cand[0].clone();
    let mut f = zip.by_name(&name).map_err(|e| format!("{name}: {e}"))?;
    let mut out = Vec::with_capacity(f.size() as usize);
    f.read_to_end(&mut out).map_err(|e| format!("{name}: {e}"))?;
    Ok(out)
}

/// (encrypted body, model string, footer CRC).
pub fn split_container(data: &[u8]) -> Result<(&[u8], String, u32), String> {
    if data.len() < 16 || (data.len() - 16) % SECTOR != 0 {
        return Err("not an XDJ .UPD (size-16 must be a multiple of 512)".into());
    }
    let body = &data[..data.len() - 16];
    let foot = &data[data.len() - 16..];
    let model_raw = &foot[..12];
    let end = model_raw.iter().position(|&b| b == 0).unwrap_or(12);
    let model = model_raw[..end].iter().map(|&b| b as char).collect();
    let crc = u32::from_le_bytes(foot[12..16].try_into().unwrap());
    Ok((body, model, crc))
}

/// The AES key: 64 hex chars, or a passphrase file the way util-linux `losetup -e aes`
/// derives it (first line, first 31 bytes, NUL-padded to 32).
pub fn key_from_hex(s: &str) -> Option<[u8; 32]> {
    let t = s.trim();
    if t.len() != 64 {
        return None;
    }
    let mut k = [0u8; 32];
    for (i, b) in k.iter_mut().enumerate() {
        *b = u8::from_str_radix(t.get(2 * i..2 * i + 2)?, 16).ok()?;
    }
    Some(k)
}

pub fn key_from_passphrase(raw: &[u8]) -> [u8; 32] {
    let line = raw.split(|&b| b == b'\n').next().unwrap_or(&[]);
    let mut k = [0u8; 32];
    let n = line.len().min(31);
    k[..n].copy_from_slice(&line[..n]);
    k
}

/// The key from a file: 64 hex chars, else a losetup passphrase file.
pub fn key_from_file(path: &Path) -> Result<[u8; 32], String> {
    let raw = std::fs::read(path).map_err(|e| format!("{}: {e}", path.display()))?;
    Ok(key_from_hex(&String::from_utf8_lossy(&raw)).unwrap_or_else(|| key_from_passphrase(&raw)))
}

/// Find the firmware key the way the tools always have: an explicit key file, else
/// `XDJRX_AES_KEY` (64 hex), else `XDJRX_AES_KEY=` in a `.env` in `start` or any parent
/// directory (the repo's, written by fetch_key), else the desktop app's saved key.
pub fn resolve_key(key_file: Option<&Path>, start: &Path) -> Result<[u8; 32], String> {
    if let Some(f) = key_file {
        return key_from_file(f);
    }
    if let Ok(v) = std::env::var("XDJRX_AES_KEY") {
        return key_from_hex(&v).ok_or_else(|| "XDJRX_AES_KEY must be 64 hex characters".to_string());
    }
    let mut dir = Some(start);
    while let Some(d) = dir {
        if let Ok(t) = std::fs::read_to_string(d.join(".env")) {
            for l in t.lines() {
                if let Some((k, v)) = l.trim().split_once('=') {
                    if k.trim() == "XDJRX_AES_KEY" {
                        let v = v.trim().trim_matches('"').trim_matches('\'');
                        return key_from_hex(v).ok_or_else(|| format!("{}: XDJRX_AES_KEY must be 64 hex characters", d.join(".env").display()));
                    }
                }
            }
        }
        dir = d.parent();
    }
    if let Some(k) = stored_key_path().filter(|k| k.is_file()) {
        return key_from_file(&k);
    }
    Err("no firmware key: pass --key-file, set XDJRX_AES_KEY, or run fetch_key (writes .env)".into())
}

/// The saved firmware key, shared by the XDJ-RX tools (the desktop app's key store; the
/// patcher reads and fills it too): macOS `~/Library/Application Support/XDJ-RX/key/aes256.key`,
/// Linux `$XDG_DATA_HOME/xdj-rx/key/aes256.key` (default `~/.local/share`), Windows
/// `%APPDATA%\XDJ-RX\key\aes256.key`. The file holds 64 hex characters (or a losetup
/// passphrase file, as `key_from_file` reads).
pub fn stored_key_path() -> Option<PathBuf> {
    let home = std::env::var_os("HOME").map(PathBuf::from);
    if cfg!(target_os = "macos") {
        return home.map(|h| h.join("Library/Application Support/XDJ-RX/key/aes256.key"));
    }
    if cfg!(windows) {
        return std::env::var_os("APPDATA").map(|a| PathBuf::from(a).join("XDJ-RX").join("key").join("aes256.key"));
    }
    let base = std::env::var_os("XDG_DATA_HOME")
        .map(PathBuf::from)
        .or_else(|| home.map(|h| h.join(".local/share")))?;
    Some(base.join("xdj-rx").join("key").join("aes256.key"))
}

/// The saved key, if there is one and it reads.
pub fn stored_key() -> Option<[u8; 32]> {
    let p = stored_key_path()?;
    if !p.is_file() {
        return None;
    }
    key_from_file(&p).ok()
}

/// Save `key` (as 64 hex characters) where `stored_key` finds it; returns the file.
pub fn store_key(key: &[u8; 32]) -> Result<PathBuf, String> {
    let p = stored_key_path().ok_or("no place to save the key (no home directory)")?;
    if let Some(d) = p.parent() {
        std::fs::create_dir_all(d).map_err(|e| format!("{}: {e}", d.display()))?;
    }
    let hex: String = key.iter().map(|b| format!("{b:02x}")).collect();
    std::fs::write(&p, hex).map_err(|e| format!("{}: {e}", p.display()))?;
    Ok(p)
}

/// Whether `key` opens this encrypted body: decrypts only the first 34 KB (the ISO9660
/// descriptor at 0x8000), so it is a quick check before a full build.
pub fn key_opens(body: &[u8], key: &[u8; 32]) -> bool {
    let n = body.len().min(0x8800) / SECTOR * SECTOR;
    decrypt_body(&body[..n], key).is_ok()
}

/// Decrypt the body and check it is an ISO9660 image (else: wrong key).
pub fn decrypt_body(body: &[u8], key: &[u8; 32]) -> Result<Vec<u8>, String> {
    type Dec = cbc::Decryptor<aes::Aes256>;
    let mut out = body.to_vec();
    for (i, sec) in out.chunks_mut(SECTOR).enumerate() {
        let mut iv = [0u8; 16];
        iv[..4].copy_from_slice(&(i as u32).to_le_bytes());
        let blocks = unsafe {
            // SAFETY: a 512-byte sector is 32 contiguous 16-byte AES blocks.
            std::slice::from_raw_parts_mut(sec.as_mut_ptr() as *mut aes::Block, sec.len() / 16)
        };
        Dec::new(key.into(), &iv.into()).decrypt_blocks_mut(blocks);
    }
    if out.get(0x8001..0x8006) != Some(b"CD001") {
        return Err("wrong key? ISO9660 'CD001' not found at 0x8000".into());
    }
    Ok(out)
}

/// Encrypt an ISO into a `.UPD`: AES-256-CBC per sector (zero-padded to a sector) +
/// the 16-byte footer: model string (12 bytes, NUL-padded; the update's check_sig
/// wants it to start with "XDJAERO" and prints the rest as the version) + LE32
/// CRC32 of the encrypted body. check_sig verifies only that CRC and the model prefix.
pub fn pack(iso: &[u8], key: &[u8; 32], model: &str) -> Result<Vec<u8>, String> {
    use aes::cipher::BlockEncryptMut;
    type Enc = cbc::Encryptor<aes::Aes256>;
    if model.len() > 12 {
        return Err(format!("model string {model:?} longer than 12 bytes"));
    }
    let mut body = iso.to_vec();
    body.resize(iso.len().div_ceil(SECTOR) * SECTOR, 0);
    for (i, sec) in body.chunks_mut(SECTOR).enumerate() {
        let mut iv = [0u8; 16];
        iv[..4].copy_from_slice(&(i as u32).to_le_bytes());
        let blocks = unsafe {
            // SAFETY: a 512-byte sector is 32 contiguous 16-byte AES blocks.
            std::slice::from_raw_parts_mut(sec.as_mut_ptr() as *mut aes::Block, sec.len() / 16)
        };
        Enc::new(key.into(), &iv.into()).encrypt_blocks_mut(blocks);
    }
    let crc = crc32fast::hash(&body);
    let mut m = model.as_bytes().to_vec();
    m.resize(12, 0);
    body.extend(m);
    body.extend(crc.to_le_bytes());
    Ok(body)
}

/// `.UPD`/`.zip` -> the decrypted ISO bytes.
pub fn decrypt_firmware(path: &Path, key: &[u8; 32]) -> Result<(Vec<u8>, String), String> {
    let data = read_upd_bytes(path)?;
    let (body, model, crc) = split_container(&data)?;
    if crc32fast::hash(body) != crc {
        eprintln!("[prep] warning: footer CRC {crc:08x} != crc32(body)");
    }
    Ok((decrypt_body(body, key)?, model))
}

// ---- ISO9660 (see iso.rs) ---------------------------------------------------------

/// Read a file from an ISO9660 image by its path ("/images/uImage").
pub fn iso_read(iso: &[u8], path: &str) -> Result<Vec<u8>, String> {
    crate::iso::read_file(iso, path)
}

/// `k=v` lines of a u-boot env text file (images/fw_env.txt).
pub fn env_lines(text: &[u8]) -> Vec<String> {
    String::from_utf8_lossy(text)
        .lines()
        .map(str::trim)
        .filter(|l| l.contains('='))
        .map(String::from)
        .collect()
}

// ---- kernel boot blobs (`xdj prepare`) ----------------------------------------------

pub const KERNEL_ENTRY: u32 = 0x1000_8000;
pub const ATAGS_ADDR: u32 = 0x1000_0100;
pub const MACH_TYPE_SABRESD: u32 = 3980;
pub const BOARD_REV_SABRESD: u32 = 0x500;
const RAM_BASE: u32 = 0x1000_0000;
const RAM_SIZE: u32 = 0x1000_0000;
/// Kernel command line: console on ttymxc0, the LVDS framebuffer mode line.
pub const CMDLINE: &str = "console=ttymxc0,115200 earlyprintk keep_bootcon \
video=mxcfb0:dev=ldb,800x480M@60,if=RGB24,bpp=32 \
video=mxcfb1:off video=mxcfb2:off video=mxcfb3:off";
pub const MTDPARTS_FALLBACK: &str = "gpmi-nand:3M(boot),1M(bb1),1M(env),10M(kernel_a),10M(kernel_b),\
30M(rootfs),10M(data),8M(pioneer),5M(bb2),10M(core),40M(gui),\
122M(quickboot),5M(bb2),1M(bbt),0x500000@0x0(block_boot),\
0x4400000@0x500000(block_linux),-@0x0(block_all)";

/// The raw kernel: the gzip payload of the zImage inside a uImage.
pub fn decompress_kernel(uimage: &[u8]) -> Result<Vec<u8>, String> {
    let img = uimage.get(64..).ok_or("uImage too short")?;
    let i = img
        .windows(3)
        .position(|w| w == [0x1f, 0x8b, 0x08])
        .ok_or("no gzip payload found in uImage")?;
    let mut out = Vec::new();
    // Trailing zImage bytes after the gzip member are ignored (like zlib's decompressobj).
    let mut d = flate2::read::GzDecoder::new(&img[i..]);
    d.read_to_end(&mut out).map_err(|e| format!("kernel gunzip: {e}"))?;
    Ok(out)
}

pub fn trampoline(mach_type: u32) -> Vec<u8> {
    // mov r0,#0 ; ldr r1,[pc,#8] ; ldr r2,[pc,#8] ; ldr pc,[pc,#8] ; pad ; id ; atags ; entry
    [0xe3a0_0000, 0xe59f_1008, 0xe59f_2008, 0xe59f_f008, 0, mach_type, ATAGS_ADDR, KERNEL_ENTRY]
        .iter()
        .flat_map(|w: &u32| w.to_le_bytes())
        .collect()
}

pub fn atags(cmdline: &str, revision: Option<u32>) -> Vec<u8> {
    let mut v: Vec<u32> = vec![5, 0x5441_0001, 0, 0x1000, 0, 4, 0x5441_0002, RAM_SIZE, RAM_BASE];
    if let Some(r) = revision {
        v.extend([3, 0x5441_0007, r]);
    }
    let mut cmd = cmdline.as_bytes().to_vec();
    cmd.push(0);
    while cmd.len() % 4 != 0 {
        cmd.push(0);
    }
    v.extend([2 + (cmd.len() / 4) as u32, 0x5441_0009]);
    let mut out: Vec<u8> = v.iter().flat_map(|w| w.to_le_bytes()).collect();
    out.extend(cmd);
    out.extend([0u8; 8]); // ATAG_NONE
    out
}

/// mtdparts=<mtdids>:<partition> from a u-boot env, else the 2.21 table.
pub fn mtdparts_from_env(lines: &[String]) -> String {
    let get = |k: &str| {
        lines
            .iter()
            .find_map(|l| l.strip_prefix(&format!("{k}=")).map(|v| v.trim().to_string()))
    };
    match (get("mtdids"), get("partition")) {
        (Some(a), Some(b)) if !a.is_empty() && !b.is_empty() => format!("{a}:{b}"),
        _ => MTDPARTS_FALLBACK.to_string(),
    }
}

/// Write uImage + vmlinux.bin + tramp.bin + atags.bin for the firmware's own kernel.
pub fn write_boot_blobs(iso: &[u8], out: &Path) -> Result<(), String> {
    let w = |n: &str, b: &[u8]| std::fs::write(out.join(n), b).map_err(|e| format!("{n}: {e}"));
    let uimage = iso_read(iso, "/images/uImage")?;
    w("uImage", &uimage)?;
    w("vmlinux.bin", &decompress_kernel(&uimage)?)?;
    let mtd = match iso_read(iso, "/images/fw_env.txt") {
        Ok(t) => mtdparts_from_env(&env_lines(&t)),
        Err(_) => MTDPARTS_FALLBACK.to_string(),
    };
    w("tramp.bin", &trampoline(MACH_TYPE_SABRESD))?;
    w("atags.bin", &atags(&format!("{CMDLINE} mtdparts={mtd}"), Some(BOARD_REV_SABRESD)))
}

// ---- the update stick (`xdj fw stick`) ---------------------------------------------

const ROOT_ENTRIES: usize = 512;
const ROOT_SECTORS: usize = ROOT_ENTRIES * 32 / SECTOR;
const NFATS: usize = 2;
const SLACK_CLUSTERS: usize = 64;

/// A raw FAT16 superfloppy holding `upd` as /XDJRX.UPD (what the update initramfs
/// mounts: /dev/sdX1, else the whole /dev/sdX).
pub fn update_stick(upd: &[u8], mtime: std::time::SystemTime) -> Result<Vec<u8>, String> {
    let (spc, nclus, fat_secs) = [1usize, 2, 4, 8, 16, 32, 64]
        .iter()
        .find_map(|&spc| {
            let need = upd.len().div_ceil(spc * SECTOR) + SLACK_CLUSTERS;
            let n = need.max(4085 + 16);
            (n <= 65524).then(|| (spc, n, ((n + 2) * 2).div_ceil(SECTOR)))
        })
        .ok_or("firmware too big for a FAT16 stick")?;
    let total = 1 + NFATS * fat_secs + ROOT_SECTORS + nclus * spc;
    let (date, time) = dos_datetime(mtime);

    let mut img = vec![0u8; total * SECTOR];
    let bs = &mut img[..SECTOR];
    bs[0..3].copy_from_slice(&[0xEB, 0x3C, 0x90]);
    bs[3..11].copy_from_slice(b"MSWIN4.1");
    bs[11..13].copy_from_slice(&(SECTOR as u16).to_le_bytes());
    bs[13] = spc as u8;
    bs[14..16].copy_from_slice(&1u16.to_le_bytes());
    bs[16] = NFATS as u8;
    bs[17..19].copy_from_slice(&(ROOT_ENTRIES as u16).to_le_bytes());
    bs[19..21].copy_from_slice(&(if total < 0x10000 { total as u16 } else { 0 }).to_le_bytes());
    bs[21] = 0xF8;
    bs[22..24].copy_from_slice(&(fat_secs as u16).to_le_bytes());
    bs[24..26].copy_from_slice(&32u16.to_le_bytes());
    bs[26..28].copy_from_slice(&64u16.to_le_bytes());
    bs[32..36].copy_from_slice(&(if total >= 0x10000 { total as u32 } else { 0 }).to_le_bytes());
    bs[36] = 0x80;
    bs[38] = 0x29;
    let volid = mtime.duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs() as u32).unwrap_or(0);
    bs[39..43].copy_from_slice(&volid.to_le_bytes());
    bs[43..54].copy_from_slice(b"XDJ UPDATE ");
    bs[54..62].copy_from_slice(b"FAT16   ");
    bs[510..512].copy_from_slice(&[0x55, 0xAA]);

    let csize = spc * SECTOR;
    let nfile = upd.len().div_ceil(csize);
    let mut fat = vec![0u8; fat_secs * SECTOR];
    fat[0..4].copy_from_slice(&[0xF8, 0xFF, 0xFF, 0xFF]);
    for i in 0..nfile {
        let c = 2 + i;
        let next: u16 = if i + 1 < nfile { (c + 1) as u16 } else { 0xFFFF };
        fat[c * 2..c * 2 + 2].copy_from_slice(&next.to_le_bytes());
    }
    for k in 0..NFATS {
        let o = (1 + k * fat_secs) * SECTOR;
        img[o..o + fat.len()].copy_from_slice(&fat);
    }
    let root = (1 + NFATS * fat_secs) * SECTOR;
    let dirent = |name: &[u8; 11], attr: u8, cluster: u16, size: u32| {
        let mut e = [0u8; 32];
        e[..11].copy_from_slice(name);
        e[11] = attr;
        e[14..16].copy_from_slice(&time.to_le_bytes());
        e[16..18].copy_from_slice(&date.to_le_bytes());
        e[18..20].copy_from_slice(&date.to_le_bytes());
        e[22..24].copy_from_slice(&time.to_le_bytes());
        e[24..26].copy_from_slice(&date.to_le_bytes());
        e[26..28].copy_from_slice(&cluster.to_le_bytes());
        e[28..32].copy_from_slice(&size.to_le_bytes());
        e
    };
    img[root..root + 32].copy_from_slice(&dirent(b"XDJ UPDATE ", 0x08, 0, 0));
    img[root + 32..root + 64].copy_from_slice(&dirent(
        b"XDJRX   UPD",
        0x20,
        if upd.is_empty() { 0 } else { 2 },
        upd.len() as u32,
    ));
    let data = root + ROOT_SECTORS * SECTOR;
    img[data..data + upd.len()].copy_from_slice(upd);
    Ok(img)
}

/// (date, time) in FAT format, local time.
fn dos_datetime(t: std::time::SystemTime) -> (u16, u16) {
    let secs = t.duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs() as i64).unwrap_or(0);
    #[cfg(unix)]
    unsafe {
        let tt: libc::time_t = secs as libc::time_t;
        let mut tm: libc::tm = std::mem::zeroed();
        if !libc::localtime_r(&tt, &mut tm).is_null() {
            let d = (((tm.tm_year + 1900 - 1980) as u16) << 9) | (((tm.tm_mon + 1) as u16) << 5) | tm.tm_mday as u16;
            let t = ((tm.tm_hour as u16) << 11) | ((tm.tm_min as u16) << 5) | (tm.tm_sec as u16 / 2);
            return (d, t);
        }
    }
    let _ = secs;
    ((1 << 5) | 1, 0) // 1980-01-01
}
