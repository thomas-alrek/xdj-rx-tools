//! An in-memory filesystem tree with full POSIX metadata, the common model the ISO,
//! cramfs and tar codecs read into and write from. Paths are relative and
//! '/'-separated; the root directory is the node with path "".

use std::path::Path;

#[derive(Clone, Debug, PartialEq)]
pub enum Kind {
    Dir,
    File(Vec<u8>),
    Symlink(String),
    /// char/block device, fifo or socket (the type is in `mode`); `rdev` for devices.
    Special(u32),
}

#[derive(Clone, Debug, PartialEq)]
pub struct Node {
    pub path: String,
    pub mode: u32,
    pub uid: u32,
    pub gid: u32,
    pub mtime: u64,
    pub kind: Kind,
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct Tree {
    pub nodes: Vec<Node>,
}

pub fn parent(p: &str) -> &str {
    p.rfind('/').map(|i| &p[..i]).unwrap_or("")
}

pub fn name(p: &str) -> &str {
    p.rfind('/').map(|i| &p[i + 1..]).unwrap_or(p)
}

pub const S_IFMT: u32 = 0o170000;
pub const S_IFDIR: u32 = 0o040000;
pub const S_IFREG: u32 = 0o100000;
pub const S_IFLNK: u32 = 0o120000;

impl Tree {
    pub fn sort(&mut self) {
        self.nodes.sort_by(|a, b| a.path.cmp(&b.path));
    }

    pub fn get(&self, path: &str) -> Option<&Node> {
        self.nodes.iter().find(|n| n.path == path)
    }

    pub fn get_mut(&mut self, path: &str) -> Option<&mut Node> {
        self.nodes.iter_mut().find(|n| n.path == path)
    }

    /// Make sure every ancestor directory of `path` exists (0755 root:root).
    pub fn ensure_parents(&mut self, path: &str) {
        let mut p = parent(path).to_string();
        let mut missing = Vec::new();
        loop {
            if self.get(&p).is_some() {
                break;
            }
            missing.push(p.clone());
            if p.is_empty() {
                break;
            }
            p = parent(&p).to_string();
        }
        for m in missing {
            self.nodes.push(Node { path: m, mode: S_IFDIR | 0o755, uid: 0, gid: 0, mtime: 0, kind: Kind::Dir });
        }
    }

    /// Replace or add a regular file. A replaced file keeps its mode/owner/mtime.
    pub fn put_file(&mut self, path: &str, data: Vec<u8>, mode_if_new: u32) {
        if let Some(n) = self.get_mut(path) {
            n.kind = Kind::File(data);
            if n.mode & S_IFMT != S_IFREG {
                n.mode = S_IFREG | (mode_if_new & 0o7777);
            }
            return;
        }
        self.ensure_parents(path);
        self.nodes.push(Node {
            path: path.into(),
            mode: S_IFREG | (mode_if_new & 0o7777),
            uid: 0,
            gid: 0,
            mtime: now(),
            kind: Kind::File(data),
        });
        self.sort();
    }

    /// Remove a path and everything under it. Returns whether anything was removed.
    pub fn remove(&mut self, path: &str) -> bool {
        let before = self.nodes.len();
        let pre = format!("{path}/");
        self.nodes.retain(|n| n.path != path && !n.path.starts_with(&pre));
        self.nodes.len() != before
    }

    /// Apply an overlay directory: every regular file under `dir` replaces (keeping the
    /// original's metadata) or adds (mode from the host file, root:root) the same path;
    /// host symlinks are added as symlinks. A file named `.xdj-delete` lists paths to
    /// remove, one per line. Returns the list of touched paths.
    pub fn apply_overlay(&mut self, dir: &Path) -> Result<Vec<String>, String> {
        let mut touched = Vec::new();
        let del = dir.join(".xdj-delete");
        if let Ok(t) = std::fs::read_to_string(&del) {
            for l in t.lines().map(str::trim).filter(|l| !l.is_empty() && !l.starts_with('#')) {
                let p = l.trim_start_matches("./").trim_start_matches('/');
                if !self.remove(p) {
                    return Err(format!("{}: {p} is not in the payload", del.display()));
                }
                touched.push(format!("-{p}"));
            }
        }
        touched.extend(self.apply_overlay_files_only(dir)?);
        Ok(touched)
    }

    /// The file/dir/symlink part of [`apply_overlay`] (no `.xdj-delete` handling).
    pub fn apply_overlay_files_only(&mut self, dir: &Path) -> Result<Vec<String>, String> {
        let mut touched = Vec::new();
        walk_host(dir, dir, &mut |rel, md, full| {
            if rel == ".xdj-delete" || rel == ".xdj-metadata" || rel.ends_with(".DS_Store") {
                return Ok(());
            }
            #[cfg(unix)]
            let perm = {
                use std::os::unix::fs::PermissionsExt;
                md.permissions().mode()
            };
            #[cfg(not(unix))]
            let perm = 0o644;
            if md.file_type().is_symlink() {
                let t = std::fs::read_link(full).map_err(|e| e.to_string())?;
                self.remove(rel);
                self.ensure_parents(rel);
                self.nodes.push(Node {
                    path: rel.into(),
                    mode: S_IFLNK | 0o777,
                    uid: 0,
                    gid: 0,
                    mtime: now(),
                    kind: Kind::Symlink(t.to_string_lossy().into_owned()),
                });
                self.sort();
            } else if md.is_file() {
                let data = std::fs::read(full).map_err(|e| format!("{}: {e}", full.display()))?;
                self.put_file(rel, data, perm);
            } else if md.is_dir() && self.get(rel).is_none() {
                self.ensure_parents(rel);
                self.nodes.push(Node { path: rel.into(), mode: S_IFDIR | (perm & 0o7777), uid: 0, gid: 0, mtime: now(), kind: Kind::Dir });
                self.sort();
            }
            if !md.is_dir() {
                touched.push(rel.to_string());
            }
            Ok(())
        })?;
        Ok(touched)
    }
}

pub fn now() -> u64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
}

fn walk_host(
    root: &Path,
    dir: &Path,
    f: &mut dyn FnMut(&str, &std::fs::Metadata, &Path) -> Result<(), String>,
) -> Result<(), String> {
    let mut ents: Vec<_> = std::fs::read_dir(dir)
        .map_err(|e| format!("{}: {e}", dir.display()))?
        .filter_map(|e| e.ok())
        .collect();
    ents.sort_by_key(|e| e.file_name());
    for e in ents {
        let full = e.path();
        let md = std::fs::symlink_metadata(&full).map_err(|e| e.to_string())?;
        let rel = full.strip_prefix(root).unwrap().to_string_lossy().replace('\\', "/");
        f(&rel, &md, &full)?;
        if md.is_dir() {
            walk_host(root, &full, f)?;
        }
    }
    Ok(())
}

/// Write a tree onto the host filesystem (for browsing): files, dirs, symlinks.
/// Special files and ownership can't be represented unprivileged; they are listed in
/// `<dest>/.xdj-metadata` (path, mode, uid, gid, type) instead.
pub fn extract(tree: &Tree, dest: &Path) -> Result<(), String> {
    std::fs::create_dir_all(dest).map_err(|e| e.to_string())?;
    let mut meta = String::from("# path\tmode\tuid\tgid\tkind\n");
    for n in &tree.nodes {
        let kind = match &n.kind {
            Kind::Dir => "dir".to_string(),
            Kind::File(_) => "file".into(),
            Kind::Symlink(t) => format!("symlink -> {t}"),
            Kind::Special(r) => format!("special rdev={r:#x}"),
        };
        meta.push_str(&format!("/{}\t{:o}\t{}\t{}\t{kind}\n", n.path, n.mode, n.uid, n.gid));
        if n.path.is_empty() {
            continue;
        }
        let p = dest.join(&n.path);
        match &n.kind {
            Kind::Dir => std::fs::create_dir_all(&p).map_err(|e| format!("{}: {e}", p.display()))?,
            Kind::File(d) => {
                if let Some(par) = p.parent() {
                    std::fs::create_dir_all(par).map_err(|e| e.to_string())?;
                }
                std::fs::write(&p, d).map_err(|e| format!("{}: {e}", p.display()))?;
                #[cfg(unix)]
                {
                    use std::os::unix::fs::PermissionsExt;
                    let _ = std::fs::set_permissions(&p, std::fs::Permissions::from_mode((n.mode & 0o777) | 0o200));
                }
            }
            Kind::Symlink(t) => {
                let _ = std::fs::remove_file(&p);
                #[cfg(unix)]
                std::os::unix::fs::symlink(t, &p).map_err(|e| format!("{}: {e}", p.display()))?;
            }
            Kind::Special(_) => {}
        }
    }
    std::fs::write(dest.join(".xdj-metadata"), meta).map_err(|e| e.to_string())
}
