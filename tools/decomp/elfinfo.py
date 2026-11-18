"""Minimal ELF32 little-endian reader for rbp (no dependencies).

Used by the decomp tooling; reads section headers, symbols from .dynsym and the
.ARM.exidx unwind index (which lists the start address of nearly every function
in a C++ binary built with exceptions).
"""
import struct


class Elf:
    def __init__(self, path):
        self.path = path
        self.d = open(path, 'rb').read()
        d = self.d
        assert d[:4] == b'\x7fELF' and d[4] == 1 and d[5] == 1, 'ELF32 LE only'
        shoff, = struct.unpack_from('<I', d, 0x20)
        shentsize, shnum, shstrndx = struct.unpack_from('<HHH', d, 0x2e)
        raw = [struct.unpack_from('<10I', d, shoff + i * shentsize) for i in range(shnum)]
        so = raw[shstrndx][4]
        self.sections = {}
        self._list = []
        for s in raw:
            name = d[so + s[0]:d.index(b'\0', so + s[0])].decode()
            sec = dict(name=name, type=s[1], flags=s[2], addr=s[3], off=s[4],
                       size=s[5], link=s[6], info=s[7], entsize=s[9])
            self._list.append(sec)
            self.sections.setdefault(name, sec)

    def sec(self, name):
        return self.sections[name]

    def sec_bytes(self, name):
        s = self.sections[name]
        return self.d[s['off']:s['off'] + s['size']]

    def read_va(self, va, n):
        for s in self.sections.values():
            if s['addr'] and s['type'] != 8 and s['addr'] <= va < s['addr'] + s['size']:
                o = s['off'] + va - s['addr']
                return self.d[o:o + n]
        raise KeyError(hex(va))

    def section_of(self, va):
        for name, s in self.sections.items():
            if s['addr'] and s['addr'] <= va < s['addr'] + s['size']:
                return name
        return None

    def exidx(self):
        """[(func_va, kind)], kind = 'cantunwind' | 'inline' | 'table'."""
        s = self.sections['.ARM.exidx']
        out = []
        for i in range(0, s['size'], 8):
            w0, w1 = struct.unpack_from('<II', self.d, s['off'] + i)
            here = s['addr'] + i
            off = w0 & 0x7fffffff
            if off & 0x40000000:
                off -= 0x80000000
            fn = (here + off) & 0xffffffff
            kind = 'cantunwind' if w1 == 1 else 'inline' if w1 & 0x80000000 else 'table'
            out.append((fn, kind))
        return out

    def section_list(self):
        """Sections in header order (index -> dict with 'name')."""
        return self._list

    def symbols(self, table='.symtab', strtab='.strtab'):
        if table not in self.sections:
            return []
        s, strs = self.sections[table], self.sections[strtab]
        out = []
        for i in range(0, s['size'], 16):
            name, value, size, info, other, shndx = struct.unpack_from('<IIIBBH', self.d, s['off'] + i)
            n = self.d[strs['off'] + name:self.d.index(b'\0', strs['off'] + name)].decode()
            out.append(dict(name=n, value=value, size=size, bind=info >> 4, type=info & 0xf, shndx=shndx))
        return out

    def relocs(self, name):
        """[(offset, type)] for a REL section such as '.rel.text'."""
        if name not in self.sections:
            return []
        s = self.sections[name]
        return [(o, i & 0xff) for o, i in
                (struct.unpack_from('<II', self.d, s['off'] + j) for j in range(0, s['size'], 8))]

    def rels(self, name):
        """[(offset, type, symbol index)] for a REL section such as '.rel.text'."""
        if name not in self.sections:
            return []
        s = self.sections[name]
        return [(o, i & 0xff, i >> 8) for o, i in
                (struct.unpack_from('<II', self.d, s['off'] + j) for j in range(0, s['size'], 8))]

    def dynsyms(self):
        s = self.sections['.dynsym']
        strs = self.sections['.dynstr']
        out = []
        for i in range(0, s['size'], 16):
            name, value, size, info, other, shndx = struct.unpack_from('<IIIBBH', self.d, s['off'] + i)
            n = self.d[strs['off'] + name:self.d.index(b'\0', strs['off'] + name)].decode()
            out.append(dict(name=n, value=value, size=size, bind=info >> 4, type=info & 0xf, shndx=shndx))
        return out
