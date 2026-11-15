//! `xdj` -- XDJ-RX firmware tool: `.UPD` decrypt/unpack/build/pack, update sticks,
//! emulator boot blobs and the emulated NAND.

use std::path::{Path, PathBuf};

use xdj_fw::{firmware, fwbuild, iso, nand, tree};

const USAGE: &str = "\
xdj -- XDJ-RX firmware tool

Firmware (.UPD, or a Pioneer .zip holding one):
  xdj fw info    <UPD>                          container, model, CRC, key check
  xdj fw decrypt <UPD> [-o OUT.iso]             decrypted ISO (default build/<name>.iso)
  xdj fw unpack  <UPD> <DIR>                    ISO tree + every payload, for reading
  xdj fw build   <BASE> -o OUT.UPD [--overlay DIR] [--release X.YY] [--model XDJAERO..]
                                                custom firmware from BASE + an overlay
  xdj fw diff    <BASE> <UNPACKED_DIR>          what an edited `fw unpack` tree changes
  xdj fw repack  <BASE> <UNPACKED_DIR> -o OUT.UPD [--release X.YY] [--keep-overlay DIR]
                                                build from an edited unpack tree (diff ->
                                                overlay -> build; untouched metadata kept)
  xdj fw pack    <ISO> -o OUT.UPD [--model XDJAERO2.21]
  xdj fw stick   <UPD> <OUT.img>                FAT16 USB stick with XDJRX.UPD
Emulator boot blobs:
  xdj prepare    <ISO|UPD> [--out DIR]          uImage/vmlinux.bin/tramp.bin/atags.bin
                                                (default: $XDJ_OUT, else build/qemu)
Emulated NAND (raw 2048+64 pages, qemu/xdj/xdj-nand.c):
  xdj nand init  <IMG> (--iso ISO | --upd UPD | --env fw_env.txt)
  xdj nand env   <IMG>                          print the u-boot env
  xdj nand info  <IMG>                          partitions, env summary, kernel banks
  xdj nand boot  <IMG> <DIR>                    u-boot's bootcmd_nand -> QEMU loader blobs

Key: --key-file FILE (64 hex, or a losetup passphrase file), else $XDJRX_AES_KEY, else
XDJRX_AES_KEY= in a .env here or in a parent directory, else the desktop app's key.
Overlay layout for `fw build`: see `xdj fw build --help`.";

const BUILD_HELP: &str = "\
xdj fw build <BASE.UPD|.zip> -o OUT.UPD [--overlay DIR] [--release X.YY] [--model S]

Builds a new update image from the BASE firmware. The overlay directory may contain:
  iso/...      raw files of the update image (e.g. iso/images/uImage)
  rootfs/...   files inside images/rootfs.cramfs  (mtd5, the root filesystem)
  pioneer/...  files inside images/pioneer.tar.gz (mtd7: rbp, apl_start, ...)
  gui/  data/  user/   files inside the other payload tarballs
A file replaces the same path (keeping its mode/owner) or adds it (host mode,
root:root); a `.xdj-delete` file in a part lists paths to remove.
--release sets images/release.txt (the version the unit shows and the updater
compares) and, unless --model is given, the footer model \"XDJAERO<release>\".
Install it like an official update: `xdj fw stick OUT.UPD stick.img`, then boot the
emulator in firmware update mode with the stick on USB1.";

struct Args {
    pos: Vec<String>,
    opts: std::collections::HashMap<String, String>,
}

fn parse(argv: &[String]) -> Result<Args, String> {
    let mut a = Args { pos: vec![], opts: Default::default() };
    let mut i = 0;
    while i < argv.len() {
        let s = &argv[i];
        if s == "-h" || s == "--help" {
            a.opts.insert("help".into(), String::new());
        } else if let Some(k) = s.strip_prefix("--").or_else(|| (s == "-o").then_some("o")) {
            let v = argv.get(i + 1).ok_or_else(|| format!("{s} needs a value"))?;
            a.opts.insert(k.into(), v.clone());
            i += 1;
        } else {
            a.pos.push(s.clone());
        }
        i += 1;
    }
    Ok(a)
}

fn rd(p: &str) -> Result<Vec<u8>, String> {
    std::fs::read(p).map_err(|e| format!("{p}: {e}"))
}

fn wr(p: &Path, b: &[u8]) -> Result<(), String> {
    if let Some(d) = p.parent().filter(|d| !d.as_os_str().is_empty()) {
        std::fs::create_dir_all(d).map_err(|e| e.to_string())?;
    }
    std::fs::write(p, b).map_err(|e| format!("{}: {e}", p.display()))
}

fn key(a: &Args) -> Result<[u8; 32], String> {
    let cwd = std::env::current_dir().map_err(|e| e.to_string())?;
    firmware::resolve_key(a.opts.get("key-file").map(Path::new), &cwd)
}

/// An ISO from either an ISO file or a .UPD/.zip (decrypted).
fn iso_of(a: &Args, p: &str) -> Result<Vec<u8>, String> {
    let b = rd(p)?;
    if b.get(0x8001..0x8006) == Some(b"CD001") {
        return Ok(b);
    }
    Ok(firmware::decrypt_firmware(Path::new(p), &key(a)?)?.0)
}

fn need<'a>(a: &'a Args, i: usize, what: &str) -> Result<&'a str, String> {
    a.pos.get(i).map(|s| s.as_str()).ok_or_else(|| format!("missing {what} (see `xdj --help`)"))
}

fn run(argv: &[String]) -> Result<(), String> {
    let a = parse(argv)?;
    let cmd: Vec<&str> = a.pos.iter().take(2).map(|s| s.as_str()).collect();
    match cmd.as_slice() {
        ["fw", "build", ..] if a.opts.contains_key("help") => println!("{BUILD_HELP}"),
        _ if a.opts.contains_key("help") || a.pos.is_empty() => println!("{USAGE}"),
        ["fw", "info", ..] => {
            let p = need(&a, 2, "<UPD>")?;
            let data = firmware::read_upd_bytes(Path::new(p))?;
            let (body, model, crc) = firmware::split_container(&data)?;
            let calc = crc32fast::hash(body);
            println!("file        : {p}");
            println!("size        : {} bytes ({} sectors + 16-byte footer)", data.len(), body.len() / 512);
            println!("model string: {model:?}  (check_sig: {})", if model.starts_with("XDJAERO") { "prefix ok" } else { "WRONG PREFIX" });
            println!("footer CRC32: {crc:08x}  (crc32(body)={calc:08x}) -> {}", if crc == calc { "OK" } else { "MISMATCH" });
            match key(&a).and_then(|k| firmware::decrypt_body(body, &k)) {
                Ok(iso_img) => {
                    let rel = iso::read_file(&iso_img, "images/release.txt").map(|b| String::from_utf8_lossy(&b).trim().to_string()).unwrap_or_default();
                    println!("key check   : ISO9660 OK; images/release.txt = {rel}");
                }
                Err(e) => println!("key check   : {e}"),
            }
        }
        ["fw", "decrypt", ..] => {
            let p = need(&a, 2, "<UPD>")?;
            let (iso_img, model) = firmware::decrypt_firmware(Path::new(p), &key(&a)?)?;
            let stem = Path::new(p).file_stem().map(|s| s.to_string_lossy().into_owned()).unwrap_or("firmware".into());
            let out = a.opts.get("o").map(PathBuf::from).unwrap_or_else(|| PathBuf::from(format!("build/{stem}.iso")));
            wr(&out, &iso_img)?;
            println!("model {model:?} -> {} ({} bytes, ISO9660 verified)", out.display(), iso_img.len());
        }
        ["fw", "unpack", ..] => {
            let iso_img = iso_of(&a, need(&a, 2, "<UPD>")?)?;
            let dest = PathBuf::from(need(&a, 3, "<DIR>")?);
            for l in fwbuild::unpack(&iso_img, &dest)? {
                println!("  {l}");
            }
            println!("unpacked -> {}/ (iso/, rootfs/, pioneer/, gui/, data/, user/; owners + special files in each .xdj-metadata)", dest.display());
        }
        ["fw", "build", ..] => {
            let base = iso_of(&a, need(&a, 2, "<BASE>")?)?;
            let out = PathBuf::from(a.opts.get("o").ok_or("fw build needs -o OUT.UPD")?);
            let o = fwbuild::BuildOptions {
                overlay: a.opts.get("overlay").map(Path::new),
                release: a.opts.get("release").map(|s| s.as_str()),
                model: a.opts.get("model").map(|s| s.as_str()),
            };
            let (iso_img, model, report) = fwbuild::build(&base, &o)?;
            for l in &report {
                println!("  {l}");
            }
            let upd = firmware::pack(&iso_img, &key(&a)?, &model)?;
            wr(&out, &upd)?;
            println!("built {} ({} bytes, model {model:?})", out.display(), upd.len());
        }
        ["fw", "diff", ..] => {
            let base = iso_of(&a, need(&a, 2, "<BASE>")?)?;
            let ch = fwbuild::diff_unpacked(&base, Path::new(need(&a, 3, "<UNPACKED_DIR>")?), None)?;
            for c in &ch {
                println!("  {c}");
            }
            println!("{} change(s)  (+ new, ~ changed, - removed, link = symlink target, mode = exec bit)", ch.len());
        }
        ["fw", "repack", ..] => {
            let base = iso_of(&a, need(&a, 2, "<BASE>")?)?;
            let dir = PathBuf::from(need(&a, 3, "<UNPACKED_DIR>")?);
            let out = PathBuf::from(a.opts.get("o").ok_or("fw repack needs -o OUT.UPD")?);
            let ov = match a.opts.get("keep-overlay") {
                Some(k) => PathBuf::from(k),
                None => std::env::temp_dir().join(format!("xdj-repack-{}", std::process::id())),
            };
            let _ = if a.opts.contains_key("keep-overlay") { Ok(()) } else { std::fs::remove_dir_all(&ov) };
            let ch = fwbuild::diff_unpacked(&base, &dir, Some(&ov))?;
            for c in &ch {
                println!("  {c}");
            }
            if ch.is_empty() && !a.opts.contains_key("release") {
                println!("  (no changes)");
            }
            std::fs::create_dir_all(&ov).map_err(|e| e.to_string())?;
            let o = fwbuild::BuildOptions {
                overlay: Some(&ov),
                release: a.opts.get("release").map(|s| s.as_str()),
                model: a.opts.get("model").map(|s| s.as_str()),
            };
            let built = fwbuild::build(&base, &o);
            if !a.opts.contains_key("keep-overlay") {
                let _ = std::fs::remove_dir_all(&ov);
            }
            let (iso_img, model, report) = built?;
            for l in report.iter().filter(|l| l.contains("bytes") || l.starts_with("images/release")) {
                println!("  {l}");
            }
            let upd = firmware::pack(&iso_img, &key(&a)?, &model)?;
            wr(&out, &upd)?;
            println!("built {} ({} bytes, model {model:?})", out.display(), upd.len());
        }
        ["fw", "pack", ..] => {
            let iso_img = rd(need(&a, 2, "<ISO>")?)?;
            if iso_img.get(0x8001..0x8006) != Some(b"CD001") {
                return Err("not an ISO9660 image".into());
            }
            let model = a.opts.get("model").cloned().unwrap_or("XDJAERO2.21".into());
            let out = PathBuf::from(a.opts.get("o").ok_or("fw pack needs -o OUT.UPD")?);
            let upd = firmware::pack(&iso_img, &key(&a)?, &model)?;
            wr(&out, &upd)?;
            println!("packed -> {} ({} bytes, model {model:?})", out.display(), upd.len());
        }
        ["fw", "stick", ..] => {
            let p = need(&a, 2, "<UPD>")?;
            let out = PathBuf::from(need(&a, 3, "<OUT.img>")?);
            let upd = firmware::read_upd_bytes(Path::new(p))?;
            firmware::split_container(&upd)?;
            let img = firmware::update_stick(&upd, std::time::SystemTime::now())?;
            wr(&out, &img)?;
            println!("{}: FAT16 stick, {} MiB, XDJRX.UPD = {} bytes", out.display(), img.len() >> 20, upd.len());
        }
        ["prepare", ..] => {
            let p = a.pos.get(1).cloned().unwrap_or_else(|| "build/XDJRX.iso".into());
            let iso_img = iso_of(&a, &p)?;
            let out = a.opts.get("out").cloned().or_else(|| std::env::var("XDJ_OUT").ok()).unwrap_or("build/qemu".into());
            std::fs::create_dir_all(&out).map_err(|e| e.to_string())?;
            firmware::write_boot_blobs(&iso_img, Path::new(&out))?;
            println!("boot blobs (uImage, vmlinux.bin, tramp.bin, atags.bin) -> {out}/");
        }
        ["nand", "init", ..] => {
            let img = PathBuf::from(need(&a, 2, "<IMG>")?);
            let env = if let Some(e) = a.opts.get("env") {
                firmware::env_lines(&rd(e)?)
            } else {
                let src = a.opts.get("iso").or(a.opts.get("upd")).ok_or("nand init needs --iso, --upd or --env")?;
                firmware::env_lines(&iso::read_file(&iso_of(&a, src)?, "images/fw_env.txt")?)
            };
            nand::init(&img, &env)?;
            println!("{}: erased 256 MiB NAND, factory env ({} vars) in mtd2", img.display(), env.len());
        }
        ["nand", "env", ..] => {
            let env = nand::env(Path::new(need(&a, 2, "<IMG>")?))?.ok_or("env: bad CRC (blank or corrupt env partition)")?;
            for (k, v) in env {
                println!("{k}={v}");
            }
        }
        ["nand", "info", ..] => {
            let p = need(&a, 2, "<IMG>")?;
            match nand::env(Path::new(p))? {
                None => println!("env: blank/invalid"),
                Some(env) => {
                    let g = |k: &str| env.iter().rev().find(|(kk, _)| kk == k).map(|(_, v)| v.as_str()).unwrap_or("-");
                    for k in ["rootfs", "kernel_bank", "boot_type", "update_status", "release", "ubifs_formatted", "rev_pioneer", "rev_panelcom"] {
                        println!("{k:16} {}", g(k));
                    }
                    println!("installed        {}", nand::installed(Path::new(p)));
                }
            }
        }
        ["nand", "boot", ..] => {
            nand::boot(Path::new(need(&a, 2, "<IMG>")?), Path::new(need(&a, 3, "<DIR>")?))?;
        }
        _ => return Err(format!("unknown command {:?} (see `xdj --help`)", a.pos.join(" "))),
    }
    Ok(())
}

fn main() {
    let argv: Vec<String> = std::env::args().skip(1).collect();
    if let Err(e) = run(&argv) {
        eprintln!("xdj: {e}");
        std::process::exit(1);
    }
    let _ = tree::now;
}
