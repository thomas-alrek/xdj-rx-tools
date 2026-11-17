#!/usr/bin/env python3
"""Make the patch bundle for xdjrx-patcher: our 3.00 changes against the official 2.21,
holding only our own bytes.

    python3 tools/fw/make-patch.py [RELEASE_DIR] [OUT]
      defaults: build/release (from tools/fw/build-release.sh)  build/patch/xdjrx-3.00.xdjpatch

From RELEASE_DIR/unpacked (the official image, unpacked) and RELEASE_DIR/overlay (what the
mods produced):
  - a file the official image doesn't have (ours: libxdjmod.so, mount.exfat-fuse, ...) is
    stored as is, with its mode;
  - a file the official image has (Pioneer's: apl_start, a udev rules file) is stored as
    line edits against it - lines we insert, or text we insert inside a line - plus the
    SHA-256 of the original and of the result. Pioneer's own text is never stored; a change
    that can't be expressed that way is an error.
The bundle (`.xdjpatch`: an uncompressed tar of manifest.json + files/<part>/<path>) also records the
release, SOURCE_DATE_EPOCH and the MD5s of the official input and of the expected output,
so the patcher can rebuild the release byte for byte and verify it.
"""
import difflib, hashlib, io, json, os, sys, tarfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REL_DIR = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "build/release")
OUT = sys.argv[2] if len(sys.argv) > 2 else os.path.join(ROOT, "build/patch/xdjrx-3.00.xdjpatch")
RELEASE = "3.00"
SOURCE_DATE_EPOCH = 1767225600
OFFICIAL_ZIP_MD5 = "c1ee67884f120e4cd8d4ffd6b9ad73eb"
OFFICIAL_UPD_MD5 = "e1facfb82de17b2544e958ca544e00c4"


def md5(p):
    return hashlib.md5(open(p, "rb").read()).hexdigest()


def sha256(b):
    return hashlib.sha256(b).hexdigest()


def lines_of(b: bytes):
    """Lines split after each \\n only (the patcher does exactly the same)."""
    t = b.decode("latin-1")
    out, i = [], 0
    while i < len(t):
        j = t.find("\n", i)
        j = len(t) if j < 0 else j + 1
        out.append(t[i:j])
        i = j
    return out


def line_edits(old: bytes, new: bytes, name: str):
    """Ops turning old into new, carrying only inserted text. Line numbers are 0-based
    indexes into the original; ops apply in order (each to the original numbering)."""
    a, b = lines_of(old), lines_of(new)
    ops = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        if tag == "insert":
            ops.append({"op": "insert_lines", "before": i1, "lines": b[j1:j2]})
        elif tag == "replace" and i2 - i1 == j2 - j1:
            for k in range(i2 - i1):
                o, n = a[i1 + k], b[j1 + k]
                p = 0
                while p < len(o) and p < len(n) and o[p] == n[p]:
                    p += 1
                ins = len(n) - len(o)
                if ins <= 0 or n[p + ins:] != o[p:]:
                    sys.exit(f"{name}: line {i1 + k + 1} changed in a way that isn't an insertion")
                ops.append({"op": "insert_text", "line": i1 + k, "at": p, "text": n[p:p + ins]})
        else:
            sys.exit(f"{name}: {tag} of lines {i1 + 1}-{i2} - only insertions are supported")
    # check: applying the ops reproduces new
    if apply_edits(old, ops) != new:
        sys.exit(f"{name}: edits don't reproduce the file")
    return ops


def apply_edits(old: bytes, ops):
    out_lines = lines_of(old)
    before = {}
    for op in ops:
        if op["op"] == "insert_lines":
            before.setdefault(op["before"], []).extend(op["lines"])
        else:
            l = out_lines[op["line"]]
            out_lines[op["line"]] = l[:op["at"]] + op["text"] + l[op["at"]:]
    res = []
    for i, l in enumerate(out_lines):
        res.extend(before.get(i, []))
        res.append(l)
    res.extend(before.get(len(out_lines), []))
    return "".join(res).encode("latin-1")


def main():
    unp, ov = os.path.join(REL_DIR, "unpacked"), os.path.join(REL_DIR, "overlay")
    upd = os.path.join(REL_DIR, "XDJRX.UPD")
    files, edits, blobs = [], [], {}
    for part in sorted(os.listdir(ov)):
        pdir = os.path.join(ov, part)
        if not os.path.isdir(pdir):
            continue
        for dp, dns, fns in os.walk(pdir):
            dns.sort()
            for fn in sorted(fns):
                full = os.path.join(dp, fn)
                rel = os.path.relpath(full, pdir).replace(os.sep, "/")
                data = open(full, "rb").read()
                base = os.path.join(unp, part, rel)
                if os.path.isfile(base):
                    old = open(base, "rb").read()
                    edits.append({"part": part, "path": rel, "base_sha256": sha256(old),
                                  "result_sha256": sha256(data), "ops": line_edits(old, data, f"{part}/{rel}")})
                else:
                    mode = os.stat(full).st_mode & 0o7777
                    files.append({"part": part, "path": rel, "mode": f"{mode:04o}", "sha256": sha256(data)})
                    blobs[f"files/{part}/{rel}"] = data
    manifest = {
        "format": 1,
        "release": RELEASE,
        "source_date_epoch": SOURCE_DATE_EPOCH,
        "input": {"zip_md5": OFFICIAL_ZIP_MD5, "upd_md5": OFFICIAL_UPD_MD5, "release": "2.21"},
        "output_md5": md5(upd),
        "files": files,
        "edits": edits,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as t:
        def add(name, data):
            ti = tarfile.TarInfo(name)
            ti.size, ti.mtime, ti.mode = len(data), SOURCE_DATE_EPOCH, 0o644
            t.addfile(ti, io.BytesIO(data))
        add("manifest.json", json.dumps(manifest, indent=1).encode())
        for name in sorted(blobs):
            add(name, blobs[name])
    open(OUT, "wb").write(buf.getvalue())
    print(f"{OUT}: {len(buf.getvalue())} bytes - {len(files)} files, {len(edits)} edited files, "
          f"release {RELEASE}, output MD5 {manifest['output_md5']}")


if __name__ == "__main__":
    main()
