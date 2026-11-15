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
    // The partitions they're flashed to (images/fw_env.txt `partition=`): a payload that
    // doesn't fit would make the updater fail half-way through on the unit.
    for (path, max, part) in [
        ("images/rootfs.cramfs", 30 << 20, "mtd5 rootfs"),
        ("images/pioneer.tar.gz", 8 << 20, "mtd7 pioneer"),
        ("images/uImage", 10 << 20, "kernel_a/kernel_b"),
    ] {
        if let Some(Kind::File(d)) = t.get(path).map(|n| &n.kind) {
            if d.len() > max {
                return Err(format!("{path} is {} bytes: doesn't fit {part} ({} bytes)", d.len(), max));
            }
        }
    }
    let model = o.model.map(String::from).unwrap_or_else(|| format!("XDJAERO{release}"));
    if !model.starts_with("XDJAERO") {
        return Err(format!("model {model:?}: the update's check_sig requires the \"XDJAERO\" prefix"));
    }
    let vol = String::from_utf8_lossy(&base[16 * 2048 + 40..16 * 2048 + 72]).trim_end().to_string();
    Ok((iso::write_tree(&t, &vol), model, report))
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
