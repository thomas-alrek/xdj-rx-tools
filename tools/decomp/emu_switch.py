"""Evaluate a leaf 'switch on r3 = ldrh [r0,#8]' function for every 16-bit input, using the split listing.
usage: emu_switch.py START END   -> prints code -> result (string literal if result is a pointer to text)."""
import os
import re, struct, subprocess, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from elfinfo import Elf
e = Elf('rbp.orig')
lo, hi = sys.argv[1], sys.argv[2]
lines = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'listing.py'), lo, hi], capture_output=True, text=True).stdout.split('\n')
prog = {}
order = []
for l in lines:
    m = re.match(r'\s+([0-9a-f]+): (\w+)\s*(.*)', l)
    if m:
        a = int(m.group(1), 16); prog[a] = (m.group(2), m.group(3)); order.append(a)
    m = re.match(r'\s+(movw|movt) (r\d), #:(lower|upper)16:\w+_([0-9a-f]+)\s+@ ([0-9a-f]+)', l)
    if m:
        a = int(m.group(5), 16); v = int(m.group(4), 16)
        prog[a] = (m.group(1), f'{m.group(2)}, #{(v & 0xffff) if m.group(3)=="lower" else v >> 16}'); order.append(a)
nxt = {a: b for a, b in zip(order, order[1:])}
CONDS = {'eq': lambda f: f['z'], 'ne': lambda f: not f['z'], 'hi': lambda f: f['c'] and not f['z'],
         'ls': lambda f: not f['c'] or f['z'], 'lo': lambda f: not f['c'], 'hs': lambda f: f['c'],
         'gt': lambda f: not f['z'] and f['n'] == f['v'], 'le': lambda f: f['z'] or f['n'] != f['v']}
def imm(s): return int(s.strip().lstrip('#'), 0)
def text(v):
    if isinstance(v, tuple):
        return f'{text(v[0])} [grp {v[1]:#x}]'
    try:
        b = e.read_va(v, 80).split(b'\0')[0]
        if b and all(32 <= c < 127 for c in b): return repr(b.decode())
    except Exception: pass
    return hex(v)
def run(code, word):
    r = {'r0': None, 'r1': 0, 'r2': 0, 'r3': 0}; f = {'z': 0, 'c': 0, 'n': 0, 'v': 0}
    pc = order[0]
    for _ in range(500):
        op, args = prog[pc]
        m = re.match(r'([a-z]+?)(eq|ne|hi|ls|lo|hs|gt|le)?$', op)
        base, cond = m.group(1), m.group(2)
        if base in ('bx', 'b', 'bl') and cond is None and op not in ('b', 'bx', 'bl'):
            pass
        if cond and not CONDS[cond](f):
            pc = nxt[pc]; continue
        a = [x.strip() for x in args.split(',')]
        if base == 'ldrh': r[a[0]] = code
        elif base == 'ldr' and 'pc' in args:
            off = int(re.search(r'#(0x[0-9a-f]+|\d+)', args).group(1), 0)
            r[a[0]] = struct.unpack('<I', e.read_va(pc + 8 + off, 4))[0]
        elif base == 'ldr': r[a[0]] = word
        elif base == 'movw': r[a[0]] = imm(a[1])
        elif base == 'movt': r[a[0]] = (r[a[0]] & 0xffff) | (imm(a[1]) << 16)
        elif base == 'mov': r[a[0]] = imm(a[1]) if a[1].startswith('#') else r[a[1]]
        elif base in ('add', 'sub'):
            y = imm(a[2]) if a[2].startswith('#') else r[a[2]]
            r[a[0]] = (r[a[1]] + y if base == 'add' else r[a[1]] - y) & 0xffffffff
        elif base in ('cmp', 'tst'):
            x = r[a[0]]; y = imm(a[1]) if a[1].startswith('#') else r[a[1]]
            if base == 'cmp':
                d = (x - y) & 0xffffffff; f = {'z': d == 0, 'c': x >= y, 'n': d >> 31, 'v': ((x ^ y) & (x ^ d)) >> 31}
            else:
                d = x & y; f = dict(f, z=d == 0, n=d >> 31); r['grp'] = y
        elif base == 'bx': return (r['r0'], r.get('grp')) if GROUPS else r['r0']
        elif base == 'b': pc = int(a[0].split()[0].lstrip('#'), 16); continue
        else: raise Exception(f'{pc:#x} {op} {args}')
        pc = nxt[pc]
    raise Exception('loop')
GROUPS = '--groups' in sys.argv
res = {}
for code in range(0x10000):
    v = run(code, code)
    res.setdefault(v, []).append(code)
default = max(res, key=lambda k: len(res[k]))
print('default', text(default) if default is not None else None)
for v, cs in sorted(res.items(), key=lambda kv: kv[1][0]):
    if v != default:
        print(' '.join(f'{c:#06x}' for c in cs), '->', text(v) if (isinstance(v, tuple) or v and v > 0x10000) else v)
