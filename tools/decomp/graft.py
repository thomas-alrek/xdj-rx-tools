"""Graft compiled C/C++ functions into a split tree, in place of their assembly.

usage: graft.py prep SPLIT OUT GRAFTS      (after compiling the sources to OUT/cc/<stem>.s)
       graft.py resolve OUT ORIGINAL       (after assembling OUT)

GRAFTS has one line per function: `ADDR SYMBOL SOURCE [compiler flags...]` (# comments allowed).
The sources are compiled with -S -ffunction-sections by the caller (the build script, which
knows the toolchain); OUT/cc/<stem of SOURCE>.s must exist before `prep`.

prep    copies the split tree to OUT without each grafted function's assembly, and extracts
        each function from its compiler output into OUT/graft_<addr>.s, in the section
        `.rbp.text.<addr>`, so the linker script's address sort puts it where the assembly
        was. Unwind directives are dropped (the unwind tables are still taken from the
        original). Local labels it references but doesn't define (string literals, statics)
        become undefined globals `__g<addr>_<label>`. Labels of the removed assembly become
        aliases of the new function.
resolve gives every undefined symbol of the grafts an address, inferred from the original
        bytes at the same offset (literal-pool words, call targets, movw/movt), checks the
        inferences agree with each other and with the split's labels, compares each graft
        with the original function (relocated words masked), and writes OUT/link.ld and
        OUT/inferred.txt (names learnt from matching grafts: `ADDR NAME`).
"""
import os
import re
import shutil
import struct
import sys
from bisect import bisect_right

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from elfinfo import Elf  # noqa: E402

UNWIND = ('.fnstart', '.fnend', '.save', '.setfp', '.pad', '.vsave', '.movsp', '.personality',
          '.personalityindex', '.cantunwind', '.unwind_raw')
HEADER = ('.arch', '.cpu', '.fpu', '.eabi_attribute', '.syntax', '.arm')


def read_grafts(path):
    out = []
    for line in open(path):
        line = line.split('#')[0].split()
        if line:
            out.append((int(line[0], 16), line[1], line[2], line[3:]))
    return out


def stem(src):
    return os.path.splitext(os.path.basename(src))[0]


def read_labels(split):
    labels = {}
    for line in open(os.path.join(split, 'labels.txt')):
        a, n = line.split()
        labels[int(a, 16)] = n
    return labels


def extract(asm, sym, addr):
    lines = open(asm).read().split('\n')
    head = [ln for ln in lines if ln.strip().split(' ')[0].split('\t')[0] in HEADER]
    # the function runs from its `.type sym, %function` (plus the .align/.global/.weak lines
    # just before it) to its `.size sym, .-sym`; this works for function sections and for
    # shared ones like .text.startup (static initialisers)
    start = end = None
    for i, ln in enumerate(lines):
        p = ln.replace(',', ' ').split()
        if p[:3] == ['.type', sym, '%function']:
            start = i
        elif start is not None and p[:2] == ['.size', sym]:
            end = i + 1
            break
    if start is None or end is None:
        # complete-object ctors/dtors (C1/D1) are `.set` aliases of the base ones (C2/D2)
        for ln in lines:
            p = ln.replace(',', ' ').split()
            if p[:2] == ['.set', sym] and len(p) == 3 or len(p) == 3 and p[0] == sym and p[1] == '=':
                return extract(asm, p[2], addr) + [f'\t.global {sym}', f'\t.set {sym}, {p[2]}']
        raise SystemExit(f'{asm}: function {sym} not found')
    while start > 0 and (lines[start - 1].split() or [''])[0] in ('.align', '.global', '.weak', '.hidden', '.p2align'):
        start -= 1
    body, skip = [], False
    for ln in lines[start:end]:
        d = ln.split()[0] if ln.split() else ''
        if d == '.handlerdata':
            skip = True
            continue
        if skip:
            skip = d != '.fnend'
            continue
        if d in UNWIND:
            continue
        if d == '.section':                     # back from exception data: our own section
            ln = f'\t.section .rbp.text.{addr:08x},"ax",%progbits'
        body.append(ln)
    defined = {m.group(1) for ln in body for m in [re.match(r'^(\.L\w+):', ln)] if m}
    used = set(re.findall(r'\.L\w+', '\n'.join(body)))

    # each external local reference (with its offset) becomes its own undefined symbol, so a
    # section anchor shared by several objects resolves per object (rbp's data layout around
    # the anchor need not match ours)
    def ext(m):
        t, off = m.group(1), m.group(2)
        if t in defined or t not in used:
            return m.group(0)
        return f'__g{addr:x}_{t[1:]}' + (f'_{"m" if int(off) < 0 else ""}{abs(int(off))}' if off else '')
    body = [re.sub(r'(\.L\w+)(?:([+-]\d+))?', ext, ln) for ln in body]
    # the compiler names every file's static-array cleanup functions __tcf_0, __tcf_1, ...; a
    # graft's reference to one (a static initialiser's atexit registration) is to its own file's
    # function, so it gets a per-graft name that the rebuild resolves like the .L references
    body = [re.sub(r'\b(__tcf_\d+)\b', lambda m: m.group(1) if m.group(1) == sym else f'__g{addr:x}_{m.group(1)}', ln)
            for ln in body]
    # static functions too: the rest of the tree refers to them by name. A function at 4 mod 8
    # gets 4 bytes of filler first, so 8-byte pool alignment inside it is computed for its real
    # address; normalize() removes the filler (and gas's end-of-section padding) before linking.
    filler = ['\t.space 4'] if addr % 8 == 4 else []
    return head + [f'\t.section .rbp.text.{addr:08x},"ax",%progbits'] + filler + [f'\t.global {sym}'] + body


def prep(split, out, grafts_path):
    grafts = read_grafts(grafts_path)
    labels = read_labels(split)
    starts = sorted(int(ln.split()[0], 16) for ln in open(os.path.join(split, 'functions.txt')))
    gaddr = {a for a, *_ in grafts}
    os.makedirs(out, exist_ok=True)
    objects = [ln.strip() for ln in open(os.path.join(split, 'objects.txt')) if ln.strip()]
    for n in objects:
        src = open(os.path.join(split, n + '.s')).read().split('\n')
        keep, drop = [], False
        for ln in src:
            m = re.match(r'\t\.section \.rbp\.text\.([0-9a-f]{8}),', ln)
            if m:
                drop = int(m.group(1), 16) in gaddr
            elif ln.startswith('\t.section'):
                drop = False
            if not drop:
                keep.append(ln)
        with open(os.path.join(out, n + '.s'), 'w') as f:
            f.write('\n'.join(keep))
    aliases = []
    for addr, sym, src, _ in grafts:
        body = extract(os.path.join(out, 'cc', stem(src) + '.s'), sym, addr)
        with open(os.path.join(out, f'graft_{addr:x}.s'), 'w') as f:
            f.write(f'@ {sym} from {src}, grafted at {addr:#x} by tools/decomp/graft.py\n')
            f.write('\n'.join(body) + '\n')
        objects.append(f'graft_{addr:x}')
        i = bisect_right(starts, addr)
        end = starts[i] if i < len(starts) else addr
        for a, n in labels.items():
            if addr <= a < end and n != sym:
                aliases.append(f'{n} = {sym} + {a - addr:#x};')
    with open(os.path.join(out, 'objects.txt'), 'w') as f:
        f.write('\n'.join(objects) + '\n')
    shutil.copy(os.path.join(split, 'link.ld'), os.path.join(out, 'link.base.ld'))
    shutil.copy(os.path.join(split, 'labels.txt'), os.path.join(out, 'labels.txt'))
    with open(os.path.join(out, 'aliases.ld'), 'w') as f:
        f.write('\n'.join(aliases) + '\n')
    with open(os.path.join(out, 'grafts.txt'), 'w') as f:
        f.write(''.join(f'{a:#x} {s}\n' for a, s, *_ in grafts))
    print(f'{len(grafts)} grafts prepared, {len(aliases)} aliases')


def normalize(path, addr, sym):
    """Trim a graft object's section to the function: drop the leading filler (functions at
    4 mod 8) and the padding gas adds at the end of an 8-aligned code section; lower the section
    alignment to 4. Relocations and symbols are shifted to match. Rewrites the file in place."""
    e = Elf(path)
    d = bytearray(e.d)
    shoff, = struct.unpack_from('<I', d, 0x20)
    shentsize, = struct.unpack_from('<H', d, 0x2e)
    secs = e.section_list()
    name = f'.rbp.text.{addr:08x}'
    idx = next(i for i, s in enumerate(secs) if s['name'] == name)
    table = e.symbols()
    f = next((t for t in table if t['name'] == sym and t['shndx'] == idx and t['size']), None)
    if f is None:
        return
    start = 4 if addr % 8 == 4 else 0
    end = max(f['value'] + f['size'], max((t['value'] + t['size'] for t in table
                                           if t['shndx'] == idx and t['type'] == 2), default=0))
    sh = shoff + idx * shentsize
    struct.pack_into('<I', d, sh + 16, secs[idx]['off'] + start)
    struct.pack_into('<I', d, sh + 20, end - start)
    struct.pack_into('<I', d, sh + 32, 4)
    if start:
        symtab = e.sec('.symtab')
        for k in range(symtab['size'] // 16):
            o = symtab['off'] + 16 * k
            if struct.unpack_from('<H', d, o + 14)[0] == idx:
                v, = struct.unpack_from('<I', d, o + 4)
                struct.pack_into('<I', d, o + 4, max(v - start, 0))
        rel = e.sections.get('.rel' + name)
        if rel:
            symtab = e.sec('.symtab')
            for k in range(rel['size'] // 8):
                o = rel['off'] + 8 * k
                v, info = struct.unpack_from('<II', d, o)
                struct.pack_into('<I', d, o, v - start)
                # REL addends live in the section data: an R_ARM_ABS32 against this section's own
                # symbol (a jump table entry) points into the section and loses the filler too
                so = symtab['off'] + 16 * (info >> 8)
                if info & 0xff == 2 and struct.unpack_from('<H', d, so + 14)[0] == idx \
                        and d[so + 12] & 0xf == 3:
                    w = secs[idx]['off'] + v
                    a, = struct.unpack_from('<I', d, w)
                    struct.pack_into('<I', d, w, a - start)
    open(path, 'wb').write(bytes(d))


def branch_dest(w, p):
    off = w & 0xffffff
    if off & 0x800000:
        off -= 0x1000000
    return p + 8 + off * 4


def resolve(out, original):
    orig = Elf(original)
    labels = read_labels(out)
    by_name = {n: a for a, n in labels.items()}
    grafts = [(int(a, 16), s) for a, s in (ln.split() for ln in open(os.path.join(out, 'grafts.txt')))]
    gnames = {s for _, s in grafts}
    syms, problems, lines, good = {}, [], [], {}
    for addr, sym in grafts:
        normalize(os.path.join(out, f'graft_{addr:x}.o'), addr, sym)
        obj = Elf(os.path.join(out, f'graft_{addr:x}.o'))
        sname = f'.rbp.text.{addr:08x}'
        sec = obj.sec(sname)
        code = obj.d[sec['off']:sec['off'] + sec['size']]
        table = obj.symbols()
        rel = {}
        halves = {}
        mine = {}
        for off, typ, si in obj.rels('.rel' + sname):
            rel[off] = typ
            s = table[si]
            ow, = struct.unpack_from('<I', orig.read_va(addr + off, 4))
            gw, = struct.unpack_from('<I', code, off)
            if s['shndx'] != 0 or typ == 40:        # resolved inside the graft / R_ARM_V4BX
                continue
            if typ == 2:
                v = (ow - gw) & 0xffffffff
            elif typ in (1, 28, 29):
                a = gw & 0xffffff
                a = (a - 0x1000000 if a & 0x800000 else a) * 4
                v = branch_dest(ow, addr + off) - a - 8
            elif typ in (43, 44):
                imm = lambda w: ((w >> 4) & 0xf000) | (w & 0xfff)  # noqa: E731
                halves.setdefault(s['name'], [None, None])[typ - 43] = (imm(ow) - imm(gw)) & 0xffff
                continue
            else:
                problems.append(f'{sym}+{off:#x}: relocation type {typ} for {s["name"]} not handled')
                continue
            mine.setdefault(s['name'], set()).add(v)
        for n, (lo, hi) in halves.items():
            if lo is None or hi is None:
                problems.append(f'{sym}: {n} has only half of a movw/movt pair')
            else:
                mine.setdefault(n, set()).add(hi << 16 | lo)
        # compare with the original, relocated words masked
        diff = 0
        for i in range(0, len(code) - len(code) % 4, 4):
            if i in rel:
                continue
            gw, = struct.unpack_from('<I', code, i)
            ow, = struct.unpack_from('<I', orig.read_va(addr + i, 4))
            if gw != ow and not ((gw >> 24) & 0xf == 0xb and (gw ^ ow) >> 24 == 0):
                diff += 1
        for n, vs in mine.items():
            syms.setdefault(n, set()).update(vs)
            if not diff:
                good.setdefault(n, set()).update(vs)
        print(f'{"MATCH" if not diff else "DIFF "} {addr:#x} {sym} ({len(code)} bytes'
              + (f', {diff} words differ)' if diff else ')'))
    for n, vs in sorted(syms.items()):
        if len(vs) > 1:
            problems.append(f'{n}: inferred addresses disagree: ' + ' '.join(map(hex, sorted(vs))))
            continue
        v = vs.pop()
        if n in gnames:
            continue
        if n in by_name:
            if by_name[n] != v:
                problems.append(f'{n}: split label at {by_name[n]:#x}, graft needs {v:#x}')
            continue
        lines.append(f'{n} = {v:#x};' + (f'  /* = {labels[v]} */' if v in labels else ''))
    # names learnt from matching grafts (real symbols only), for naming and further grafts
    with open(os.path.join(out, 'inferred.txt'), 'w') as f:
        for n, vs in sorted(good.items()):
            if len(vs) == 1 and not n.startswith('__g') and n not in gnames:
                f.write(f'{min(vs):#x} {n}\n')
    base = open(os.path.join(out, 'link.base.ld')).read()
    aliases = open(os.path.join(out, 'aliases.ld')).read()
    with open(os.path.join(out, 'link.ld'), 'w') as f:
        f.write(base + '\n/* aliases for labels of grafted-over assembly */\n' + aliases
                + '\n/* addresses inferred from the original bytes */\n' + '\n'.join(lines) + '\n')
    for p in problems:
        print('PROBLEM', p)
    print(f'{len(lines)} symbols inferred, {len(problems)} problems')
    return 1 if problems else 0


if __name__ == '__main__':
    cmd = sys.argv[1]
    if cmd == 'prep':
        prep(*sys.argv[2:5])
    elif cmd == 'resolve':
        sys.exit(resolve(*sys.argv[2:4]))
    else:
        sys.exit(__doc__)
