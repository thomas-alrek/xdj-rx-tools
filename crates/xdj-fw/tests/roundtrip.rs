//! Round trips against the real firmware image (build/XDJRX.iso, from
//! `xdj fw decrypt`). Skipped when it isn't there.

use std::path::{Path, PathBuf};

use xdj_fw::tree::Kind;
use xdj_fw::{cramfs, fwbuild, iso, tarball};

fn base() -> Option<Vec<u8>> {
    let p = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../build/XDJRX.iso");
    std::fs::read(&p).ok()
}

fn file(t: &xdj_fw::tree::Tree, p: &str) -> Vec<u8> {
    match &t.get(p).unwrap().kind {
        Kind::File(d) => d.clone(),
        _ => panic!("{p} not a file"),
    }
}

#[test]
fn iso_roundtrip() {
    let Some(b) = base() else { return eprintln!("skip: no build/XDJRX.iso") };
    let t = iso::read_tree(&b).unwrap();
    assert!(t.nodes.len() > 80);
    assert!(matches!(&t.get("lib/libc.so.6").unwrap().kind, Kind::Symlink(s) if s == "libc-2.13.so"));
    assert_eq!(t.get("update").unwrap().mode, 0o100555);
    let out = iso::write_tree(&t, "UsbAuto");
    let t2 = iso::read_tree(&out).unwrap();
    assert_eq!(t, t2);
    assert_eq!(iso::read_file(&out, "/images/release.txt").unwrap(), b"2.21\n");
}

#[test]
fn cramfs_roundtrip() {
    let Some(b) = base() else { return };
    let img = iso::read_file(&b, "images/rootfs.cramfs").unwrap();
    let t = cramfs::read(&img).unwrap();
    assert!(t.nodes.iter().any(|n| matches!(n.kind, Kind::Special(_))), "device nodes expected");
    let out = cramfs::write(&t, "Compressed").unwrap();
    let t2 = cramfs::read(&out).unwrap();
    assert_eq!(t, t2);
    assert_eq!(&out[0..4], &0x28cd3d45u32.to_le_bytes());
    let mut z = out.clone();
    z[32..36].copy_from_slice(&[0; 4]);
    assert_eq!(crc32fast::hash(&z).to_le_bytes(), out[32..36]);
}

#[test]
fn tar_rewrite_identity() {
    let Some(b) = base() else { return };
    let empty = std::env::temp_dir().join("xdj-empty-overlay");
    std::fs::create_dir_all(&empty).unwrap();
    for name in ["pioneer", "gui", "data", "user"] {
        let tgz = iso::read_file(&b, &format!("images/{name}.tar.gz")).unwrap();
        let (new, touched) = tarball::rewrite(&tgz, &empty).unwrap();
        assert!(touched.is_empty());
        assert_eq!(tarball::read(&tgz).unwrap(), tarball::read(&new).unwrap(), "{name}");
    }
}

#[test]
fn build_identity_and_overlay() {
    let Some(b) = base() else { return };
    // no overlay, no release: every file identical
    let (out, model, _) = fwbuild::build(&b, &fwbuild::BuildOptions { overlay: None, release: None, model: None }).unwrap();
    assert_eq!(model, "XDJAERO2.21");
    assert_eq!(iso::read_tree(&b).unwrap(), iso::read_tree(&out).unwrap());
    // overlay: rootfs + pioneer edits, release 2.30
    let ov = std::env::temp_dir().join("xdj-test-overlay");
    let _ = std::fs::remove_dir_all(&ov);
    std::fs::create_dir_all(ov.join("rootfs/etc")).unwrap();
    std::fs::create_dir_all(ov.join("pioneer/pioneer")).unwrap();
    std::fs::write(ov.join("rootfs/etc/xdj-test"), "hello\n").unwrap();
    std::fs::write(ov.join("pioneer/pioneer/xdj-test"), "hi\n").unwrap();
    let (out, model, _) = fwbuild::build(&b, &fwbuild::BuildOptions { overlay: Some(Path::new(&ov)), release: Some("2.30"), model: None }).unwrap();
    assert_eq!(model, "XDJAERO2.30");
    let t = iso::read_tree(&out).unwrap();
    assert_eq!(file(&t, "images/release.txt"), b"2.30\n");
    let rf = cramfs::read(&file(&t, "images/rootfs.cramfs")).unwrap();
    assert_eq!(file(&rf, "etc/xdj-test"), b"hello\n");
    let orig_rf = cramfs::read(&iso::read_file(&b, "images/rootfs.cramfs").unwrap()).unwrap();
    assert_eq!(rf.nodes.len(), orig_rf.nodes.len() + 1);
    let pt = tarball::read(&file(&t, "images/pioneer.tar.gz")).unwrap();
    assert_eq!(file(&pt, "pioneer/xdj-test"), b"hi\n");
    // untouched payloads carried over byte for byte
    assert_eq!(file(&t, "images/gui.tar.gz"), iso::read_file(&b, "images/gui.tar.gz").unwrap());
    assert_eq!(file(&t, "images/uImage"), iso::read_file(&b, "images/uImage").unwrap());
}

/// Incompressible bytes (an LCG), so a payload grows by about `n`.
fn noise(n: usize) -> Vec<u8> {
    let mut x: u32 = 0x1234_5678;
    (0..n).map(|_| { x = x.wrapping_mul(1_664_525).wrapping_add(1_013_904_223); (x >> 24) as u8 }).collect()
}

#[test]
fn boot_limits() {
    let Some(b) = base() else { return };
    // stock 2.21 fits what the unit loads at boot
    let budget = fwbuild::boot_budget(&iso::read_tree(&b).unwrap()).unwrap();
    assert!(budget.errors.is_empty(), "{:?}", budget.errors);
    let build = |name: &str, part: &str, path: &str, n: usize| {
        let ov = std::env::temp_dir().join(name);
        let _ = std::fs::remove_dir_all(&ov);
        let f = ov.join(part).join(path);
        std::fs::create_dir_all(f.parent().unwrap()).unwrap();
        std::fs::write(&f, noise(n)).unwrap();
        fwbuild::build(&b, &fwbuild::BuildOptions { overlay: Some(&ov), release: Some("9.99"), model: None })
    };
    // pioneer.tar.gz past the 3 MiB the initramfs reads: refused
    let e = build("xdj-test-big-pioneer", "pioneer", "pioneer/blob", 1536 << 10).err().expect("oversized pioneer built");
    assert!(e.contains("pioneer.tar.gz") && e.contains("0x300000"), "{e}");
    // rootfs past the 20 MiB read and the RAM disk: refused
    let e = build("xdj-test-big-rootfs", "rootfs", "usr/share/blob", 6 << 20).err().expect("oversized rootfs built");
    assert!(e.contains("0x1400000") && e.contains("RAM disk"), "{e}");
}

#[test]
fn unpack_edit_repack() {
    let Some(b) = base() else { return };
    let d = std::env::temp_dir().join(format!("xdj-repack-test-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    fwbuild::unpack(&b, &d.join("u")).unwrap();
    // untouched: no changes
    assert!(fwbuild::diff_unpacked(&b, &d.join("u"), None).unwrap().is_empty());
    // edit, add, delete, retarget a symlink
    let u = d.join("u");
    std::fs::write(u.join("rootfs/etc/xdj-new"), "new\n").unwrap();
    let mut rcs = std::fs::read(u.join("rootfs/etc/rc.d/rcS")).unwrap();
    rcs.extend_from_slice(b"echo repack\n");
    std::fs::write(u.join("rootfs/etc/rc.d/rcS"), &rcs).unwrap();
    std::fs::remove_file(u.join("rootfs/etc/issue.net")).unwrap();
    std::fs::remove_file(u.join("pioneer/pioneer/with_core_rbp")).unwrap();
    std::fs::remove_file(u.join("rootfs/bin/addgroup")).unwrap();
    std::os::unix::fs::symlink("/bin/busybox", u.join("rootfs/bin/addgroup")).unwrap();
    let ov = d.join("ov");
    let ch = fwbuild::diff_unpacked(&b, &u, Some(&ov)).unwrap();
    assert_eq!(ch.len(), 5, "{ch:?}");
    let (out, _, _) = fwbuild::build(&b, &fwbuild::BuildOptions { overlay: Some(&ov), release: Some("2.31"), model: None }).unwrap();
    let t = iso::read_tree(&out).unwrap();
    let rf = cramfs::read(&file(&t, "images/rootfs.cramfs")).unwrap();
    assert_eq!(file(&rf, "etc/xdj-new"), b"new\n");
    assert_eq!(file(&rf, "etc/rc.d/rcS"), rcs);
    assert!(rf.get("etc/issue.net").is_none());
    assert!(matches!(&rf.get("bin/addgroup").unwrap().kind, Kind::Symlink(s) if s == "/bin/busybox"));
    let pt = tarball::read(&file(&t, "images/pioneer.tar.gz")).unwrap();
    assert!(pt.get("pioneer/with_core_rbp").is_none());
    // the repacked image, unpacked, diffs to nothing against itself
    fwbuild::unpack(&out, &d.join("r")).unwrap();
    assert!(fwbuild::diff_unpacked(&out, &d.join("r"), None).unwrap().is_empty());
    let _ = std::fs::remove_dir_all(&d);
}
