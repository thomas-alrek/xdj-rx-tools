"""Generate the inline destructors of classes whose D1 only restores an ancestor's vtable.

For every RTTI class whose D1 is `ldr r3,[pc,#4]; str r3,[r0]; bx lr; .word V` and whose D0 is the
matching deleting form, where V is another class's vtable+8, emit `class C : public B` (B = the
class owning V, declared as a stub with an inline virtual destructor) and instantiate it, so GCC
emits the same D1/D0. Writes src/auto/auto_dtors.cpp and obj/auto_dtors.pairs. Run from decomp/.
"""
import os
import re, struct, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from elfinfo import Elf
e = Elf('rbp.orig')
starts = sorted(int(l.split()[0], 16) for l in open('split/functions.txt') if l.startswith('0x'))
sizes_all = {a: b - a for a, b in zip(starts, starts[1:])}
grafted = {int(l.split()[0], 16) for l in open('grafts.txt') if l.strip() and l[0] != '#' and l.split()[2] != 'src/auto/auto_dtors.cpp'}   # own earlier output is regenerated
words = lambda a, n: struct.unpack(f'<{n}I', e.read_va(a, 4 * n))

vt_owner, classes, slot0 = {}, [], {}
vt_count = {}
cls = None
for l in open('rtti/classes.txt'):
    if '  typeinfo' in l:
        cls = l.split()[0]
        kind = l.split('(')[1].split(')')[0]
    elif l.strip().startswith('vtable') and cls:
        vt_count[cls] = vt_count.get(cls, 0) + 1
        if 'offset 0:' not in l:
            continue
        va = int(l.split()[1], 16)
        vt_owner[va + 8] = cls
        fs = [int(x, 16) for x in l.split(':', 1)[1].split()]
        classes.append((cls, fs, va, kind))


def bl_target(w, a):
    if w & 0x0f000000 != 0x0b000000:
        return None
    off = w & 0xffffff
    return a + 8 + ((off - 0x1000000) if off & 0x800000 else off) * 4


for c, fs, va, kind in classes:          # slot0 = the class's D1, wherever the dtor sits
    if fs and fs[0] != 0xecc4:
        slot0.setdefault(fs[0], c)
    for i in range(min(3, len(fs) - 1)):
        if i and fs[i] != 0xecc4 and words(fs[i], 2)[0] in (0xe59f3004, 0xe59f3014):
            slot0.setdefault(fs[i], c)
            break

D0_INLINE = (0xe59f3014, 0xe92d4010, 0xe1a04000, 0xe5803000, None, 0xe1a00004, 0xe8bd8010)
NAME = re.compile(r'^[A-Za-z_][\w:]*$')
targets, calls_base, out_of_line = [], set(), set()


ZDL = {int(l.split()[0], 16) for l in open('split/labels.txt') if l.split()[1:] == ['p__ZdlPv']}


def calls_delete(w, at):
    """a bl to the global operator delete (classes with their own operator delete don't fit)"""
    return w & 0x0f000000 == 0x0b000000 and bl_target(w, at) in ZDL


def d0_calls(d0, d1):
    """the out-of-line form: push {r4,lr}; mov r4,r0; bl D1; mov r0,r4; bl delete; mov r0,r4; pop"""
    w = words(d0, 7)
    return w[0] == 0xe92d4010 and w[1] == 0xe1a04000 and bl_target(w[2], d0 + 8) == d1 and \
        w[3] == 0xe1a00004 and calls_delete(w[4], d0 + 16) and w[5] == 0xe1a00004 and w[6] == 0xe8bd8010


for c, fs, va, kind in classes:
    if not NAME.match(c) or vt_count.get(c, 0) != 1 or kind == 'class' and False:
        continue
    for i in range(min(3, len(fs) - 1)):
        d1, d0 = fs[i], fs[i + 1]
        if d1 in grafted or d0 in grafted or 0xecc4 in (d1, d0):
            continue
        w4, w8 = words(d1, 4), words(d1, 8)
        base, own, ool = None, False, d0_calls(d0, d1)
        if w4[:3] == (0xe59f3004, 0xe5803000, 0xe12fff1e) and w4[3] in vt_owner:
            v = w4[3]
            if vt_owner[v] == c:
                own = True                                   # root class
            else:
                base = vt_owner[v]                           # restores an ancestor's vtable
            if not ool:
                d = words(d0, 8)
                if own or any(x is not None and x != y for x, y in zip(D0_INLINE, d[:7])) or d[7] != v \
                        or not calls_delete(d[4], d0 + 16):
                    continue
        elif w8[:4] == (0xe59f3014, 0xe92d4010, 0xe1a04000, 0xe5803000) and \
                w8[5:] == (0xe1a00004, 0xe8bd8010, va + 8) and bl_target(w8[4], d1 + 16) in slot0:
            base = slot0[bl_target(w8[4], d1 + 16)]
            calls_base.add(base)
        else:
            continue
        if base is not None and (base == c or not NAME.match(base)):
            continue
        targets.append((c, base, d1, d0))
        if ool:
            out_of_line.add(c)
        break

NS = {'anonymous', 'ui', 'uif', 'common', 'mixerengine', 'playengine', 'dsp', 'db', 'network', 'djengine', 'juce',
      'utlitem', 'panel_protocol', 'browser', 'std'}

def split_ns(full):
    """(namespaces, classes) of a qualified name: leading lowercase known namespaces first."""
    parts = full.split('::')
    i = 0
    while i < len(parts) - 1 and parts[i] in NS:
        i += 1
    return parts[:i], parts[i:]

# classes without RTTI: a D1 restoring a known class's vtable, its D0 the next word of the vtable
seen = {d1 for _, _, d1, _ in targets}
label_addr = {}
for l in open('split/labels.txt'):
    p = l.split()
    if len(p) == 2:
        label_addr[p[1]] = int(p[0], 16)
next_word = {}
prev = None
for l in open('split/sec_rodata.s'):
    t = l.strip().split()
    cur = label_addr.get(t[1]) if len(t) >= 2 and t[0] == '.word' else None
    cur = cur if cur is not None and 0xeda0 <= cur < 0x360000 else None
    if prev is not None and cur is not None:
        next_word[prev] = cur
    prev = cur
for d1 in sorted(next_word):
    if d1 in grafted or d1 in seen or d1 >= 0x360000:
        continue
    w4 = words(d1, 4)
    if w4[:3] != (0xe59f3004, 0xe5803000, 0xe12fff1e) or w4[3] not in vt_owner:
        continue
    d0 = next_word[d1]
    if d0 in grafted:
        continue
    d = words(d0, 8)
    if any(x is not None and x != y for x, y in zip(D0_INLINE, d[:7])) or d[7] != w4[3] or \
            not calls_delete(d[4], d0 + 16):
        continue
    base = vt_owner[w4[3]]
    if not NAME.match(base):
        continue
    targets.append((f'anonymous::Derived_{d1:x}', base, d1, d0))

# deleting destructors whose D1 has a real body: only the D0 is generated (its call to D1 is
# resolved to rbp's D1 by address)
d0_only = []
for a, sz in sorted(sizes_all.items()):
    if a in grafted or a >= 0x360000 or a < 0xeda0 or sz not in (28, 32):
        continue
    if sz == 32 and words(a, 8)[7] != 0:        # 32: followed by an alignment word
        continue
    w = words(a, 7)
    if w[0] == 0xe92d4010 and w[1] == 0xe1a04000 and w[2] & 0xff000000 == 0xeb000000 and \
            w[3] == 0xe1a00004 and calls_delete(w[4], a + 16) and w[5] == 0xe1a00004 and w[6] == 0xe8bd8010 and \
            a not in {d0 for _, _, _, d0 in targets}:
        d0_only.append(a)
deleting_only = set()
for a in d0_only:
    targets.append((f'anonymous::Deleting_{a:x}', None, None, a))
    deleting_only.add(f'anonymous::Deleting_{a:x}')

# inline destructors of classes with a second vtable (multiple inheritance): store both own
# vptrs (the secondary one at +OFF, vtable address point +K), then call the base's D2
mi_classes = []
for c, fs, va, kind in classes:
    if len(fs) < 2 or fs[0] in grafted or fs[1] in grafted:
        continue
    d1, d0 = fs[0], fs[1]
    w = words(d1, 10)
    if not (w[0] & 0xfffff000 == 0xe59f3000 and w[1] == 0xe92d4010 and w[2] & 0xfffff000 == 0xe2832000 and
            w[3] == 0xe1a04000 and w[4] & 0xfffff000 == 0xe5802000 and w[5] == 0xe5803000 and
            w[6] & 0xff000000 == 0xeb000000 and w[7] == 0xe1a00004 and w[8] == 0xe8bd8010 and w[9] == va + 8):
        continue
    if bl_target(w[6], d1 + 24) not in slot0:
        continue
    d = words(d0, 12)
    if not (d[6] & 0xff000000 == 0xeb000000 and d[8] & 0xff000000 == 0xeb000000 and d[11] == va + 8):
        continue
    k, off = w[2] & 0xfff, w[4] & 0xfff
    if (k - 8) % 4 or off < 4:
        continue
    mi_classes.append((d1, d0, (k - 8) // 4, off))

base_of = {c: b for c, b, _, _ in targets if b}
classes = set()
for name in [c for c, _, _, _ in targets] + list(base_of.values()):
    ns, cl = split_ns(name)
    for k in range(1, len(cl) + 1):
        classes.add('::'.join(ns + cl[:k]))

def enclosing(c):
    ns, cl = split_ns(c)
    return '::'.join(ns + cl[:-1]) if len(cl) > 1 else None

children = {}
for c in classes:
    o = enclosing(c)
    if o:
        children.setdefault(o, []).append(c.split('::')[-1])

order, state = [], {}
def visit(c):
    if state.get(c) == 2:
        return
    if state.get(c) == 1:
        raise SystemExit(f'cycle at {c}')
    state[c] = 1
    for d in (base_of.get(c), enclosing(c)):
        if d:
            visit(d)
    state[c] = 2
    order.append(c)
for c in sorted(classes):
    visit(c)

def mangled(c):
    return 'N' + ''.join(f'{len(p)}{p}' for p in c.split('::'))

src = ['// Generated by scripts/autodtor.py from rbp\'s RTTI: inline destructors that only restore an',
       '// ancestor\'s vtable (rbp keeps their COMDAT copies next to the code that uses the classes).', '']
for c in order:
    ns, cl = split_ns(c)
    head = 'class ' + '::'.join(cl) + (f' : public ::{base_of[c]}' if c in base_of else '')
    if c in deleting_only:           # D0 must call D1, as rbp's D1 has a body we don't generate
        dtor = f'    virtual ~{cl[-1]}() __attribute__ ((noinline));'
    else:
        dtor = f'    virtual ~{cl[-1]}();' if c in calls_base or c in out_of_line else f'    virtual ~{cl[-1]}() {{}}'
    body = [head, '{', 'public:', dtor] + \
           [f'    class {k};' for k in sorted(children.get(c, []))] + ['};']
    src += [''.join(f'namespace {n} {{ ' for n in ns)] + body + ['}' * len(ns)] if ns else body
src += [''] + [f'{c}::~{c.split("::")[-1]}() {{}}' for c in sorted(out_of_line)]
src += [f'{c}::~{c.split("::")[-1]}() {{}}' for c in sorted(deleting_only)]
for d1, d0, nslots, off in mi_classes:
    ns = f'mi_{d1:x}'
    pad = f' char pad[{off - 4}];' if off > 4 else ''
    methods = ' '.join(f'virtual void f{k}();' for k in range(nslots - 2))
    src.append(f'namespace anonymous {{ namespace {ns} {{ struct P {{ virtual ~P(); {methods}{pad} }}; '
               f'struct S {{ virtual void s(); }}; struct B : P, S {{ virtual ~B(); }}; '
               f'struct C : B {{ virtual ~C() {{}} }}; void* make() {{ return new C(); }} }} }}')
src += ['', 'void* instantiateAutoDtors (int i)', '{', '    switch (i)', '    {']
src += [f'        case {i}: return new ::{c}();' for i, (c, _, _, _) in enumerate(targets) if c not in deleting_only]
src += ['        default: return 0;', '    }', '}', '']
open('src/auto/auto_dtors.cpp', 'w').write('\n'.join(src))
with open('obj/auto_dtors.pairs', 'w') as f:
    for d1, d0, _, _ in mi_classes:
        f.write(f'_ZN9anonymous{len(f"mi_{d1:x}")}mi_{d1:x}1CD1Ev={d1:#x}\n_ZN9anonymous{len(f"mi_{d1:x}")}mi_{d1:x}1CD0Ev={d0:#x}\n')
    for c, _, d1, d0 in targets:
        if d1 is not None:
            f.write(f'_Z{mangled(c)}D1Ev={d1:#x}\n')
        f.write(f'_Z{mangled(c)}D0Ev={d0:#x}\n')
print(len(targets), 'classes')
