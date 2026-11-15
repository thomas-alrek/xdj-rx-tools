//! cramfs (the XDJ-RX root filesystem, `images/rootfs.cramfs` -> mtd5): read into a
//! [`Tree`] and write one back, mkcramfs-compatible.
//!
//! Layout (little-endian, Linux `cramfs_fs.h`): 64-byte superblock (magic 0x28cd3d45,
//! size, flags = FSID_VERSION_2|SORTED_DIRS, "Compressed ROMFS", fsid{crc, edition,
//! blocks, files}, name) + the root inode at 64. Inode = 12 bytes: mode:16 uid:16 /
//! size:24 gid:8 / namelen:6 (x4 bytes) offset:26 (x4 bytes), then the NUL-padded name.
//! A directory's offset points at its children's inodes (sorted by name), size = their
//! total length; a file's offset points at a table of u32 block-end pointers followed
//! by the zlib-compressed 4 KiB pages; a symlink stores its target the same way; a
//! device stores rdev in `size`. The crc covers the whole image with the crc field 0.

use std::io::{Read, Write};

use crate::tree::{Kind, Node, Tree, S_IFDIR, S_IFLNK, S_IFMT, S_IFREG};

const MAGIC: u32 = 0x28cd_3d45;
const PAGE: usize = 4096;
const FLAG_V2: u32 = 0x1;
const FLAG_SORTED: u32 = 0x2;

fn u32_at(d: &[u8], o: usize) -> Result<u32, String> {
    d.get(o..o + 4).map(|b| u32::from_le_bytes(b.try_into().unwrap())).ok_or_else(|| "cramfs truncated".to_string())
}

pub fn read(img: &[u8]) -> Result<Tree, String> {
    if u32_at(img, 0)? != MAGIC {
        return Err("not a cramfs image".into());
    }
    if img.get(16..32) != Some(b"Compressed ROMFS") {
        return Err("bad cramfs signature".into());
    }
    let mut tree = Tree::default();
    let (mode, uid, size, gid, _nl, off) = inode(img, 64)?;
    tree.nodes.push(Node { path: String::new(), mode, uid, gid, mtime: 0, kind: Kind::Dir });
    read_dir(img, off, size, "", &mut tree, 0)?;
    tree.sort();
    Ok(tree)
}

fn inode(img: &[u8], at: usize) -> Result<(u32, u32, u32, u32, usize, usize), String> {
    let (w0, w1, w2) = (u32_at(img, at)?, u32_at(img, at + 4)?, u32_at(img, at + 8)?);
    Ok((w0 & 0xffff, w0 >> 16, w1 & 0xff_ffff, w1 >> 24, ((w2 & 0x3f) * 4) as usize, ((w2 >> 6) * 4) as usize))
}

fn read_dir(img: &[u8], off: usize, size: u32, prefix: &str, tree: &mut Tree, depth: usize) -> Result<(), String> {
    if depth > 64 {
        return Err("cramfs nesting too deep".into());
    }
    let (mut p, end) = (off, off + size as usize);
    while p < end {
        let (mode, uid, size, gid, nl, doff) = inode(img, p)?;
        let raw = img.get(p + 12..p + 12 + nl).ok_or("cramfs name truncated")?;
        let name = String::from_utf8_lossy(raw.split(|&b| b == 0).next().unwrap_or(&[])).into_owned();
        p += 12 + nl;
        let path = if prefix.is_empty() { name } else { format!("{prefix}/{name}") };
        let kind = match mode & S_IFMT {
            S_IFDIR => {
                tree.nodes.push(Node { path: path.clone(), mode, uid, gid, mtime: 0, kind: Kind::Dir });
                if size > 0 {
                    read_dir(img, doff, size, &path, tree, depth + 1)?;
                }
                continue;
            }
            S_IFREG => Kind::File(file_data(img, doff, size as usize)?),
            S_IFLNK => Kind::Symlink(String::from_utf8_lossy(&file_data(img, doff, size as usize)?).into_owned()),
            _ => Kind::Special(size),
        };
        tree.nodes.push(Node { path, mode, uid, gid, mtime: 0, kind });
    }
    Ok(())
}

fn file_data(img: &[u8], off: usize, size: usize) -> Result<Vec<u8>, String> {
    if size == 0 {
        return Ok(Vec::new());
    }
    let n = size.div_ceil(PAGE);
    let mut start = off + n * 4;
    let mut out = Vec::with_capacity(size);
    for i in 0..n {
        let end = u32_at(img, off + i * 4)? as usize;
        let comp = img.get(start..end).ok_or("cramfs block outside the image")?;
        let mut d = flate2::read::ZlibDecoder::new(comp);
        d.read_to_end(&mut out).map_err(|e| format!("cramfs inflate: {e}"))?;
        start = end;
    }
    out.truncate(size);
    Ok(out)
}

fn inode_bytes(n: &Node, size: u32, offset: usize) -> Result<Vec<u8>, String> {
    let name = crate::tree::name(&n.path).as_bytes();
    let nl = name.len().div_ceil(4) * 4;
    if nl > 252 {
        return Err(format!("{}: name too long for cramfs", n.path));
    }
    if size > 0xff_ffff {
        return Err(format!("{}: too big for cramfs (16 MiB max)", n.path));
    }
    if n.uid > 0xffff || n.gid > 0xff {
        return Err(format!("{}: uid/gid out of cramfs range", n.path));
    }
    let mut b = Vec::with_capacity(12 + nl);
    b.extend_from_slice(&((n.mode & 0xffff) | (n.uid << 16)).to_le_bytes());
    b.extend_from_slice(&(size | (n.gid << 24)).to_le_bytes());
    b.extend_from_slice(&(((nl / 4) as u32) | (((offset / 4) as u32) << 6)).to_le_bytes());
    b.extend_from_slice(name);
    b.resize(12 + nl, 0);
    Ok(b)
}

fn compress_file(data: &[u8], base: usize) -> Vec<u8> {
    let n = data.len().div_ceil(PAGE);
    let mut blocks = Vec::with_capacity(n);
    for page in data.chunks(PAGE) {
        let mut e = flate2::write::ZlibEncoder::new(Vec::new(), flate2::Compression::best());
        e.write_all(page).unwrap();
        blocks.push(e.finish().unwrap());
    }
    let mut out = Vec::new();
    let mut pos = base + n * 4;
    for b in &blocks {
        pos += b.len();
        out.extend_from_slice(&(pos as u32).to_le_bytes());
    }
    for b in blocks {
        out.extend(b);
    }
    out
}

/// Build a cramfs image from `tree` (name = the 16-byte volume name).
pub fn write(tree: &Tree, volname: &str) -> Result<Vec<u8>, String> {
    let nodes = &tree.nodes;
    let root = nodes.iter().position(|n| n.path.is_empty()).ok_or("tree has no root")?;
    // children per directory, sorted by name (SORTED_DIRS)
    let mut kids: std::collections::HashMap<&str, Vec<usize>> = std::collections::HashMap::new();
    for (i, n) in nodes.iter().enumerate() {
        if !n.path.is_empty() {
            kids.entry(crate::tree::parent(&n.path)).or_default().push(i);
        }
    }
    for v in kids.values_mut() {
        v.sort_by(|a, b| crate::tree::name(&nodes[*a].path).as_bytes().cmp(crate::tree::name(&nodes[*b].path).as_bytes()));
    }
    let entry_len = |i: usize| 12 + crate::tree::name(&nodes[i].path).len().div_ceil(4) * 4;
    // pass 1: place directory entry blocks breadth-first after the root inode
    let mut dir_off: std::collections::HashMap<usize, (usize, usize)> = std::collections::HashMap::new(); // dir node -> (children offset, size)
    let mut order: Vec<usize> = Vec::new(); // directories in BFS order
    let mut q = std::collections::VecDeque::from([root]);
    let mut pos = 76usize;
    while let Some(d) = q.pop_front() {
        let ch = kids.get(nodes[d].path.as_str()).cloned().unwrap_or_default();
        let size: usize = ch.iter().map(|&c| entry_len(c)).sum();
        dir_off.insert(d, (if ch.is_empty() { 0 } else { pos }, size));
        pos += size;
        order.push(d);
        for c in ch {
            if matches!(nodes[c].kind, Kind::Dir) {
                q.push_back(c);
            }
        }
    }
    // pass 2: file/symlink data, in directory-entry order
    let mut data_off: std::collections::HashMap<usize, usize> = std::collections::HashMap::new();
    let mut data = Vec::new();
    let mut blocks = 0u32;
    for &d in &order {
        for &c in kids.get(nodes[d].path.as_str()).map(|v| v.as_slice()).unwrap_or(&[]) {
            let bytes: Option<&[u8]> = match &nodes[c].kind {
                Kind::File(b) => Some(b),
                Kind::Symlink(t) => Some(t.as_bytes()),
                _ => None,
            };
            if let Some(b) = bytes {
                if b.is_empty() {
                    data_off.insert(c, 0);
                    continue;
                }
                let at = pos + data.len();
                data_off.insert(c, at);
                data.extend(compress_file(b, at));
                while data.len() % 4 != 0 {
                    data.push(0);
                }
                blocks += b.len().div_ceil(PAGE) as u32;
            }
        }
    }
    // assemble
    let mut img = vec![0u8; 76];
    for &d in &order {
        for &c in kids.get(nodes[d].path.as_str()).map(|v| v.as_slice()).unwrap_or(&[]) {
            let n = &nodes[c];
            let (size, off) = match &n.kind {
                Kind::Dir => {
                    let (o, s) = dir_off[&c];
                    (s as u32, o)
                }
                Kind::File(b) => (b.len() as u32, data_off[&c]),
                Kind::Symlink(t) => (t.len() as u32, data_off[&c]),
                Kind::Special(rdev) => (*rdev, 0),
            };
            img.extend(inode_bytes(n, size, off)?);
        }
    }
    img.extend(data);
    while img.len() % PAGE != 0 {
        img.push(0);
    }
    let (ro, rs) = dir_off[&root];
    let r = &nodes[root];
    let rnode = Node { path: String::new(), ..r.clone() };
    let root_inode = inode_bytes(&rnode, rs as u32, ro)?;
    img[64..76].copy_from_slice(&root_inode[..12]);
    img[0..4].copy_from_slice(&MAGIC.to_le_bytes());
    let total = img.len() as u32;
    img[4..8].copy_from_slice(&total.to_le_bytes());
    img[8..12].copy_from_slice(&(FLAG_V2 | FLAG_SORTED).to_le_bytes());
    img[16..32].copy_from_slice(b"Compressed ROMFS");
    img[40..44].copy_from_slice(&blocks.to_le_bytes());
    img[44..48].copy_from_slice(&(nodes.len() as u32).to_le_bytes());
    let mut nm = volname.as_bytes().to_vec();
    nm.resize(16, 0);
    img[48..64].copy_from_slice(&nm);
    let crc = crc32fast::hash(&img);
    img[32..36].copy_from_slice(&crc.to_le_bytes());
    if img.len() > 0xffff_ffff {
        return Err("cramfs image too large".into());
    }
    Ok(img)
}
