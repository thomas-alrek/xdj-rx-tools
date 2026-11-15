#!/usr/bin/env python3
"""
Collect every license text the packaged app needs into <Resources>/licenses/.

    packaging/collect-licenses.py <Contents/Resources> <qemu source dir>

- this project (GPL-2.0-or-later): LICENSE + THIRD_PARTY_NOTICES.md
- QEMU: COPYING, COPYING.LIB, LICENSE from the source tree it was built from
- each bundled dylib in Resources/libs: the license files of the Homebrew keg it came from
  (GLib's keg has none: its LGPL-2.1 text comes from QEMU's COPYING.LIB)
- noVNC and audio-knobs (the web UI)
- every Rust crate compiled into the shell and the host (cargo metadata), with its license files,
  or a note with its SPDX expression if the crate ships none
Run by packaging/build-macos-app.sh after the bundle is assembled, before signing.
"""
import glob, json, os, re, shutil, subprocess, sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
LIC_RE = re.compile(r"^(copying|licen[cs]e|notice|unlicense|authors)([.\-_].*)?$", re.I)


def copy_licenses(src_dir, dst_dir, top_only=True):
    found = []
    if not os.path.isdir(src_dir):
        return found
    for n in sorted(os.listdir(src_dir)):
        p = os.path.join(src_dir, n)
        if os.path.isfile(p) and LIC_RE.match(n):
            os.makedirs(dst_dir, exist_ok=True)
            shutil.copy(p, os.path.join(dst_dir, n))
            found.append(n)
    return found


def main():
    res, qemu_src = sys.argv[1], sys.argv[2]
    out = os.path.join(res, "licenses")
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out)
    shutil.copy(os.path.join(ROOT, "LICENSE"), os.path.join(out, "xdj-rx-tools-LICENSE.txt"))
    shutil.copy(os.path.join(ROOT, "THIRD_PARTY_NOTICES.md"), out)
    q = os.path.join(out, "qemu")
    os.makedirs(q)
    for n in ("COPYING", "COPYING.LIB", "LICENSE"):
        shutil.copy(os.path.join(qemu_src, n), q)

    # dylibs -> their Homebrew keg
    missing = []
    for lib in sorted(os.listdir(os.path.join(res, "libs"))):
        hits = glob.glob("/opt/homebrew/Cellar/*/*/lib/" + lib) + glob.glob("/usr/local/Cellar/*/*/lib/" + lib)
        if not hits:
            missing.append(lib)
            continue
        keg = os.path.dirname(os.path.dirname(hits[0]))
        formula, version = keg.split("/")[-2], keg.split("/")[-1]
        dst = os.path.join(out, formula)
        if os.path.isdir(dst):
            continue
        got = copy_licenses(keg, dst)
        if not got:
            got = copy_licenses(os.path.join(keg, "share", "doc", formula), dst)
        if not got and formula == "glib":
            os.makedirs(dst, exist_ok=True)
            shutil.copy(os.path.join(qemu_src, "COPYING.LIB"), os.path.join(dst, "LGPL-2.1.txt"))
            got = ["LGPL-2.1.txt (from QEMU's COPYING.LIB; GLib is LGPL-2.1-or-later)"]
        if not got:
            missing.append("%s (%s %s)" % (lib, formula, version))
        else:
            with open(os.path.join(dst, "VERSION.txt"), "w") as f:
                f.write("%s %s (Homebrew)\n" % (formula, version))

    # web UI
    copy_licenses(os.path.join(res, "ui", "novnc"), os.path.join(out, "novnc"))
    copy_licenses(os.path.join(ROOT, "app", "ui", "node_modules", "audio-knobs"), os.path.join(out, "audio-knobs"))

    # Rust crates
    seen = set()
    rust = os.path.join(out, "rust")
    for manifest in ("app/shell/Cargo.toml", "app/host/Cargo.toml"):
        meta = json.loads(subprocess.run(
            ["cargo", "metadata", "--format-version", "1", "--offline", "--manifest-path",
             os.path.join(ROOT, manifest), "--filter-platform", "aarch64-apple-darwin"],
            capture_output=True, text=True, check=True).stdout)
        used = {n["id"] for n in meta["resolve"]["nodes"]}
        for pkg in meta["packages"]:
            if pkg["id"] not in used or pkg.get("source") is None:   # path deps = this repo
                continue
            key = "%s-%s" % (pkg["name"], pkg["version"])
            if key in seen:
                continue
            seen.add(key)
            dst = os.path.join(rust, key)
            got = copy_licenses(os.path.dirname(pkg["manifest_path"]), dst)
            if not got:
                os.makedirs(dst, exist_ok=True)
                with open(os.path.join(dst, "LICENSE-EXPRESSION.txt"), "w") as f:
                    f.write("%s %s: %s (no license file shipped in the crate; see crates.io)\n"
                            % (pkg["name"], pkg["version"], pkg.get("license") or "unknown"))
    print("licenses: %d Rust crates, %d library kegs -> %s" % (
        len(seen), len([d for d in os.listdir(out) if os.path.isdir(os.path.join(out, d))]) - 1, out))
    if missing:
        print("WARNING: no license files found for: " + ", ".join(missing))
        sys.exit(1)


if __name__ == "__main__":
    main()
