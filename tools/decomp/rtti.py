"""Recover C++ classes from RTTI in a stripped (non-PIC, GCC/Itanium ABI) executable.

usage: rtti.py TARGET FUNCTIONS OUTDIR

FUNCTIONS is a function list (`ADDR [NAME]` lines, e.g. split.py's functions.txt).
Typeinfo objects are found by their first word, which points into one of the three
__cxxabiv1 typeinfo vtables (imported by copy relocation, so they have .dynsym entries).
Vtables are found as [offset-to-top][&typeinfo][virtual functions...].

Writes OUTDIR/classes.txt (class, typeinfo, bases, vtables and their slots) and
OUTDIR/vfuncs.txt (`ADDR CLASS SLOT`): each virtual function attributed to the most basic
class that has it in a vtable.
"""
import os
import shutil
import struct
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from elfinfo import Elf  # noqa: E402

KINDS = {'_ZTVN10__cxxabiv117__class_type_infoE': 'class',
         '_ZTVN10__cxxabiv120__si_class_type_infoE': 'si',
         '_ZTVN10__cxxabiv121__vmi_class_type_infoE': 'vmi'}


def demangle(names):
    tool = shutil.which('c++filt') or shutil.which('llvm-cxxfilt')
    if not tool:
        return {n: n for n in names}
    out = subprocess.run([tool], input='\n'.join('_ZTS' + n for n in names), capture_output=True,
                         text=True).stdout.split('\n')
    pre = 'typeinfo name for '
    return {n: (d[len(pre):] if d.startswith(pre) else n) for n, d in zip(names, out)}


def main(target, functions, outdir):
    e = Elf(target)
    funcs = {int(ln.split()[0], 16) for ln in open(functions) if ln.strip()}
    vt = {s['value'] + 8: KINDS[s['name']] for s in e.dynsyms() if s['name'] in KINDS}
    data = [s for s in e.section_list() if s['name'] in ('.rodata', '.data')]

    def words(s):
        return s['addr'], struct.unpack_from(f'<{s["size"] // 4}I', e.d, s['off'])

    def cstr(a):
        try:
            b = e.read_va(a, 512)
        except KeyError:
            return None
        b = b[:b.find(b'\0')] if b'\0' in b else None
        return b.decode('ascii') if b and all(0x20 < c < 0x7f for c in b) else None

    def word(a):
        return struct.unpack('<I', e.read_va(a, 4))[0]

    ti = {}                                    # typeinfo addr -> dict
    for s in data:
        base, ws = words(s)
        for i, w in enumerate(ws[:-1]):
            if w in vt:
                name = cstr(ws[i + 1])
                if not name or not (name[0].isdigit() or name[0] == 'N'):
                    continue
                a = base + 4 * i
                bases = []
                if vt[w] == 'si':
                    bases = [(word(a + 8), 0)]
                elif vt[w] == 'vmi':
                    n = word(a + 12)
                    bases = [(word(a + 16 + 8 * k), word(a + 20 + 8 * k)) for k in range(min(n, 32))]
                ti[a] = dict(mangled=name, kind=vt[w], bases=bases, vtables=[])
    names = demangle([t['mangled'] for t in ti.values()])
    for t in ti.values():
        t['name'] = names[t['mangled']]
    for s in data:
        base, ws = words(s)
        for i in range(1, len(ws) - 1):
            if ws[i] in ti and ws[i - 1] >= 0xfff00000 or ws[i] in ti and ws[i - 1] == 0:
                a = base + 4 * (i - 1)
                if a in ti:                       # a typeinfo, not a vtable
                    continue
                slots, k = [], i + 1
                while k < len(ws) and ws[k] in funcs:
                    slots.append(ws[k])
                    k += 1
                if slots:
                    off = ws[i - 1] - (1 << 32) if ws[i - 1] >= 0x80000000 else ws[i - 1]
                    ti[ws[i]]['vtables'].append((a, off, slots))

    def ancestors(t, seen=None):
        seen = seen if seen is not None else set()
        for b, _ in ti[t]['bases']:
            if b in ti and b not in seen:
                seen.add(b)
                ancestors(b, seen)
        return seen

    owners = {}
    for a, t in ti.items():
        for _, off, slots in t['vtables']:
            for n, f in enumerate(slots):
                owners.setdefault(f, []).append((a, n))
    vf = {}
    for f, cs in owners.items():
        classes = {c for c, _ in cs}
        best = [c for c in classes if not (ancestors(c) & classes)]
        c = min(best, key=lambda c: len(ancestors(c)))
        vf[f] = (ti[c]['name'], min(n for cc, n in cs if cc == c))
    os.makedirs(outdir, exist_ok=True)
    with open(os.path.join(outdir, 'classes.txt'), 'w') as f:
        for a, t in sorted(ti.items(), key=lambda x: x[1]['name']):
            bs = ', '.join(ti[b]['name'] if b in ti else f'{b:#x}' for b, _ in t['bases'])
            f.write(f'{t["name"]}  typeinfo {a:#x} ({t["kind"]})' + (f'  : {bs}' if bs else '') + '\n')
            for va, off, slots in t['vtables']:
                f.write(f'    vtable {va:#x} offset {off}: ' + ' '.join(f'{x:#x}' for x in slots) + '\n')
    with open(os.path.join(outdir, 'vfuncs.txt'), 'w') as f:
        for a, (c, n) in sorted(vf.items()):
            f.write(f'{a:#x} {c} {n}\n')
    print(f'{len(ti)} classes, {sum(len(t["vtables"]) for t in ti.values())} vtables, '
          f'{len(vf)} virtual functions attributed')


if __name__ == '__main__':
    main(*sys.argv[1:4])
