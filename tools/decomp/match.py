"""Compare functions in a compiled object file with the same functions in a linked binary.

usage: match.py TARGET OBJ.o [NAME=0xADDR ...]

Functions are paired by name with TARGET's .dynsym, or by the NAME=ADDR pairs given.
Words covered by a relocation in OBJ, and every bl (calls within a file carry no
relocation, their offsets depend on layout), are masked: branch/call relocations compare only
the opcode bits (the offset is filled in by the linker), data relocations are skipped.
Prints one line per function: MATCH, or the first differing words.
"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from elfinfo import Elf  # noqa: E402

R_ARM_ABS32, R_ARM_REL32 = 2, 3
R_ARM_CALL, R_ARM_JUMP24, R_ARM_PC24 = 28, 29, 1
R_ARM_MOVW_ABS_NC, R_ARM_MOVT_ABS = 43, 44
BRANCH = {R_ARM_CALL, R_ARM_JUMP24, R_ARM_PC24}
MOVW = {R_ARM_MOVW_ABS_NC, R_ARM_MOVT_ABS}


def compare(obj, text_name, value, size, target, addr):
    sec = obj.sec(text_name)
    mine = obj.d[sec['off'] + value:sec['off'] + value + size]
    theirs = target.read_va(addr, size)
    rel = {o: t for o, t in obj.relocs('.rel' + text_name)}
    diffs = []
    for i in range(0, size - size % 4, 4):
        a, = struct.unpack_from('<I', mine, i)
        b, = struct.unpack_from('<I', theirs, i)
        t = rel.get(value + i)
        if t is None and (a >> 24) & 0x0f == 0x0b and (b >> 24) & 0x0f == 0x0b:
            t = R_ARM_CALL  # bl to a function in the same file: offset depends on file layout
            a, b = a & 0xff000000, b & 0xff000000
        elif t in MOVW:
            a, b = a & 0xfff0f000, b & 0xfff0f000
        elif t is not None:
            continue
        if a != b:
            diffs.append((i, a, b))
    return diffs


def main():
    target = Elf(sys.argv[1])
    obj = Elf(sys.argv[2])
    pairs = dict(p.split('=') for p in sys.argv[3:])
    known = {s['name']: s for s in target.dynsyms() if s['shndx'] and s['type'] == 2}
    secs = obj.section_list()
    ok = total = 0
    for s in obj.symbols():
        if s['type'] != 2 or not s['size']:
            continue
        if s['name'] in pairs:
            addr, tsize = int(pairs[s['name']], 0), None
        elif s['name'] in known:
            addr, tsize = known[s['name']]['value'], known[s['name']]['size']
        else:
            continue
        total += 1
        text = secs[s['shndx']]['name']
        size_note = '' if tsize in (None, s['size']) else f' size {s["size"]} vs {tsize}'
        diffs = compare(obj, text, s['value'], s['size'], target, addr)
        if not diffs and not size_note:
            ok += 1
            print(f'MATCH  {s["name"]} ({s["size"]} bytes)')
        else:
            first = ' '.join(f'+{i:#x}:{a:08x}/{b:08x}' for i, a, b in diffs[:3])
            print(f'DIFF   {s["name"]}{size_note}: {len(diffs)} words differ {first}')
    print(f'{ok}/{total} match')


if __name__ == '__main__':
    main()
