//! ISO9660 + Rock Ridge: read a whole image into a [`Tree`], and write one back.
//!
//! The firmware image is a `genisoimage -R` volume ("UsbAuto"); the update initramfs
//! loop-mounts it and runs `./update` from it, so what matters to the guest is the
//! Rock Ridge view: names (NM), modes/owners (PX: `update`/`mot2bin` are 0555) and
//! symlinks (SL: `lib/libc.so.6 -> libc-2.13.so`, ...). The writer emits a plain
//! ISO9660 volume (level-1-style uppercase identifiers, both path tables) carrying
//! SUSP SP + RRIP ER in the root and PX/NM/SL on every record -- what Linux isofs
//! (and pycdlib) read. No Joliet: Linux prefers Rock Ridge anyway.

use crate::tree::{Kind, Node, Tree};

const BLK: usize = 2048;

// ---- reader -----------------------------------------------------------------------

struct Rec<'a> {
    lba: usize,
    len: usize,
    dir: bool,
    ident: &'a [u8],
    su: Vec<u8>, // system use (with CE continuations appended)
}

fn parse_rec<'a>(iso: &'a [u8], r: &'a [u8]) -> Option<Rec<'a>> {
    if r.len() < 34 || r[0] < 34 {
        return None;
    }
    let lba = u32::from_le_bytes(r[2..6].try_into().ok()?) as usize;
    let len = u32::from_le_bytes(r[10..14].try_into().ok()?) as usize;
    let nl = r[32] as usize;
    let ident = r.get(33..33 + nl)?;
    let su_start = 33 + nl + (1 - nl % 2);
    let mut su = r.get(su_start..).unwrap_or(&[]).to_vec();
    // Follow CE (continuation area) entries.
    let mut scan = 0;
    let mut hops = 0;
    while scan + 4 <= su.len() && hops < 16 {
        let l = su[scan + 2] as usize;
        if l < 4 || scan + l > su.len() {
            break;
        }
        if &su[scan..scan + 2] == b"CE" && l >= 28 {
            let b = u32::from_le_bytes(su[scan + 4..scan + 8].try_into().ok()?) as usize;
            let o = u32::from_le_bytes(su[scan + 12..scan + 16].try_into().ok()?) as usize;
            let n = u32::from_le_bytes(su[scan + 20..scan + 24].try_into().ok()?) as usize;
            if let Some(ext) = iso.get(b * BLK + o..b * BLK + o + n) {
                su.extend_from_slice(ext);
                hops += 1;
            }
        }
        scan += l;
    }
    Some(Rec { lba, len, dir: r[25] & 2 != 0, ident, su })
}

/// SUSP entries of a record: (signature, body-after-4-byte-header).
fn susp(su: &[u8]) -> Vec<(&[u8], &[u8])> {
    let mut out = Vec::new();
    let mut i = 0;
    while i + 4 <= su.len() {
        let l = su[i + 2] as usize;
        if l < 4 || i + l > su.len() {
            break;
        }
        out.push((&su[i..i + 2], &su[i + 4..i + l]));
        i += l;
    }
    out
}

fn rr_info(rec: &Rec) -> (Option<String>, Option<(u32, u32, u32)>, Option<String>) {
    let (mut nm, mut px, mut sl) = (Vec::<u8>::new(), None, None::<String>);
    let mut have_nm = false;
    let mut sl_parts: Vec<String> = Vec::new();
    let mut have_sl = false;
    for (sig, b) in susp(&rec.su) {
        match sig {
            b"NM" if !b.is_empty() => {
                have_nm = true;
                nm.extend_from_slice(&b[1..]);
            }
            b"PX" if b.len() >= 32 => {
                let g = |o: usize| u32::from_le_bytes(b[o..o + 4].try_into().unwrap());
                px = Some((g(0), g(16), g(24)));
            }
            b"SL" if !b.is_empty() => {
                have_sl = true;
                let mut j = 1;
                let mut cur = String::new();
                while j + 2 <= b.len() {
                    let (f, l) = (b[j], b[j + 1] as usize);
                    let content = b.get(j + 2..j + 2 + l).unwrap_or(&[]);
                    if f & 0x02 != 0 {
                        cur.push('.');
                    } else if f & 0x04 != 0 {
                        cur.push_str("..");
                    } else if f & 0x08 != 0 {
                        sl_parts.push(String::new()); // root
                    } else {
                        cur.push_str(&String::from_utf8_lossy(content));
                    }
                    if f & 0x01 == 0 {
                        // component complete
                        if f & 0x08 == 0 {
                            sl_parts.push(std::mem::take(&mut cur));
                        }
                    }
                    j += 2 + l;
                }
            }
            _ => {}
        }
    }
    if have_sl {
        let s = sl_parts.join("/");
        sl = Some(if s.is_empty() { "/".into() } else { s });
    }
    (have_nm.then(|| String::from_utf8_lossy(&nm).into_owned()), px, sl)
}

fn iso_name(ident: &[u8]) -> String {
    let s = String::from_utf8_lossy(ident).to_string();
    let s = s.split(';').next().unwrap_or("").to_string();
    s.strip_suffix('.').map(String::from).unwrap_or(s)
}

/// Read every file, directory and symlink of an ISO image (Rock Ridge names/modes).
pub fn read_tree(iso: &[u8]) -> Result<Tree, String> {
    let pvd = iso.get(16 * BLK..17 * BLK).ok_or("ISO too short")?;
    if &pvd[1..6] != b"CD001" || pvd[0] != 1 {
        return Err("not an ISO9660 image".into());
    }
    let root = parse_rec(iso, &pvd[156..190]).ok_or("bad ISO root record")?;
    let mut tree = Tree::default();
    // root's own mode from its '.' entry
    let root_mode = dir_entries(iso, &root)?
        .first()
        .and_then(|r| rr_info(r).1)
        .map(|p| p.0)
        .unwrap_or(0o40755);
    tree.nodes.push(Node { path: String::new(), mode: root_mode, uid: 0, gid: 0, mtime: 0, kind: Kind::Dir });
    walk(iso, &root, "", &mut tree, 0)?;
    tree.sort();
    Ok(tree)
}

fn dir_entries<'a>(iso: &'a [u8], dir: &Rec) -> Result<Vec<Rec<'a>>, String> {
    let data = iso
        .get(dir.lba * BLK..dir.lba * BLK + dir.len)
        .ok_or("directory extent outside the image")?;
    let mut out = Vec::new();
    let mut off = 0;
    while off < data.len() {
        let rl = data[off] as usize;
        if rl == 0 {
            off = (off / BLK + 1) * BLK;
            continue;
        }
        if let Some(r) = data.get(off..off + rl).and_then(|r| parse_rec(iso, r)) {
            out.push(r);
        }
        off += rl;
    }
    Ok(out)
}

fn walk(iso: &[u8], dir: &Rec, prefix: &str, tree: &mut Tree, depth: usize) -> Result<(), String> {
    if depth > 64 {
        return Err("ISO directory nesting too deep".into());
    }
    for r in dir_entries(iso, dir)? {
        if r.ident == [0] || r.ident == [1] {
            continue; // '.' / '..'
        }
        let (nm, px, sl) = rr_info(&r);
        let name = nm.unwrap_or_else(|| iso_name(r.ident));
        let path = if prefix.is_empty() { name.clone() } else { format!("{prefix}/{name}") };
        let (mode, uid, gid) = px.unwrap_or((if r.dir { 0o40555 } else { 0o100444 }, 0, 0));
        if r.dir {
            tree.nodes.push(Node { path: path.clone(), mode, uid, gid, mtime: 0, kind: Kind::Dir });
            walk(iso, &r, &path, tree, depth + 1)?;
        } else if let Some(t) = sl {
            tree.nodes.push(Node { path, mode, uid, gid, mtime: 0, kind: Kind::Symlink(t) });
        } else {
            let data = iso
                .get(r.lba * BLK..r.lba * BLK + r.len)
                .ok_or_else(|| format!("{path}: extent outside the image"))?
                .to_vec();
            tree.nodes.push(Node { path, mode, uid, gid, mtime: 0, kind: Kind::File(data) });
        }
    }
    Ok(())
}

/// The data of one file by path ("/images/uImage" or "images/uImage").
pub fn read_file(iso: &[u8], path: &str) -> Result<Vec<u8>, String> {
    let want = path.trim_start_matches('/');
    let pvd = iso.get(16 * BLK..17 * BLK).ok_or("ISO too short")?;
    if &pvd[1..6] != b"CD001" {
        return Err("not an ISO9660 image".into());
    }
    let mut cur = parse_rec(iso, &pvd[156..190]).ok_or("bad ISO root record")?;
    let comps: Vec<&str> = want.split('/').filter(|c| !c.is_empty()).collect();
    for (i, comp) in comps.iter().enumerate() {
        let next = dir_entries(iso, &cur)?
            .into_iter()
            .filter(|r| r.ident != [0] && r.ident != [1])
            .find(|r| rr_info(r).0.unwrap_or_else(|| iso_name(r.ident)) == *comp)
            .ok_or_else(|| format!("{path}: not in the firmware image"))?;
        if i + 1 < comps.len() && !next.dir {
            return Err(format!("{path}: not a directory"));
        }
        cur = next;
    }
    iso.get(cur.lba * BLK..cur.lba * BLK + cur.len)
        .map(|b| b.to_vec())
        .ok_or_else(|| format!("{path}: extent outside the image"))
}

// ---- writer -----------------------------------------------------------------------

fn both16(v: u16) -> [u8; 4] {
    let mut b = [0u8; 4];
    b[..2].copy_from_slice(&v.to_le_bytes());
    b[2..].copy_from_slice(&v.to_be_bytes());
    b
}

fn both32(v: u32) -> [u8; 8] {
    let mut b = [0u8; 8];
    b[..4].copy_from_slice(&v.to_le_bytes());
    b[4..].copy_from_slice(&v.to_be_bytes());
    b
}

/// 7-byte directory-record date (fixed: 2017-10-18 10:30:00 UTC, like the release).
const REC_DATE: [u8; 7] = [117, 10, 18, 10, 30, 0, 0];
const VD_DATE: &[u8; 17] = b"2017101810300000\0";

fn px(mode: u32, nlink: u32, uid: u32, gid: u32) -> Vec<u8> {
    let mut e = vec![b'P', b'X', 36, 1];
    for v in [mode, nlink, uid, gid] {
        e.extend_from_slice(&both32(v));
    }
    e
}

fn nm(name: &str) -> Vec<u8> {
    let mut e = vec![b'N', b'M', (5 + name.len()) as u8, 1, 0];
    e.extend_from_slice(name.as_bytes());
    e
}

fn sl(target: &str) -> Vec<u8> {
    let mut comps = Vec::new();
    let mut parts: Vec<&str> = target.split('/').collect();
    if target.starts_with('/') {
        comps.extend([0x08u8, 0]);
        parts.remove(0);
    }
    for p in parts {
        match p {
            "" => {}
            "." => comps.extend([0x02u8, 0]),
            ".." => comps.extend([0x04u8, 0]),
            s => {
                comps.extend([0u8, s.len() as u8]);
                comps.extend_from_slice(s.as_bytes());
            }
        }
    }
    let mut e = vec![b'S', b'L', (5 + comps.len()) as u8, 1, 0];
    e.extend(comps);
    e
}

/// RRIP "ER" (extensions reference). Too long for the root record together with SP +
/// PX, so it lives in a continuation area (CE), as genisoimage does it.
fn er() -> Vec<u8> {
    let id = b"RRIP_1991A";
    let des = b"THE ROCK RIDGE INTERCHANGE PROTOCOL PROVIDES SUPPORT FOR POSIX FILE SYSTEM SEMANTICS";
    let src = b"PLEASE CONTACT DISC PUBLISHER FOR SPECIFICATION SOURCE.  SEE PUBLISHER IDENTIFIER IN PRIMARY VOLUME DESCRIPTOR FOR CONTACT INFORMATION.";
    let mut er = vec![b'E', b'R', (8 + id.len() + des.len() + src.len()) as u8, 1, id.len() as u8, des.len() as u8, src.len() as u8, 1];
    er.extend_from_slice(id);
    er.extend_from_slice(des);
    er.extend_from_slice(src);
    er
}

/// SUSP "SP" (must open the root's '.' record) + a "CE" pointing at the ER.
fn sp_ce(ce_lba: u32, ce_len: u32) -> Vec<u8> {
    let mut e = vec![b'S', b'P', 7, 1, 0xBE, 0xEF, 0];
    e.extend([b'C', b'E', 28, 1]);
    e.extend_from_slice(&both32(ce_lba));
    e.extend_from_slice(&both32(0));
    e.extend_from_slice(&both32(ce_len));
    e
}

fn dir_record(ident: &[u8], lba: u32, len: u32, is_dir: bool, su: &[u8]) -> Vec<u8> {
    let nl = ident.len();
    let pad = 1 - nl % 2;
    let total = 33 + nl + pad + su.len();
    assert!(total <= 255, "ISO record too long");
    let mut r = vec![total as u8, 0];
    r.extend_from_slice(&both32(lba));
    r.extend_from_slice(&both32(len));
    r.extend_from_slice(&REC_DATE);
    r.push(if is_dir { 2 } else { 0 });
    r.extend([0, 0]);
    r.extend_from_slice(&both16(1));
    r.push(nl as u8);
    r.extend_from_slice(ident);
    r.extend(std::iter::repeat_n(0, pad));
    r.extend_from_slice(su);
    r
}

/// ISO-level identifier: uppercase d-characters, one dot, `;1` on files; unique per dir.
fn ident_for(name: &str, is_dir: bool, used: &mut std::collections::HashSet<String>) -> String {
    let up: String = name
        .chars()
        .map(|c| if c.is_ascii_alphanumeric() { c.to_ascii_uppercase() } else if c == '.' { '.' } else { '_' })
        .collect();
    let (base, ext) = match (is_dir, up.rfind('.')) {
        (false, Some(i)) if i > 0 => (up[..i].replace('.', "_"), up[i + 1..].to_string()),
        _ => (up.replace('.', "_"), String::new()),
    };
    let base: String = base.chars().take(24).collect();
    let ext: String = ext.chars().take(6).collect();
    for n in 0.. {
        let b = if n == 0 { base.clone() } else { format!("{}{n}", &base[..base.len().min(20)]) };
        let id = if is_dir { b } else { format!("{b}.{ext};1") };
        if used.insert(id.clone()) {
            return id;
        }
    }
    unreachable!()
}

struct WDir {
    path: String,
    parent: usize, // index into dirs
    lba: u32,
    size: u32,
    entries: Vec<WEnt>,
}

struct WEnt {
    ident: String,
    node: usize, // index into tree.nodes
    child_dir: Option<usize>,
}

fn path_table(dirs: &[WDir], big: bool) -> Vec<u8> {
    let mut t = Vec::new();
    for (i, d) in dirs.iter().enumerate() {
        let id: Vec<u8> = if i == 0 {
            vec![0]
        } else {
            dirs[d.parent].entries.iter().find(|e| e.child_dir == Some(i)).unwrap().ident.as_bytes().to_vec()
        };
        t.push(id.len() as u8);
        t.push(0);
        let (lba, par) = (d.lba, (d.parent + 1) as u16);
        if big {
            t.extend_from_slice(&lba.to_be_bytes());
            t.extend_from_slice(&par.to_be_bytes());
        } else {
            t.extend_from_slice(&lba.to_le_bytes());
            t.extend_from_slice(&par.to_le_bytes());
        }
        t.extend_from_slice(&id);
        if id.len() % 2 == 1 {
            t.push(0);
        }
    }
    t
}

/// Write `tree` as an ISO9660 + Rock Ridge image.
pub fn write_tree(tree: &Tree, volume_id: &str) -> Vec<u8> {
    let nodes = &tree.nodes;
    let idx: std::collections::HashMap<&str, usize> = nodes.iter().enumerate().map(|(i, n)| (n.path.as_str(), i)).collect();
    // directories, breadth-first (path-table order)
    let mut dirs: Vec<WDir> = vec![WDir { path: String::new(), parent: 0, lba: 0, size: 0, entries: vec![] }];
    let mut q = 0;
    while q < dirs.len() {
        let dpath = dirs[q].path.clone();
        let mut used = std::collections::HashSet::new();
        let mut kids: Vec<(String, usize)> = nodes
            .iter()
            .enumerate()
            .filter(|(_, n)| !n.path.is_empty() && crate::tree::parent(&n.path) == dpath)
            .map(|(i, n)| (ident_for(crate::tree::name(&n.path), matches!(n.kind, Kind::Dir), &mut used), i))
            .collect();
        kids.sort_by(|a, b| a.0.cmp(&b.0));
        let mut ents = Vec::new();
        for (ident, i) in kids {
            let child_dir = if matches!(nodes[i].kind, Kind::Dir) {
                dirs.push(WDir { path: nodes[i].path.clone(), parent: q, lba: 0, size: 0, entries: vec![] });
                Some(dirs.len() - 1)
            } else {
                None
            };
            ents.push(WEnt { ident, node: i, child_dir });
        }
        dirs[q].entries = ents;
        q += 1;
    }
    // SU for each node
    let su_of = |i: usize| -> Vec<u8> {
        let n = &nodes[i];
        let nlink = if matches!(n.kind, Kind::Dir) { 2 } else { 1 };
        let mut su = px(n.mode, nlink, n.uid, n.gid);
        su.extend(nm(crate::tree::name(&n.path)));
        if let Kind::Symlink(t) = &n.kind {
            su.extend(sl(t));
        }
        su
    };
    let dir_mode: Vec<u32> = dirs
        .iter()
        .map(|d| idx.get(d.path.as_str()).map(|i| nodes[*i].mode).unwrap_or(0o40755))
        .collect();
    // path tables + the continuation block (ER) come before the directories
    let pt_size = path_table(&dirs, false).len();
    let pt_blocks = pt_size.div_ceil(BLK).max(1);
    let (l_lba, m_lba) = (18, 18 + pt_blocks);
    let ce_lba = (18 + 2 * pt_blocks) as u32;
    let er_bytes = er();
    let dot_su = |d: usize, root: bool| -> Vec<u8> {
        let mode = dir_mode[d];
        let mut su = Vec::new();
        if root {
            su.extend(sp_ce(ce_lba, er_bytes.len() as u32));
        }
        su.extend(px(mode, 2, 0, 0));
        su
    };
    // directory extent sizes
    let rec_len = |ident_len: usize, su_len: usize| 33 + ident_len + (1 - ident_len % 2) + su_len;
    for d in 0..dirs.len() {
        let mut recs = vec![rec_len(1, dot_su(d, d == 0).len()), rec_len(1, dot_su(dirs[d].parent, false).len())];
        for e in &dirs[d].entries {
            recs.push(rec_len(e.ident.len(), su_of(e.node).len()));
        }
        let mut size = 0usize;
        for l in recs {
            if size % BLK + l > BLK {
                size = (size / BLK + 1) * BLK;
            }
            size += l;
        }
        dirs[d].size = (size.div_ceil(BLK) * BLK) as u32;
    }
    // path tables
    // layout: 16 system + PVD + terminator, L table, M table, dirs, files
    let mut lba = ce_lba as usize + 1;
    for d in dirs.iter_mut() {
        d.lba = lba as u32;
        lba += d.size as usize / BLK;
    }
    let mut file_lba: std::collections::HashMap<usize, u32> = std::collections::HashMap::new();
    for (i, n) in nodes.iter().enumerate() {
        if let Kind::File(data) = &n.kind {
            file_lba.insert(i, if data.is_empty() { 0 } else { lba as u32 });
            lba += data.len().div_ceil(BLK);
        }
    }
    let total = lba;
    let mut img = vec![0u8; total * BLK];
    // directories
    for (d, dir) in dirs.iter().enumerate() {
        let mut recs = vec![
            dir_record(&[0], dir.lba, dir.size, true, &dot_su(d, d == 0)),
            dir_record(&[1], dirs[dir.parent].lba, dirs[dir.parent].size, true, &dot_su(dir.parent, false)),
        ];
        for e in &dir.entries {
            let n = &nodes[e.node];
            let (l, s) = match (&n.kind, e.child_dir) {
                (_, Some(c)) => (dirs[c].lba, dirs[c].size),
                (Kind::File(data), None) => (file_lba[&e.node], data.len() as u32),
                _ => (0, 0),
            };
            recs.push(dir_record(e.ident.as_bytes(), l, s, e.child_dir.is_some(), &su_of(e.node)));
        }
        let mut off = dir.lba as usize * BLK;
        let base = off;
        for r in recs {
            if (off - base) % BLK + r.len() > BLK {
                off = base + ((off - base) / BLK + 1) * BLK;
            }
            img[off..off + r.len()].copy_from_slice(&r);
            off += r.len();
        }
    }
    for (i, n) in nodes.iter().enumerate() {
        if let Kind::File(data) = &n.kind {
            let o = file_lba[&i] as usize * BLK;
            img[o..o + data.len()].copy_from_slice(data);
        }
    }
    let lt = path_table(&dirs, false);
    let mt = path_table(&dirs, true);
    img[l_lba * BLK..l_lba * BLK + lt.len()].copy_from_slice(&lt);
    img[m_lba * BLK..m_lba * BLK + mt.len()].copy_from_slice(&mt);
    // PVD
    let mut v = vec![0u8; BLK];
    v[0] = 1;
    v[1..6].copy_from_slice(b"CD001");
    v[6] = 1;
    let pad = |s: &str, n: usize| -> Vec<u8> {
        let mut b: Vec<u8> = s.bytes().take(n).collect();
        b.resize(n, b' ');
        b
    };
    v[8..40].copy_from_slice(&pad("LINUX", 32));
    v[40..72].copy_from_slice(&pad(volume_id, 32));
    v[80..88].copy_from_slice(&both32(total as u32));
    v[120..124].copy_from_slice(&both16(1));
    v[124..128].copy_from_slice(&both16(1));
    v[128..132].copy_from_slice(&both16(BLK as u16));
    v[132..140].copy_from_slice(&both32(pt_size as u32));
    v[140..144].copy_from_slice(&(l_lba as u32).to_le_bytes());
    v[148..152].copy_from_slice(&(m_lba as u32).to_be_bytes());
    let root = dir_record(&[0], dirs[0].lba, dirs[0].size, true, &[]);
    v[156..190].copy_from_slice(&root);
    for (o, n) in [(190, 128), (318, 128), (446, 128)] {
        v[o..o + n].copy_from_slice(&pad("", n));
    }
    v[574..702].copy_from_slice(&pad("XDJ-FW (xdj-rx-tools)", 128));
    for o in [702, 739, 776] {
        v[o..o + 37].copy_from_slice(&pad("", 37));
    }
    for o in [813, 830, 847] {
        v[o..o + 17].copy_from_slice(VD_DATE);
    }
    v[864..881].copy_from_slice(b"0000000000000000\0");
    v[881] = 1;
    img[16 * BLK..17 * BLK].copy_from_slice(&v);
    let ce = ce_lba as usize * BLK;
    img[ce..ce + er_bytes.len()].copy_from_slice(&er_bytes);
    img[17 * BLK] = 255;
    img[17 * BLK + 1..17 * BLK + 6].copy_from_slice(b"CD001");
    img[17 * BLK + 6] = 1;
    img
}
