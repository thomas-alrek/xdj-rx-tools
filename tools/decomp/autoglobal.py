"""Generate small C wrappers that forward to an object reached through a global pointer.

Shape (with variations in argument shuffles and what happens to the result):
    ldr rX, =global ; ldr rY, [rX] ; cmp rY, #0 ; return 0 if null
    ldr r0, [rY, #off] ; cmp r0, #0 ; return 0 if null
    b/bl target (r0 = the member object, r1.. = the wrapper's arguments)
These come from C-style API files ("if (g && g->impl) return g->impl->f(args);"). The exact
source of each is not decided by the shape alone, so every candidate is compiled in a set of
variants (argument count, null checks through a local or the global, return type of the callee,
a clamp or constant after the call) and the first variant that matches byte for byte is kept.
Writes src/auto/auto_global.cpp and obj/auto_global.pairs. Run from decomp/.
"""
import os
import re
import struct
import subprocess
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from elfinfo import Elf

FLAGS = ('-O2 -mcpu=cortex-a8 -marm -mfpu=neon -mfloat-abi=softfp ' + __import__('os').environ.get('EXTRA_FLAGS', '')).split()
OUT = __import__('os').environ.get('AUTOGLOBAL_OUT', 'src/auto/auto_global.cpp')
e = Elf('rbp.orig')
starts = sorted(int(l.split()[0], 16) for l in open('split/functions.txt') if l.startswith('0x'))
size = {a: b - a for a, b in zip(starts, starts[1:])}
grafted = {int(l.split()[0], 16) for l in open('grafts.txt') if l.strip() and l[0] != '#'
           and l.split()[2] != OUT}
labels = {}
for l in open('split/labels.txt'):
    p = l.split()
    if len(p) == 2:
        labels.setdefault(int(p[0], 16), p[1])


def rot_imm(w):
    imm, rot = w & 0xff, (w >> 8) & 0xf
    return ((imm >> (2 * rot)) | (imm << (32 - 2 * rot))) & 0xffffffff


def branch(w, a):
    off = w & 0xffffff
    return a + 8 + ((off - 0x1000000) if off & 0x800000 else off) * 4


def candidate(a, s):
    """(global label, member offset, target label, constants) or None."""
    if s > 64:
        return None
    w = struct.unpack(f'<{s // 4}I', e.read_va(a, s))
    glob = off = target = None
    consts, rets = [], set()
    for i, x in enumerate(w):
        at = a + 4 * i
        if x & 0x0fff0000 == 0x059f0000 and x >> 28 == 0xe:          # ldr rX, [pc, #imm]
            lit = at + 8 + (x & 0xfff)
            if a <= lit < a + s:
                v = struct.unpack('<I', e.read_va(lit, 4))[0]
                if glob is None and v in labels:
                    glob = labels[v]
        elif x & 0x0ff0f000 == 0x05900000 and x >> 28 == 0xe and off is None and glob:
            if (x >> 16) & 0xf != 15 and (x & 0xfff):              # ldr r0, [rY, #off]
                off = x & 0xfff
        elif x & 0x0e000000 == 0x0a000000 and x >> 28 == 0xe:       # b / bl
            if target is not None:
                return None
            t = branch(x, at)
            if t not in labels:
                return None
            target = labels[t]
            rets |= {'int'} if x & 0x01000000 == 0 else {'ll', 'const'}
        elif x & 0x0ff0f000 == 0x03700000:                              # cmn: a clamp
            rets = {'clamp'}
        elif x & 0x0fff0ff0 == 0x06bf0070:                              # sxth
            rets.add('short')
        elif x & 0x0fe00000 == 0x03a00000 and x & 0xf000 in (0, 0x1000):   # mov r0/r1, #imm
            consts.append(rot_imm(x))
        elif x & 0x0ff00000 == 0x03000000 and x & 0xf000 in (0, 0x1000):   # movw
            consts.append(((x >> 4) & 0xf000) | (x & 0xfff))
        elif x & 0x0fe00000 == 0x03e00000 and x & 0xf000 in (0, 0x1000):   # mvn
            consts.append(~rot_imm(x) & 0xffffffff)
    if glob and off is not None and target:
        return glob, off, target, sorted(set(consts) - {0})[:3], rets
    return None


def variants(consts, rets):
    """(nargs, arg transform, access, return form, null value, K) tuples, most likely first."""
    ks = consts or [1]
    out = []
    for ret in [r for r in ('int', 'll', 'clamp', 'short', 'const') if r in rets]:
        for acc in ('local', 'global'):
            for nargs in (1, 0, 2, 3):
                for xf in ('plain', 'ne0', 'neg1', 'constarg'):
                    if xf != 'plain' and nargs == 0:
                        continue
                    for nul in ('0', 'K') if consts else ('0',):
                        for k in (ks if 'K' in (nul,) or ret == 'const' or xf == 'constarg' else [0]):
                            out.append((nargs, xf, acc, ret, nul, k))
    return out


def source(name, glob, off, target, v, alias):
    nargs, xf, acc, ret, nul, k = v
    params = ', '.join(f'int a{i}' for i in range(nargs)) or 'void'
    args = [f'a{i}' for i in range(nargs)]
    if xf == 'ne0':
        args[-1] = f'a{nargs - 1} != 0'
    elif xf == 'neg1':
        args[-1] = f'a{nargs - 1} != -1'
    elif xf == 'constarg':
        args[-1] = str(k)
    rtype = {'ll': 'long long', 'short': 'short'}.get(ret, 'int')
    proto = ', '.join(['char*'] + ['int'] * len(args))
    z = str(k) if nul == 'K' else '0'
    decl = f'extern {rtype} {alias} ({proto}) __asm__ ("{target}");'
    wtype = 'short' if ret == 'short' else 'int'
    if acc == 'local':
        head = (f'    char* s = {glob}_p; if (!s) return {z};\n'
                f'    char* t = *(char**) (s + {off}); if (!t) return {z};\n')
        obj = 't'
    else:
        head = (f'    if (!{glob}_p || !*(char**) ({glob}_p + {off})) return {z};\n')
        obj = f'*(char**) ({glob}_p + {off})'
    call = f'{alias} ({", ".join([obj] + args)})'
    if ret == 'clamp':
        tail = f'    unsigned r = {call}; if (r >= 0x7fffffffu) r = -1; return r;\n'
    elif ret == 'const':
        tail = f'    {call}; return {k};\n'
    else:
        tail = f'    return {call};\n'
    return decl, f'{wtype} {name} ({params})\n{{\n{head}{tail}}}\n'


def build(path, chunks, globs):
    text = ['// Generated by autoglobal.py: wrappers forwarding through a global object pointer.',
            '// Names are placeholders.', '', 'extern "C" {', '']
    text += [f'extern char* {g}_p __asm__ ("{g}");' for g in sorted(globs)] + ['']
    for decl, body in chunks:
        text += [decl, body]
    text.append('}')
    open(path, 'w').write('\n'.join(text) + '\n')


def compile_match(path, obj, pairs):
    r = subprocess.run(['./cc11.sh', path, obj] + FLAGS, capture_output=True, text=True)
    if r.returncode:
        sys.exit(r.stderr[-2000:])
    out = subprocess.run(['.venv/bin/python', '../tools/decomp/match.py', 'rbp.orig', obj] +
                         [f'{n}={a:#x}' for n, a in pairs], capture_output=True, text=True).stdout
    return {l.split()[1] for l in out.split('\n') if l.startswith('MATCH')}


cands = {}
for a, s in sorted(size.items()):
    if a in grafted or a < 0xeda0 or a >= 0x360000:
        continue
    c = candidate(a, s)
    if c:
        cands[a] = c
print(len(cands), 'candidates')

# Try the variants in rounds, one object per variant index, dropping each function once matched.
found = {}
vs = {a: variants(c[3], c[4]) for a, c in cands.items()}
for i in range(max(len(v) for v in vs.values())):
    todo = [a for a in cands if a not in found and i < len(vs[a])]
    if not todo:
        break
    chunks, globs, pairs = [], set(), []
    for a in todo:
        glob, off, target = cands[a][:3]
        globs.add(glob)
        chunks.append(source(f'glob_{a:x}', glob, off, target, vs[a][i], f'callee_{a:x}'))
        pairs.append((f'glob_{a:x}', a))
    build('obj/_autoglobal.cpp', chunks, globs)
    ok = compile_match('obj/_autoglobal.cpp', 'obj/_autoglobal.o', pairs)
    for a in todo:
        if f'glob_{a:x}' in ok:
            found[a] = vs[a][i]
    print(f'round {i}: {len(todo)} tried, {len(found)} found', flush=True)

chunks, globs = [], set()
for a in sorted(found):
    glob, off, target = cands[a][:3]
    globs.add(glob)
    chunks.append(source(f'glob_{a:x}', glob, off, target, found[a], f'callee_{a:x}'))
build(OUT, chunks, globs)
open(OUT.replace('src/auto/', 'obj/').replace('.cpp', '.pairs'), 'w').write(''.join(f'glob_{a:x}={a:#x}\n' for a in sorted(found)))
print(len(found), 'wrappers')
