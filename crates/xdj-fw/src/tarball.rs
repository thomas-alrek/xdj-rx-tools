//! The firmware's `.tar.gz` payloads (pioneer -> mtd7, data/user, gui): list them as a
//! [`Tree`] for browsing, and rewrite them with an overlay while keeping every
//! untouched entry's header (owner names, mtime, order) exactly as it was.

use std::collections::BTreeMap;
use std::io::{Read, Write};
use std::path::Path;

use crate::tree::{Kind, Node, Tree};

struct Entry {
    header: tar::Header,
    path: String,
    link: Option<String>,
    data: Vec<u8>,
}

fn norm(p: &str) -> String {
    p.trim_start_matches("./").trim_end_matches('/').to_string()
}

fn entries(tgz: &[u8]) -> Result<Vec<Entry>, String> {
    let mut a = tar::Archive::new(flate2::read::GzDecoder::new(tgz));
    let mut out = Vec::new();
    for e in a.entries().map_err(|e| format!("tar: {e}"))? {
        let mut e = e.map_err(|e| format!("tar: {e}"))?;
        let path = e.path().map_err(|e| format!("tar path: {e}"))?.to_string_lossy().into_owned();
        let link = e.link_name().ok().flatten().map(|l| l.to_string_lossy().into_owned());
        let mut data = Vec::new();
        e.read_to_end(&mut data).map_err(|e| format!("tar read: {e}"))?;
        out.push(Entry { header: e.header().clone(), path, link, data });
    }
    Ok(out)
}

/// The archive's contents as a tree (for `xdj fw unpack`).
pub fn read(tgz: &[u8]) -> Result<Tree, String> {
    let mut t = Tree::default();
    t.nodes.push(Node { path: String::new(), mode: 0o40755, uid: 0, gid: 0, mtime: 0, kind: Kind::Dir });
    for e in entries(tgz)? {
        let p = norm(&e.path);
        if p.is_empty() || p == "." {
            continue;
        }
        let h = &e.header;
        let mode = h.mode().unwrap_or(0o644);
        let (uid, gid, mtime) = (h.uid().unwrap_or(0) as u32, h.gid().unwrap_or(0) as u32, h.mtime().unwrap_or(0));
        let (kind, ty) = match h.entry_type() {
            tar::EntryType::Directory => (Kind::Dir, 0o040000),
            tar::EntryType::Symlink => (Kind::Symlink(e.link.clone().unwrap_or_default()), 0o120000),
            tar::EntryType::Char | tar::EntryType::Block | tar::EntryType::Fifo => {
                let ty = match h.entry_type() {
                    tar::EntryType::Char => 0o020000,
                    tar::EntryType::Block => 0o060000,
                    _ => 0o010000,
                };
                let rdev = (h.device_major().ok().flatten().unwrap_or(0) << 8) | h.device_minor().ok().flatten().unwrap_or(0);
                (Kind::Special(rdev), ty)
            }
            _ => (Kind::File(e.data), 0o100000),
        };
        t.nodes.retain(|n| n.path != p);
        t.ensure_parents(&p);
        t.nodes.push(Node { path: p, mode: ty | (mode & 0o7777), uid, gid, mtime, kind });
    }
    t.sort();
    Ok(t)
}

/// Rewrite `tgz` with the overlay `dir` (same rules as [`Tree::apply_overlay`]: files
/// replace or add, `.xdj-delete` removes). Returns (new archive, touched paths).
pub fn rewrite(tgz: &[u8], dir: &Path) -> Result<(Vec<u8>, Vec<String>), String> {
    let mut ents = entries(tgz)?;
    let dot = ents.iter().any(|e| e.path.starts_with("./"));
    // the overlay as a tree of just its own files
    let mut ov = Tree::default();
    ov.nodes.push(Node { path: String::new(), mode: 0o40755, uid: 0, gid: 0, mtime: 0, kind: Kind::Dir });
    let touched = ov.apply_overlay_files_only(dir)?;
    let mut deletes: Vec<String> = Vec::new();
    if let Ok(t) = std::fs::read_to_string(dir.join(".xdj-delete")) {
        for l in t.lines().map(str::trim).filter(|l| !l.is_empty() && !l.starts_with('#')) {
            let p = norm(l.trim_start_matches('/'));
            let before = ents.len();
            ents.retain(|e| {
                let q = norm(&e.path);
                q != p && !q.starts_with(&format!("{p}/"))
            });
            if ents.len() == before {
                return Err(format!("{}: {p} is not in the archive", dir.join(".xdj-delete").display()));
            }
            deletes.push(format!("-{p}"));
        }
    }
    let mut files: BTreeMap<String, &Node> = ov
        .nodes
        .iter()
        .filter(|n| !matches!(n.kind, Kind::Dir))
        .map(|n| (n.path.clone(), n))
        .collect();
    let mut out = tar::Builder::new(Vec::new());
    out.mode(tar::HeaderMode::Complete);
    let mut present: std::collections::HashSet<String> = std::collections::HashSet::new();
    for e in &ents {
        let key = norm(&e.path);
        present.insert(key.clone());
        let mut h = e.header.clone();
        match (files.remove(&key), h.entry_type()) {
            (Some(n), tar::EntryType::Regular) | (Some(n), tar::EntryType::Continuous) => {
                let Kind::File(d) = &n.kind else { return Err(format!("{key}: overlay is not a regular file")) };
                h.set_size(d.len() as u64);
                out.append_data(&mut h, &e.path, d.as_slice()).map_err(|e| e.to_string())?;
            }
            (Some(_), _) => return Err(format!("{key}: overlay replaces a non-regular archive entry")),
            (None, tar::EntryType::Symlink) | (None, tar::EntryType::Link) => {
                out.append_link(&mut h, &e.path, e.link.clone().unwrap_or_default()).map_err(|e| e.to_string())?;
            }
            (None, _) => {
                out.append_data(&mut h, &e.path, e.data.as_slice()).map_err(|e| e.to_string())?;
            }
        }
    }
    // new files (and their missing parent dirs)
    for (path, n) in files {
        let mut parents: Vec<String> = Vec::new();
        let mut p = crate::tree::parent(&path).to_string();
        while !p.is_empty() && !present.contains(&p) {
            parents.push(p.clone());
            p = crate::tree::parent(&p).to_string();
        }
        for d in parents.into_iter().rev() {
            let mut h = tar::Header::new_gnu();
            h.set_entry_type(tar::EntryType::Directory);
            h.set_mode(0o755);
            h.set_mtime(crate::tree::now());
            h.set_size(0);
            let name = if dot { format!("./{d}/") } else { format!("{d}/") };
            out.append_data(&mut h, name, std::io::empty()).map_err(|e| e.to_string())?;
            present.insert(d);
        }
        let name = if dot { format!("./{path}") } else { path.clone() };
        let mut h = tar::Header::new_gnu();
        h.set_mode(n.mode & 0o7777);
        h.set_mtime(n.mtime);
        h.set_uid(0);
        h.set_gid(0);
        let _ = h.set_username("root");
        let _ = h.set_groupname("root");
        match &n.kind {
            Kind::Symlink(t) => {
                h.set_entry_type(tar::EntryType::Symlink);
                h.set_size(0);
                out.append_link(&mut h, name, t).map_err(|e| e.to_string())?;
            }
            Kind::File(d) => {
                h.set_entry_type(tar::EntryType::Regular);
                h.set_size(d.len() as u64);
                out.append_data(&mut h, name, d.as_slice()).map_err(|e| e.to_string())?;
            }
            _ => {}
        }
    }
    let tar_bytes = out.into_inner().map_err(|e| e.to_string())?;
    let mut gz = flate2::write::GzEncoder::new(Vec::new(), flate2::Compression::best());
    gz.write_all(&tar_bytes).map_err(|e| e.to_string())?;
    let mut all = deletes;
    all.extend(touched);
    Ok((gz.finish().map_err(|e| e.to_string())?, all))
}
