#!/usr/bin/env python3
"""XDJ-RX GUI images: decode them to PNG, or replace one.

  tools/fw/gui-images.py dump GUI_DIR OUT_DIR
      GUI_DIR = the gui part of `xdj fw unpack` (build/unpacked/gui). Writes img_XXXX.png for
      every record of pset/imagedata/imagedata.dat (XXXX = rbp's image id, hex), the
      pioneer/imagedata/bin/*/<language>.bin pictures, index.txt and index.html (contact sheet).
  tools/fw/gui-images.py put IMAGEDATA_IN IMAGEDATA_OUT ID PNG
      Replace record ID with PNG (same width and height; 8-bit RGB / RGBA, non-interlaced).
      Put IMAGEDATA_OUT at OVERLAY/gui/pset/imagedata/imagedata.dat and build with
      `xdj fw build ... --overlay OVERLAY` (a USER update flashes the gui part).

Formats: imagedata.dat = records of 0x2c bytes (+4 width, +6 height (u16), +0x18 format 2,
+0x20 data offset; the first offset ends the table), pixels RGB565 little-endian, 0xf81f =
transparent. <language>.bin = width, height (u16) then RGB565. The output is Pioneer's artwork:
keep it out of the repository.
SPDX-License-Identifier: GPL-2.0-or-later
"""
import collections, os, struct, sys, zlib


def png(path, w, h, rgba):
    raw = b''.join(b'\0' + rgba[y * w * 4:(y + 1) * w * 4] for y in range(h))
    ch = lambda t, b: struct.pack('>I', len(b)) + t + b + struct.pack('>I', zlib.crc32(t + b) & 0xffffffff)
    open(path, 'wb').write(b'\x89PNG\r\n\x1a\n' + ch(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 6, 0, 0, 0))
                           + ch(b'IDAT', zlib.compress(raw, 6)) + ch(b'IEND', b''))

def rgb565(buf, w, h, key=0xf81f):
    out = bytearray(w * h * 4)
    for i, (v,) in enumerate(struct.iter_unpack('<H', buf[:w * h * 2])):
        r, g, b = (v >> 11) & 31, (v >> 5) & 63, v & 31
        out[4 * i:4 * i + 4] = bytes(((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2), 0 if v == key else 255))
    return bytes(out)

def read_png(path):
    b = open(path, 'rb').read()
    assert b[:8] == b'\x89PNG\r\n\x1a\n'
    o, idat, hdr = 8, b'', None
    while o < len(b):
        n, t = struct.unpack('>I4s', b[o:o + 8]); body = b[o + 8:o + 8 + n]; o += 12 + n
        if t == b'IHDR': hdr = struct.unpack('>IIBBBBB', body)
        elif t == b'IDAT': idat += body
    w, h, depth, ctype, _, _, inter = hdr
    assert depth == 8 and ctype in (2, 6) and not inter, hdr
    bpp = 3 if ctype == 2 else 4
    raw = zlib.decompress(idat); stride = w * bpp
    rows, prev, p = [], bytearray(stride), 0
    for y in range(h):
        f = raw[p]; line = bytearray(raw[p + 1:p + 1 + stride]); p += 1 + stride
        for i in range(stride):
            a = line[i - bpp] if i >= bpp else 0; up = prev[i]; c = prev[i - bpp] if i >= bpp else 0
            if f == 1: line[i] = (line[i] + a) & 255
            elif f == 2: line[i] = (line[i] + up) & 255
            elif f == 3: line[i] = (line[i] + (a + up) // 2) & 255
            elif f == 4:
                pa, pb, pc = abs(up - c), abs(a - c), abs(a + up - 2 * c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else up if pb <= pc else c)) & 255
        rows.append(line); prev = line
    return w, h, bpp, rows


def dump(gui, out):
    os.makedirs(out, exist_ok=True)
    d = open(os.path.join(gui, 'pset/imagedata/imagedata.dat'), 'rb').read()
    n = struct.unpack_from('<I', d, 0x20)[0] // 0x2c
    fmts = collections.Counter()
    index = []
    for i in range(n):
        r = d[0x2c * i:0x2c * (i + 1)]
        w, h = struct.unpack_from('<HH', r, 4)
        fmt, = struct.unpack_from('<I', r, 0x18)
        off, = struct.unpack_from('<I', r, 0x20)
        fmts[fmt] += 1
        index.append(f'0x{i:03x}  {w:4}x{h:<4} fmt {fmt} off 0x{off:x}')
        if fmt != 2 or not w or not h or off + w * h * 2 > len(d):
            continue
        png(os.path.join(out, f'img_{i:04x}.png'), w, h, rgb565(d[off:], w, h))
    open(os.path.join(out, 'index.txt'), 'w').write('\n'.join(index) + '\n')
    print(f'imagedata.dat: {n} records, formats {dict(fmts)}')
    for root, _, files in os.walk(os.path.join(gui, 'pioneer/imagedata/bin')):
        for f in files:
            b = open(os.path.join(root, f), 'rb').read()
            w, h = struct.unpack_from('<HH', b, 0)
            if 4 + w * h * 2 == len(b):
                name = os.path.basename(root) + '_' + f.replace('.bin', '.png')
                png(os.path.join(out, name), w, h, rgb565(b[4:], w, h, key=None))
                print(name, w, h)
            else:
                print('skipped', root, f, w, h, len(b))
    names = sorted(f for f in os.listdir(out) if f.endswith('.png'))
    with open(os.path.join(out, 'index.html'), 'w') as f:
        f.write('<!doctype html><meta charset=utf-8><title>XDJ-RX GUI images</title><style>body{background:#333;'
                'color:#ccc;font:11px monospace}div{display:inline-block;margin:4px;padding:4px;background:#111;'
                'vertical-align:top;text-align:center}img{display:block;margin:auto}</style>\n')
        for n in names:
            f.write(f'<div><img src="{n}">{n[:-4]}</div>\n')


def put(src, dst, rid, png):
    d = bytearray(open(src, 'rb').read())
    w, h = struct.unpack_from('<HH', d, 0x2c * rid + 4)
    fmt, = struct.unpack_from('<I', d, 0x2c * rid + 0x18)
    off, = struct.unpack_from('<I', d, 0x2c * rid + 0x20)
    pw, ph, bpp, rows = read_png(png)
    assert (pw, ph) == (w, h) and fmt == 2, ((pw, ph), (w, h), fmt)
    out = bytearray()
    for line in rows:
        for x in range(w):
            r, g, b = line[x * bpp:x * bpp + 3]
            v = 0xf81f if bpp == 4 and line[x * 4 + 3] < 128 else ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
            if v == 0xf81f and not (bpp == 4 and line[x * 4 + 3] < 128):
                v = 0xf81e                       # opaque magenta must not become the transparent key
            out += struct.pack('<H', v)
    d[off:off + w * h * 2] = out
    open(dst, 'wb').write(d)
    print(f'record 0x{rid:x}: {w}x{h} written at 0x{off:x}')


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == 'dump':
        dump(sys.argv[2], sys.argv[3])
    elif len(sys.argv) == 6 and sys.argv[1] == 'put':
        put(sys.argv[2], sys.argv[3], int(sys.argv[4], 0), sys.argv[5])
    else:
        sys.exit(__doc__)
