"""Find the functions of compiled object files inside a linked binary, by masked byte signature.

usage: findsig.py TARGET OBJ.o [OBJ.o ...]

Each function in each object becomes a word pattern: words covered by a relocation are
wildcards (data) or opcode-only (branches), and every bl is opcode-only (calls within
a file carry no relocation). Patterns are searched at every word of TARGET's .text.
Output, one line per function: FOUND <addr> <name> <object> <size> (unique hit), MULTI <n> <name>,
or NONE <name>; then a summary. Functions shorter than --min words are skipped
(too generic to place).
"""
import os
import struct
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from elfinfo import Elf  # noqa: E402
from match import BRANCH, MOVW  # noqa: E402

MIN_WORDS = 6


def pattern(obj, sec, value, size, rel):
    words = []
    for i in range(0, size - size % 4, 4):
        w, = struct.unpack_from('<I', obj.d, sec['off'] + value + i)
        t = rel.get(value + i)
        if t is None and (w >> 24) & 0x0f == 0x0b:
            t = 28
        if t in BRANCH:
            words.append((0xff000000, w & 0xff000000))
        elif t in MOVW:
            words.append((0xfff0f000, w & 0xfff0f000))
        elif t is not None:
            words.append((0, 0))
        else:
            words.append((0xffffffff, w))
    return words


def main(argv):
    min_words = MIN_WORDS
    if argv and argv[0].startswith('--min='):
        min_words = int(argv.pop(0)[6:])
    target = Elf(argv[0])
    t = target.sec('.text')
    tw = struct.unpack_from(f'<{t["size"] // 4}I', target.d, t['off'])
    index = defaultdict(list)
    for i, w in enumerate(tw):
        index[w].append(i)
    found = multi = none = skipped = 0
    for path in argv[1:]:
        obj = Elf(path)
        secs = obj.section_list()
        rels = {}
        for s in obj.symbols():
            if s['type'] != 2 or not s['size'] or s['shndx'] >= len(secs):
                continue
            if s['size'] // 4 < min_words:
                skipped += 1
                continue
            sec = secs[s['shndx']]
            if sec['name'] not in rels:
                rels[sec['name']] = dict(obj.relocs('.rel' + sec['name']))
            pat = pattern(obj, sec, s['value'], s['size'], rels[sec['name']])
            # anchor on the rarest fully-known word
            anchors = [(len(index.get(v, ())), k, v) for k, (m, v) in enumerate(pat) if m == 0xffffffff]
            if not anchors:
                skipped += 1
                continue
            _, k, v = min(anchors)
            hits = []
            for pos in index.get(v, ()):
                start = pos - k
                if start < 0 or start + len(pat) > len(tw):
                    continue
                if all((tw[start + j] & m) == val for j, (m, val) in enumerate(pat)):
                    hits.append(t['addr'] + start * 4)
            if len(hits) == 1:
                found += 1
                print(f'FOUND {hits[0]:#x} {s["name"]} {os.path.basename(path)} {s["size"]}')
            elif hits:
                multi += 1
                print(f'MULTI {len(hits)} {s["name"]} ' + ' '.join(f'{h:#x}' for h in hits[:4]))
            else:
                none += 1
                print(f'NONE  {s["name"]}')
    print(f'# found {found}, multiple {multi}, none {none}, skipped (short) {skipped}')


if __name__ == '__main__':
    main(sys.argv[1:])
