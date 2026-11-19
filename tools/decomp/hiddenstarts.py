"""Find functions the splitter merged into their predecessor.

The split takes function starts from call targets and the unwind index; a function that is only
reached through a pointer (a vtable or callback table) and has no unwind entry of its own ends
up inside the function before it. This scans each split function for code that follows an
unconditional return (bx lr, pop {.., pc}, b) and its literal pool, is not a branch or
jump-table target inside the function and has no case label, and starts like a function (a push,
or a short leaf ending in bx lr; with RELAXED=1 also a short block ending in pop {.., pc} or a
tail call to a function). Writes the starts, one per line (hex, then the containing
function), to hidden_starts.txt (outside split/, which juce.py regenerates). The generators
take them with EXTRA_STARTS=hidden_starts.txt.
Run from decomp/.
"""
import os
import struct
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from elfinfo import Elf

e = Elf('rbp.orig')
starts = sorted(int(l.split()[0], 16) for l in open('split/functions.txt') if l.startswith('0x'))
labels = {}
for l in open('split/labels.txt'):
    p = l.split()
    if len(p) == 2:
        labels.setdefault(int(p[0], 16), []).append(p[1])
LO, HI = 0xeda0, 0x360000
RELAXED = os.environ.get('RELAXED') == '1'
start_set = set(starts)
NOPS = (0, 0xe1a00000, 0xe320f000)


def is_return(x):
    return x >> 28 == 0xe and (x == 0xe12fff1e or (x & 0x0fff8000) == 0x08bd8000
                               or (x & 0x0ffff000) == 0x049df000 or (x & 0x0f000000) == 0x0a000000)


def is_push(x):
    return (x & 0xffff0000) == 0xe92d0000 or (x & 0xffff0fff) == 0xe52d0004


out = []
for a, b in zip(starts, starts[1:]):
    if not LO <= a < HI or b - a < 8:
        continue
    n = (b - a) // 4
    w = struct.unpack('<%dI' % n, e.read_va(a, b - a))
    targets, pool = set(), set()
    for i, x in enumerate(w):
        at = a + 4 * i
        if (x & 0x0e000000) == 0x0a000000 and x >> 28 != 0xf:
            off = x & 0xffffff
            targets.add(at + 8 + ((off - 0x1000000) if off & 0x800000 else off) * 4)
        if (x & 0x0f7f0000) == 0x051f0000:                       # ldr rX, [pc, #imm]
            imm = x & 0xfff
            pool.add(at + 8 + (imm if x & 0x00800000 else -imm))
        if (x & 0x0f7f0f00) == 0x0d1f0b00:                       # vldr dN, [pc, #imm]
            imm = (x & 0xff) * 4
            lit = at + 8 + (imm if x & 0x00800000 else -imm)
            pool |= {lit, lit + 4}
        if a <= x < b:                                           # jump-table entries
            targets.add(x)
    i = 0
    while i < n - 1:
        if not is_return(w[i]):
            i += 1
            continue
        j = i + 1
        while j < n and (a + 4 * j in pool or w[j] in NOPS):
            j += 1
        if j < n:
            at, x = a + 4 * j, w[j]
            leaf = 0xe12fff1e in w[j:j + 6]
            if RELAXED:     # also short blocks that end in a return or a tail call to a function
                for k in range(j, min(j + 8, n)):
                    y = w[k]
                    if y >> 28 == 0xe and ((y & 0x0fff8000) == 0x08bd8000 or (y & 0x0f000000) == 0x0a000000):
                        off = y & 0xffffff
                        t = a + 4 * k + 8 + ((off - 0x1000000) if off & 0x800000 else off) * 4
                        leaf = leaf or (y & 0x0f000000) != 0x0a000000 or t in start_set
                        break
            if (at not in targets and not any(l.startswith('c_') for l in labels.get(at, []))
                    and (is_push(x) or (leaf and x >> 28 == 0xe))):
                out.append((at, a))
        i = j
open('hidden_starts.txt', 'w').write(''.join('%x %x\n' % t for t in out))
print(len(out), 'hidden starts')
