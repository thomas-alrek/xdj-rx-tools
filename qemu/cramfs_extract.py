#!/usr/bin/env python3
# Minimal cramfs extractor: files, dirs, symlinks, device nodes. Run as root.
import os, sys, struct, zlib, stat
img = open(sys.argv[1], "rb").read()
out = sys.argv[2]
MAGIC = 0x28cd3d45
assert struct.unpack_from("<I", img, 0)[0] == MAGIC, "not cramfs"
# root inode at offset 64
def read_inode(off):
    w0, w1, w2 = struct.unpack_from("<III", img, off)
    mode = w0 & 0xFFFF; uid = w0 >> 16
    size = w1 & 0xFFFFFF; gid = w1 >> 24
    namelen = (w2 & 0x3F) * 4; offset = (w2 >> 6) * 4
    name = img[off+12:off+12+namelen].split(b"\x00")[0].decode("latin1")
    return mode, size, offset, name, off+12+namelen

def file_data(offset, size):
    if size == 0: return b""
    nblocks = (size + 4095) // 4096
    ptrs = struct.unpack_from("<%dI" % nblocks, img, offset)
    data = b""; start = offset + nblocks*4
    for p in ptrs:
        data += zlib.decompress(img[start:p]); start = p
    return data[:size]

def walk(dir_off, dir_size, path):
    end = dir_off + dir_size; off = dir_off
    while off < end:
        mode, size, offset, name, nxt = read_inode(off)
        off = nxt
        if not name or name in (".", ".."):
            if name in (".",".."): continue
        full = os.path.join(path, name)
        if stat.S_ISDIR(mode):
            os.makedirs(full, exist_ok=True); os.chmod(full, mode & 0o7777)
            if offset: walk(offset, size, full)
        elif stat.S_ISLNK(mode):
            tgt = file_data(offset, size).decode("latin1")
            try: os.symlink(tgt, full)
            except FileExistsError: pass
        elif stat.S_ISREG(mode):
            open(full, "wb").write(file_data(offset, size)); os.chmod(full, mode & 0o7777)
        elif stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
            try: os.mknod(full, mode, os.makedev((size>>8)&0xff, size&0xff))
            except (FileExistsError, PermissionError): pass
        elif stat.S_ISFIFO(mode):
            try: os.mkfifo(full, mode & 0o7777)
            except FileExistsError: pass
os.makedirs(out, exist_ok=True)
rmode, rsize, roff, rname, _ = read_inode(64)
walk(roff, rsize, out)
nl=sum(len(f) for _,_,f in os.walk(out))
print("extracted ok")
