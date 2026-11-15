//! The unit's NAND image (was qemu/xdj_nand.py, which stays as the dev tool; keep the
//! two in step). The image is the raw flash qemu/xdj/xdj-nand.c stores: 131072 pages
//! of 2048+64 bytes, page-major, OOB included, in the i.MX6 GPMI/BCH layout:
//!
//!   raw page = [10-byte metadata][512 data0][13 ecc0][512 data1][13 ecc1]...[512 data3]
//!              [13 ecc3][2 spare]     (BCH t=8, GF13, 4 chunks; parity written as zeros)
//!   block-mark swap: logical byte 1999 <-> metadata[0].  Erased = all 0xff.

use std::fs::{File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::Path;

use crate::firmware;

const PAGE: usize = 2048;
const OOB: usize = 64;
const RAW: usize = PAGE + OOB;
const PPB: usize = 64;
const BLOCKS: usize = 2048;
const PAGES: usize = PPB * BLOCKS;
pub const SIZE: u64 = (PAGES * RAW) as u64;
const ERASE: usize = PAGE * PPB;

const META: usize = 10;
const CHUNK: usize = 512;
const NCHUNK: usize = 4;
const GF: usize = 13;
const STRENGTH: usize = ((OOB - META) * 8 / (GF * NCHUNK)) & !1; // 8
const ECC_BITS: usize = STRENGTH * GF; // 104
const BBM_BYTE: usize = (PAGE * 8 - (ECC_BITS * (NCHUNK - 1) + META * 8)) / 8; // 1999
const ENV_PART: &str = "env";
const ENV_SIZE: usize = 0x20000;

/// Byte offset of data chunk `i` inside a raw page.
const fn chunk_pos(i: usize) -> usize {
    META + i * (CHUNK + ECC_BITS / 8)
}

fn raw_to_page(raw: &[u8]) -> Vec<u8> {
    if raw.iter().all(|&b| b == 0xff) {
        return vec![0xff; PAGE];
    }
    let mut pay = Vec::with_capacity(PAGE);
    for i in 0..NCHUNK {
        pay.extend_from_slice(&raw[chunk_pos(i)..chunk_pos(i) + CHUNK]);
    }
    pay[BBM_BYTE] = raw[0]; // undo the block-mark swap
    pay
}

fn page_to_raw(data: &[u8]) -> Vec<u8> {
    let mut pay = data.to_vec();
    pay.resize(PAGE, 0xff);
    let mut raw = vec![0xffu8; RAW];
    raw[..META].fill(0xff);
    raw[0] = pay[BBM_BYTE];
    pay[BBM_BYTE] = 0xff;
    for i in 0..NCHUNK {
        let p = chunk_pos(i);
        raw[p..p + CHUNK].copy_from_slice(&pay[i * CHUNK..(i + 1) * CHUNK]);
        raw[p + CHUNK..p + CHUNK + ECC_BITS / 8].fill(0);
    }
    raw
}

pub struct Nand {
    f: File,
}

impl Nand {
    pub fn open(path: &Path) -> Result<Nand, String> {
        let f = OpenOptions::new().read(true).write(true).open(path)
            .map_err(|e| format!("{}: {e}", path.display()))?;
        let len = f.metadata().map_err(|e| e.to_string())?.len();
        if len < SIZE {
            return Err(format!("{}: not a {SIZE}-byte XDJ NAND image", path.display()));
        }
        Ok(Nand { f })
    }

    /// A factory-fresh chip: every page erased.
    pub fn create(path: &Path) -> Result<Nand, String> {
        let mut f = OpenOptions::new().read(true).write(true).create(true).truncate(true).open(path)
            .map_err(|e| format!("{}: {e}", path.display()))?;
        let blk = vec![0xffu8; PPB * RAW];
        for _ in 0..BLOCKS {
            f.write_all(&blk).map_err(|e| format!("{}: {e}", path.display()))?;
        }
        Ok(Nand { f })
    }

    fn raw_block(&mut self, block: usize) -> Result<Vec<u8>, String> {
        let mut b = vec![0u8; PPB * RAW];
        self.f.seek(SeekFrom::Start((block * PPB * RAW) as u64)).map_err(|e| e.to_string())?;
        self.f.read_exact(&mut b).map_err(|e| e.to_string())?;
        Ok(b)
    }

    /// Like u-boot `nand read` / nanddump --bb=skipbad: logical bytes, bad blocks skipped.
    pub fn read_range(&mut self, off: usize, length: usize) -> Result<Vec<u8>, String> {
        let (mut out, mut block, skip) = (Vec::new(), off / ERASE, off % ERASE);
        while out.len() < skip + length && block < BLOCKS {
            let raw = self.raw_block(block)?;
            block += 1;
            if raw[PAGE] != 0xff {
                continue; // factory bad-block mark in the first page's OOB
            }
            for pg in 0..PPB {
                out.extend(raw_to_page(&raw[pg * RAW..(pg + 1) * RAW]));
            }
        }
        out.drain(..skip.min(out.len()));
        out.truncate(length);
        Ok(out)
    }

    /// Erase the covered blocks and program `data` at a block-aligned offset.
    pub fn write_range(&mut self, off: usize, data: &[u8]) -> Result<(), String> {
        assert_eq!(off % ERASE, 0);
        for (i, chunk) in data.chunks(ERASE).enumerate() {
            let block = off / ERASE + i;
            let mut raw = vec![0xffu8; PPB * RAW];
            for (pg, page) in chunk.chunks(PAGE).enumerate() {
                raw[pg * RAW..(pg + 1) * RAW].copy_from_slice(&page_to_raw(page));
            }
            self.f.seek(SeekFrom::Start((block * PPB * RAW) as u64)).map_err(|e| e.to_string())?;
            self.f.write_all(&raw).map_err(|e| e.to_string())?;
        }
        Ok(())
    }
}

// ---- partitions / env --------------------------------------------------------------

/// 'gpmi-nand:3M(boot),...,-@0x0(all)' -> [(name, offset, size)] like cmdlinepart.
fn parse_mtdparts(s: &str) -> Vec<(String, usize, usize)> {
    let s = match s.split_once(':') {
        Some((head, rest)) if !head.contains('(') => rest,
        _ => s,
    };
    let num = |t: &str| -> Option<usize> {
        let (body, mul) = match t.chars().last()?.to_ascii_lowercase() {
            'k' => (&t[..t.len() - 1], 1 << 10),
            'm' => (&t[..t.len() - 1], 1 << 20),
            'g' => (&t[..t.len() - 1], 1 << 30),
            _ => (t, 1),
        };
        let v = match body.strip_prefix("0x") {
            Some(h) => usize::from_str_radix(h, 16).ok()?,
            None => body.parse().ok()?,
        };
        Some(v * mul)
    };
    let (mut parts, mut off) = (Vec::new(), 0usize);
    for item in s.split(',') {
        let Some((spec, rest)) = item.split_once('(') else { continue };
        let Some(name) = rest.strip_suffix(')') else { continue };
        let (size_s, at) = match spec.split_once('@') {
            Some((a, b)) => (a, Some(b)),
            None => (spec, None),
        };
        if let Some(a) = at {
            match num(a) { Some(v) => off = v, None => continue }
        }
        let size = if size_s == "-" { PAGE * PAGES - off } else { match num(size_s) { Some(v) => v, None => continue } };
        parts.push((name.to_string(), off, size));
        off += size;
    }
    parts
}

fn env_offset(mtdparts: &str) -> usize {
    parse_mtdparts(mtdparts)
        .into_iter()
        .find(|(n, _, _)| n == ENV_PART)
        .map(|(_, o, _)| o)
        .unwrap_or(0x40_0000)
}

fn env_blob(entries: &[String]) -> Vec<u8> {
    let mut data: Vec<u8> = entries.join("\0").bytes().collect();
    data.extend([0, 0]);
    data.resize(ENV_SIZE - 4, 0xff);
    let mut out = crc32fast::hash(&data).to_le_bytes().to_vec();
    out.extend(data);
    out
}

/// The u-boot env (ordered k=v), or None if blank/corrupt.
pub fn read_env(nand: &mut Nand) -> Result<Option<Vec<(String, String)>>, String> {
    let blob = nand.read_range(env_offset(firmware::MTDPARTS_FALLBACK), ENV_SIZE)?;
    let crc = u32::from_le_bytes(blob[..4].try_into().unwrap());
    if crc32fast::hash(&blob[4..]) != crc {
        return Ok(None);
    }
    let mut env = Vec::new();
    for e in blob[4..].split(|&b| b == 0) {
        if e.is_empty() {
            break;
        }
        let s: String = e.iter().map(|&b| b as char).collect();
        let (k, v) = s.split_once('=').unwrap_or((&s, ""));
        env.push((k.to_string(), v.to_string()));
    }
    Ok(Some(env))
}

fn get<'a>(env: &'a [(String, String)], k: &str) -> Option<&'a str> {
    env.iter().rev().find(|(kk, _)| kk == k).map(|(_, v)| v.as_str())
}

/// Erased NAND + the factory u-boot env (the firmware's images/fw_env.txt) in mtd2.
pub fn init(path: &Path, fw_env: &[String]) -> Result<(), String> {
    let mut nand = Nand::create(path)?;
    let env: Vec<(String, String)> = fw_env
        .iter()
        .filter_map(|l| l.split_once('=').map(|(a, b)| (a.to_string(), b.to_string())))
        .collect();
    let mtd = match (get(&env, "mtdids"), get(&env, "partition")) {
        (Some(a), Some(b)) => format!("{a}:{b}"),
        _ => firmware::MTDPARTS_FALLBACK.to_string(),
    };
    nand.write_range(env_offset(&mtd), &env_blob(fw_env))
}

/// Has the firmware's own update installed a system? Its last step (finEnv) writes
/// `release=` into the env, which the factory env lacks.
pub fn installed(path: &Path) -> bool {
    let Ok(mut nand) = Nand::open(path) else { return false };
    matches!(read_env(&mut nand), Ok(Some(env)) if get(&env, "release").is_some_and(|v| !v.trim().is_empty()))
}

fn expand(s: &str, env: &[(String, String)]) -> String {
    let mut out = String::new();
    let mut rest = s;
    while let Some(i) = rest.find("${") {
        out.push_str(&rest[..i]);
        match rest[i + 2..].find('}') {
            Some(j) => {
                out.push_str(get(env, &rest[i + 2..i + 2 + j]).unwrap_or(""));
                rest = &rest[i + 2 + j + 1..];
            }
            None => {
                out.push_str(&rest[i..]);
                rest = "";
            }
        }
    }
    out.push_str(rest);
    out
}

/// bootargs as `run bootargs_base bootargs_nand` builds them.
fn uboot_bootargs(env: &[(String, String)]) -> String {
    let mut env = env.to_vec();
    for var in ["bootargs_base", "bootargs_nand"] {
        let cmd = get(&env, var).unwrap_or("").to_string();
        if let Some(v) = cmd.strip_prefix("setenv bootargs ") {
            let b = expand(v, &env);
            env.push(("bootargs".into(), b));
        }
    }
    get(&env, "bootargs").unwrap_or("").to_string()
}

/// The env's bootargs + what the emulator's display/console need: u-boot's bare
/// `video=mxcfb0` is replaced by the emulator's LVDS mode line.
fn emulator_cmdline(bootargs: &str) -> String {
    let emu: Vec<&str> = firmware::CMDLINE.split_whitespace().collect();
    let mut out: Vec<&str> = emu[..3].to_vec();
    out.extend(bootargs.split_whitespace().filter(|a| !a.starts_with("video=") && !a.starts_with("console=")));
    out.extend(emu.iter().filter(|a| a.starts_with("video=")));
    out.join(" ")
}

/// u-boot's bootcmd_nand, host-side: kernel_bank -> that bank's uImage -> vmlinux.bin +
/// atags.bin (bootargs from the env) + tramp.bin in `out`.
pub fn boot(path: &Path, out: &Path) -> Result<(), String> {
    let mut nand = Nand::open(path)?;
    let env = read_env(&mut nand)?.ok_or("no valid u-boot env in the NAND image")?;
    let bank = get(&env, "kernel_bank").unwrap_or("A").to_string();
    let spec = get(&env, &format!("bootargs_nand_{bank}")).unwrap_or("").to_string();
    // 'nand read ${loadaddr} <off> <len>'
    let w: Vec<&str> = spec.split_whitespace().collect();
    let pos = w.windows(2).position(|p| p == ["nand", "read"]).ok_or(format!("env has no usable bootargs_nand_{bank}"))?;
    let parse = |t: &str| t.strip_prefix("0x").map_or(t.parse().ok(), |h| usize::from_str_radix(h, 16).ok());
    let (off, len) = match (w.get(pos + 3).and_then(|t| parse(t)), w.get(pos + 4).and_then(|t| parse(t))) {
        (Some(o), Some(l)) => (o, l),
        _ => return Err(format!("env has no usable bootargs_nand_{bank}")),
    };
    let img = nand.read_range(off, len)?;
    if img.len() < 64 || u32::from_be_bytes(img[..4].try_into().unwrap()) != 0x2705_1956 {
        return Err(format!("kernel bank {bank} (0x{off:x}) holds no uImage"));
    }
    let size = u32::from_be_bytes(img[12..16].try_into().unwrap()) as usize;
    let uimage = &img[..(64 + size).min(img.len())];
    std::fs::create_dir_all(out).map_err(|e| e.to_string())?;
    let wr = |n: &str, b: &[u8]| std::fs::write(out.join(n), b).map_err(|e| format!("{n}: {e}"));
    wr("uImage", uimage)?;
    wr("vmlinux.bin", &firmware::decompress_kernel(uimage)?)?;
    wr("tramp.bin", &firmware::trampoline(firmware::MACH_TYPE_SABRESD))?;
    let cmdline = emulator_cmdline(&uboot_bootargs(&env));
    wr("atags.bin", &firmware::atags(&cmdline, Some(firmware::BOARD_REV_SABRESD)))?;
    eprintln!("[host] kernel bank {bank} @0x{off:x}: uImage {} bytes; cmdline: {cmdline}", uimage.len());
    Ok(())
}
