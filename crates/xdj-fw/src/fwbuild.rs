//! Custom firmware: unpack a `.UPD` into an editable tree, and build a new `.UPD` from
//! a base firmware + an overlay directory + a release number.
//!
//! Overlay layout (every part optional):
//!   <overlay>/iso/...      raw files of the update image (e.g. iso/images/uImage)
//!   <overlay>/rootfs/...   files inside images/rootfs.cramfs  (mtd5, the root fs)
//!   <overlay>/pioneer/...  files inside images/pioneer.tar.gz (mtd7: rbp, apl_start, ...)
//!   <overlay>/gui/... data/... user/...   the other payload tarballs
//! Inside each part, a file replaces the same path (keeping the original's mode and
//! owner) or adds it (host mode, root:root); host symlinks become symlinks; a
//! `.xdj-delete` file lists paths to remove. Untouched entries are carried over with
//! their exact metadata (device nodes, owners, symlinks) -- which is why builds work
//! from the original image + an overlay instead of from an unpacked copy.
//!
//! The release number goes to images/release.txt (what the updater installs as the
//! u-boot env `release`, shown in UTILITY -> VERSION No.) and to the footer model
//! string "XDJAERO<release>" (what check_sig prints as the version).

use std::path::Path;

use crate::tree::Kind;
use crate::{cramfs, iso, tarball, tree};

#[derive(Clone, Copy, PartialEq)]
enum Codec {
    Cramfs,
    TarGz,
}

/// (overlay/unpack dir name, path inside the update image, codec)
const PAYLOADS: [(&str, &str, Codec); 5] = [
    ("rootfs", "images/rootfs.cramfs", Codec::Cramfs),
    ("pioneer", "images/pioneer.tar.gz", Codec::TarGz),
    ("gui", "images/gui.tar.gz", Codec::TarGz),
    ("data", "images/data.tar.gz", Codec::TarGz),
    ("user", "images/user.tar.gz", Codec::TarGz),
];

/// Extract the update image (`<dest>/iso`) and every payload (`<dest>/rootfs`, ...).
/// For reading and as a source of files to copy into an overlay: ownership and
/// special files are listed in each part's `.xdj-metadata`, not recreated.
pub fn unpack(iso_img: &[u8], dest: &Path) -> Result<Vec<String>, String> {
    let t = iso::read_tree(iso_img)?;
    tree::extract(&t, &dest.join("iso"))?;
    let mut report = vec![format!("iso: {} entries", t.nodes.len())];
    for (name, path, codec) in PAYLOADS {
        let Some(n) = t.get(path) else { continue };
        let Kind::File(data) = &n.kind else { continue };
        let pt = match codec {
            Codec::Cramfs => cramfs::read(data)?,
            Codec::TarGz => tarball::read(data)?,
        };
        tree::extract(&pt, &dest.join(name))?;
        report.push(format!("{name}: {} entries ({path})", pt.nodes.len()));
    }
    Ok(report)
}

pub struct BuildOptions<'a> {
    pub overlay: Option<&'a Path>,
    pub release: Option<&'a str>,
    pub model: Option<&'a str>,
}

/// Build a new update image from `base` (decrypted ISO). Returns (iso, model, report).
pub fn build(base: &[u8], o: &BuildOptions) -> Result<(Vec<u8>, String, Vec<String>), String> {
    let mut t = iso::read_tree(base)?;
    let mut report = Vec::new();
    if let Some(ov) = o.overlay {
        if !ov.is_dir() {
            return Err(format!("overlay {} is not a directory", ov.display()));
        }
        let known: Vec<&str> = std::iter::once("iso").chain(PAYLOADS.iter().map(|p| p.0)).collect();
        for e in std::fs::read_dir(ov).map_err(|e| e.to_string())?.flatten() {
            let n = e.file_name().to_string_lossy().into_owned();
            if !known.contains(&n.as_str()) && !n.starts_with('.') && !n.ends_with(".md") {
                return Err(format!("overlay: unknown part {n:?} (expected one of {})", known.join(", ")));
            }
        }
        let raw = ov.join("iso");
        if raw.is_dir() {
            for p in t.apply_overlay(&raw)? {
                report.push(format!("iso: {p}"));
            }
        }
        for (name, path, codec) in PAYLOADS {
            let dir = ov.join(name);
            if !dir.is_dir() {
                continue;
            }
            let Some(Kind::File(data)) = t.get(path).map(|n| n.kind.clone()) else {
                return Err(format!("{path}: not in the base firmware"));
            };
            let (new, touched) = match codec {
                Codec::Cramfs => {
                    let mut pt = cramfs::read(&data)?;
                    let touched = pt.apply_overlay(&dir)?;
                    let vol = String::from_utf8_lossy(&data[48..64]).trim_end_matches('\0').to_string();
                    (cramfs::write(&pt, &vol)?, touched)
                }
                Codec::TarGz => tarball::rewrite(&data, &dir)?,
            };
            for p in touched {
                report.push(format!("{name}: {p}"));
            }
            report.push(format!("{path}: {} -> {} bytes", data.len(), new.len()));
            t.put_file(path, new, 0o444);
        }
    }
    let release = match o.release {
        Some(r) => {
            if !r.chars().all(|c| c.is_ascii_digit() || c == '.') || r.parse::<f64>().is_err() {
                return Err(format!("release {r:?}: expected a number like 2.30 (the updater compares it as a float)"));
            }
            t.put_file("images/release.txt", format!("{r}\n").into_bytes(), 0o444);
            report.push(format!("images/release.txt: {r}"));
            r.to_string()
        }
        None => match t.get("images/release.txt").map(|n| &n.kind) {
            Some(Kind::File(d)) => String::from_utf8_lossy(d).lines().next().unwrap_or("").trim().to_string(),
            _ => String::new(),
        },
    };
    // What the unit can actually load at boot (BOOT_READS, the RAM disk): an image past
    // these limits installs fine and then loses files, or fails to boot.
    let budget = boot_budget(&t)?;
    report.extend(budget.lines.iter().map(|l| format!("boot: {l}")));
    if !budget.errors.is_empty() {
        return Err(format!("the unit couldn't load this firmware at boot:\n  {}", budget.errors.join("\n  ")));
    }
    let model = o.model.map(String::from).unwrap_or_else(|| format!("XDJAERO{release}"));
    if !model.starts_with("XDJAERO") {
        return Err(format!("model {model:?}: the update's check_sig requires the \"XDJAERO\" prefix"));
    }
    let vol = String::from_utf8_lossy(&base[16 * 2048 + 40..16 * 2048 + 72]).trim_end().to_string();
    Ok((iso::write_tree(&t, &vol), model, report))
}

// ---- boot-time limits -------------------------------------------------------------
//
// The unit doesn't load a partition, it loads a fixed number of bytes from it. Those
// lengths are hard-coded in Pioneer's boot path and are smaller than the partitions
// (docs/custom-firmware.md section 2b): whatever lies past them is never read. The
// updater writes and md5-verifies the whole payload either way, so an oversized image
// installs "successfully" and then loses files (or doesn't boot).

/// (path in the update image, bytes the unit reads, where the length comes from)
pub const BOOT_READS: [(&str, usize, &str); 5] = [
    ("images/uImage", 0x50_0000,
     "u-boot `nand read ${loadaddr} <bank> 0x500000` (bootargs_nand_A/B), of a 10 MiB kernel bank"),
    ("images/rootfs.cramfs", 0x140_0000,
     "initramfs `nanddump --length=0x1400000 /dev/mtd5`, of a 30 MiB partition"),
    ("images/pioneer.tar.gz", 0x30_0000,
     "initramfs `nanddump --length=0x300000 /dev/mtd7`, of an 8 MiB partition"),
    ("images/gui.tar.gz", 0x80_0000,
     "apl_start first-boot `nanddump --length=0x800000 /dev/mtd9` (UBI format of gui)"),
    ("images/data.tar.gz", 0x2_0000,
     "apl_start first-boot `nanddump --length=0x20000 /dev/mtd8` (UBI format of data)"),
];
/// The initramfs RAM disk (`mount -t tmpfs -o size=60m`) the root fs and pioneer unpack into.
pub const RAMDISK: usize = 60 << 20;
/// Kept free while the rootfs is copied (the initramfs tools run in the same RAM disk).
pub const RAMDISK_SPARE: usize = 1 << 20;
/// Free RAM disk required after boot: the running system's /tmp, logs and work files
/// live there. Stock 2.21 leaves 21 MiB; 16 MiB is our safety margin, not Pioneer's.
pub const RUNTIME_FREE_MIN: usize = 16 << 20;

pub struct Budget {
    /// One line per limit: usage / limit.
    pub lines: Vec<String>,
    /// Every limit exceeded (empty = the unit can load it).
    pub errors: Vec<String>,
}

/// RAM-disk bytes a tree takes once copied into tmpfs (whole 4 KiB pages per file).
fn tmpfs_bytes(t: &tree::Tree) -> usize {
    t.nodes
        .iter()
        .map(|n| match &n.kind {
            Kind::File(d) => d.len().div_ceil(4096) * 4096,
            _ => 0,
        })
        .sum()
}

fn mib(b: usize) -> String {
    if b < 1 << 20 {
        format!("{:.1} KiB", b as f64 / 1024.0)
    } else {
        format!("{:.2} MiB", b as f64 / (1 << 20) as f64)
    }
}

/// Check an update image (ISO tree) against what the unit loads at boot.
pub fn boot_budget(t: &tree::Tree) -> Result<Budget, String> {
    let mut b = Budget { lines: Vec::new(), errors: Vec::new() };
    let file = |p: &str| match t.get(p).map(|n| &n.kind) {
        Some(Kind::File(d)) => Some(d.clone()),
        _ => None,
    };
    for (path, max, why) in BOOT_READS {
        let Some(d) = file(path) else { continue };
        b.lines.push(format!("{path} {} of {}", mib(d.len()), mib(max)));
        if d.len() > max {
            b.errors.push(format!("{path} is {} ({} bytes): the unit reads only {} ({why})",
                                  mib(d.len()), d.len(), mib(max)));
        }
    }
    // The NAND boot: nanddump writes the full 20 MiB read into the RAM disk, the cramfs is
    // copied out next to it (cpdir), then deleted; pioneer.tar.gz (3 MiB) is unpacked on top.
    let rootfs = match file("images/rootfs.cramfs") {
        Some(d) => tmpfs_bytes(&cramfs::read(&d)?),
        None => return Ok(b),
    };
    let pioneer = match file("images/pioneer.tar.gz") {
        Some(d) => tmpfs_bytes(&tarball::read(&d)?),
        None => 0,
    };
    let rootfs_read = BOOT_READS[1].1;
    let peak = rootfs_read + rootfs;
    b.lines.push(format!("RAM disk while copying the rootfs {} of {} (20 MiB image + {} unpacked)",
                         mib(peak), mib(RAMDISK - RAMDISK_SPARE), mib(rootfs)));
    if peak > RAMDISK - RAMDISK_SPARE {
        b.errors.push(format!(
            "unpacked rootfs is {}: with the 20 MiB image it's copied from, that needs {} of the 60 MiB \
             RAM disk (limit {}, keeping 1 MiB for the initramfs) - the copy would fail and the unit not boot",
            mib(rootfs), mib(peak), mib(RAMDISK - RAMDISK_SPARE)));
    }
    let free = RAMDISK.saturating_sub(rootfs + pioneer);
    b.lines.push(format!("RAM disk free after boot {} (rootfs {} + pioneer {}; at least {})",
                         mib(free), mib(rootfs), mib(pioneer), mib(RUNTIME_FREE_MIN)));
    if free < RUNTIME_FREE_MIN {
        b.errors.push(format!(
            "after boot only {} of the 60 MiB RAM disk would be free (rootfs {} + pioneer {} unpacked); \
             the running system needs at least {} (stock 2.21 leaves 21 MiB)",
            mib(free), mib(rootfs), mib(pioneer), mib(RUNTIME_FREE_MIN)));
    }
    Ok(b)
}

// ---- unpack -> edit -> repack ------------------------------------------------------

/// Payload part names in build order ("iso" first: raw files, then the payloads).
pub fn parts() -> Vec<&'static str> {
    std::iter::once("iso").chain(PAYLOADS.iter().map(|p| p.0)).collect()
}

fn part_tree(iso_tree: &tree::Tree, part: &str) -> Result<Option<tree::Tree>, String> {
    if part == "iso" {
        return Ok(Some(iso_tree.clone()));
    }
    let Some((_, path, codec)) = PAYLOADS.iter().find(|p| p.0 == part) else { return Ok(None) };
    let Some(n) = iso_tree.get(path) else { return Ok(None) };
    let Kind::File(data) = &n.kind else { return Ok(None) };
    Ok(Some(match codec {
        Codec::Cramfs => cramfs::read(data)?,
        Codec::TarGz => tarball::read(data)?,
    }))
}

#[cfg(unix)]
fn host_mode(md: &std::fs::Metadata) -> u32 {
    use std::os::unix::fs::PermissionsExt;
    md.permissions().mode()
}
#[cfg(not(unix))]
fn host_mode(_: &std::fs::Metadata) -> u32 {
    0o644
}

/// Compare an edited `xdj fw unpack` tree (`dir`) with the base image and write the
/// differences as an overlay into `out` (which must not exist). Per part: changed or
/// new regular files are copied, changed/new symlinks recreated, new directories made,
/// and paths that are gone are listed in `.xdj-delete`. An executable-bit change on an
/// existing file is carried as delete + re-add (the re-added file takes the host mode).
/// Owners and special files can't be edited unprivileged and are always kept.
/// Returns the change list ("+path", "~path", "-path", "mode path", "link path").
pub fn diff_unpacked(base: &[u8], dir: &Path, out: Option<&Path>) -> Result<Vec<String>, String> {
    let iso_tree = iso::read_tree(base)?;
    let mut changes = Vec::new();
    if let Some(o) = out {
        if o.exists() {
            return Err(format!("{} already exists", o.display()));
        }
    }
    for part in parts() {
        let hdir = dir.join(part);
        if !hdir.is_dir() {
            continue;
        }
        let Some(bt) = part_tree(&iso_tree, part)? else { continue };
        let odir = out.map(|o| o.join(part));
        let mut deletes: Vec<String> = Vec::new();
        let mut copy = |rel: &str, kind: &str| -> Result<(), String> {
            changes.push(format!("{kind} {part}/{rel}"));
            if let Some(od) = &odir {
                let src = hdir.join(rel);
                let dst = od.join(rel);
                std::fs::create_dir_all(dst.parent().unwrap()).map_err(|e| e.to_string())?;
                let md = std::fs::symlink_metadata(&src).map_err(|e| e.to_string())?;
                if md.file_type().is_symlink() {
                    #[cfg(unix)]
                    std::os::unix::fs::symlink(std::fs::read_link(&src).map_err(|e| e.to_string())?, &dst)
                        .map_err(|e| e.to_string())?;
                } else if md.is_dir() {
                    std::fs::create_dir_all(&dst).map_err(|e| e.to_string())?;
                } else {
                    std::fs::copy(&src, &dst).map_err(|e| format!("{}: {e}", src.display()))?;
                }
            }
            Ok(())
        };
        // host -> base: changed / new
        let mut stack = vec![String::new()];
        while let Some(d) = stack.pop() {
            let full = if d.is_empty() { hdir.clone() } else { hdir.join(&d) };
            let mut ents: Vec<_> = std::fs::read_dir(&full).map_err(|e| e.to_string())?.flatten().collect();
            ents.sort_by_key(|e| e.file_name());
            for e in ents {
                let name = e.file_name().to_string_lossy().into_owned();
                if d.is_empty() && name == ".xdj-metadata" || name == ".DS_Store" {
                    continue;
                }
                let rel = if d.is_empty() { name } else { format!("{d}/{name}") };
                let md = std::fs::symlink_metadata(e.path()).map_err(|e| e.to_string())?;
                let base_node = bt.get(&rel);
                if md.file_type().is_symlink() {
                    let t = std::fs::read_link(e.path()).map_err(|e| e.to_string())?.to_string_lossy().into_owned();
                    match base_node.map(|n| &n.kind) {
                        Some(Kind::Symlink(bt_t)) if *bt_t == t => {}
                        Some(_) => copy(&rel, "link")?,
                        None => copy(&rel, "+")?,
                    }
                } else if md.is_dir() {
                    match base_node.map(|n| &n.kind) {
                        Some(Kind::Dir) => {}
                        Some(_) => {
                            deletes.push(rel.clone());
                            copy(&rel, "+dir")?
                        }
                        None => copy(&rel, "+dir")?,
                    }
                    stack.push(rel);
                } else if md.is_file() {
                    let data = std::fs::read(e.path()).map_err(|e| e.to_string())?;
                    match base_node {
                        Some(n) => match &n.kind {
                            Kind::File(bd) => {
                                let exec_changed = (host_mode(&md) & 0o111 != 0) != (n.mode & 0o111 != 0);
                                if exec_changed {
                                    deletes.push(rel.clone());
                                    copy(&rel, "mode")?;
                                } else if *bd != data {
                                    copy(&rel, "~")?;
                                }
                            }
                            _ => {
                                deletes.push(rel.clone());
                                copy(&rel, "~")?
                            }
                        },
                        None => copy(&rel, "+")?,
                    }
                }
            }
        }
        // base -> host: removed (specials were never extracted; skip them)
        let mut gone_dirs: Vec<String> = Vec::new();
        for n in &bt.nodes {
            if n.path.is_empty() || matches!(n.kind, Kind::Special(_)) {
                continue;
            }
            if gone_dirs.iter().any(|g| n.path.starts_with(&format!("{g}/"))) {
                continue;
            }
            if std::fs::symlink_metadata(hdir.join(&n.path)).is_err() {
                changes.push(format!("- {part}/{}", n.path));
                deletes.push(n.path.clone());
                if matches!(n.kind, Kind::Dir) {
                    gone_dirs.push(n.path.clone());
                }
            }
        }
        if let Some(od) = &odir {
            if !deletes.is_empty() {
                std::fs::create_dir_all(od).map_err(|e| e.to_string())?;
                std::fs::write(od.join(".xdj-delete"), deletes.join("\n") + "\n").map_err(|e| e.to_string())?;
            }
        }
    }
    Ok(changes)
}
