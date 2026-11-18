"""Compare a relinked binary with the original, section by section, at the original addresses.

usage: cmp.py ORIGINAL BUILT [--max N]

For every allocated, non-empty section of ORIGINAL (except .bss) the bytes at the same
addresses in BUILT are compared. Differences are reported with the nearest symbol of BUILT
(the splitter's labels). Exit status 0 when everything matches.
"""
import os
import sys
from bisect import bisect_right

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from elfinfo import Elf  # noqa: E402


def main(argv):
    maxn = 10
    if '--max' in argv:
        i = argv.index('--max')
        maxn = int(argv[i + 1])
        del argv[i:i + 2]
    orig, built = Elf(argv[0]), Elf(argv[1])
    syms = sorted((s['value'], s['name']) for s in built.symbols()
                  if s['name'] and not s['name'].startswith('$') and s['shndx'])
    addrs = [v for v, _ in syms]

    def where(a):
        i = bisect_right(addrs, a) - 1
        return f'{syms[i][1]}+{a - syms[i][0]:#x}' if i >= 0 else '?'

    bad = 0
    for s in orig.section_list():
        if not (s['flags'] & 2) or not s['addr'] or s['type'] == 8 or not s['size']:
            continue
        mine = orig.sec_bytes(s['name'])
        try:
            theirs = built.read_va(s['addr'], s['size'])
        except KeyError:
            print(f'MISSING {s["name"]}')
            bad += 1
            continue
        if len(theirs) < len(mine):
            print(f'SHORT  {s["name"]}: {len(theirs)} of {len(mine)} bytes')
            bad += 1
        diffs = [i for i in range(0, min(len(mine), len(theirs)), 4) if mine[i:i + 4] != theirs[i:i + 4]]
        if diffs:
            bad += 1
            print(f'DIFF   {s["name"]}: {len(diffs)} words')
            for i in diffs[:maxn]:
                a = s['addr'] + i
                print(f'         {a:#x} {where(a)}: {mine[i:i + 4].hex()} -> {theirs[i:i + 4].hex()}')
        elif len(theirs) >= len(mine):
            print(f'OK     {s["name"]} ({s["size"]} bytes)')
    print('IDENTICAL' if not bad else f'{bad} sections differ')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
