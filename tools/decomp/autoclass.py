"""Generate a C++ skeleton for a class whose virtual functions are trivial, from rbp's vtable.

usage: autoclass.py CLASS [CLASS...]     (run from decomp/; reads rtti/classes.txt)

Classes without bases get a full skeleton; derived classes a flattened one holding only the
slots RTTI attributes to them (no destructors; inheritance is irrelevant to trivial bodies). Each vtable slot is classified:
  0xecc4 (the __cxa_pure_virtual PLT stub)  -> pure virtual
  bx lr                                      -> empty body
  mov/mvn r0, #imm ; bx lr                   -> return imm
  ldr r0, [r0, #off] ; bx lr                 -> return a field at off
  str r1, [r0, #off] ; bx lr                 -> set a field at off
  slots 0/1                                  -> destructors (empty body)
  anything else                              -> declared, not defined (stays assembly)
Writes src/auto/auto_<ns>_<Class>.{h,cpp} and obj/auto_<ns>_<Class>.pairs (name=addr of the
functions we define), for compiling, matching and grafting like a hand-written file.
"""
import os
import re
import struct
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from elfinfo import Elf

PURE = 0xecc4
e = Elf('rbp.orig')


def words(a, n):
    return struct.unpack(f'<{n}I', e.read_va(a, 4 * n))


def classify(a):
    if a == PURE:
        return ('pure',)
    w0, w1 = words(a, 2)
    if w0 == 0xe12fff1e:
        return ('empty',)
    if w1 == 0xe12fff1e:
        if w0 & 0xfffff000 == 0xe3a00000:                       # mov r0, #imm
            imm = w0 & 0xff
            rot = (w0 >> 8) & 0xf
            return ('ret', (imm >> (2 * rot)) | (imm << (32 - 2 * rot)) & 0xffffffff)
        if w0 & 0xfffff000 == 0xe3e00000:                       # mvn r0, #imm
            imm = w0 & 0xff
            rot = (w0 >> 8) & 0xf
            v = (imm >> (2 * rot)) | (imm << (32 - 2 * rot)) & 0xffffffff
            return ('ret', ~v & 0xffffffff)
        if w0 & 0xfffff000 == 0xe5900000:                       # ldr r0, [r0, #off]
            return ('get', w0 & 0xfff)
        if w0 & 0xfffff000 == 0xe5801000:                       # str r1, [r0, #off]
            return ('set', w0 & 0xfff)
        if w0 & 0xfffff000 == 0xe5d00000:                       # ldrb r0, [r0, #off]
            return ('getb', w0 & 0xfff)
        if w0 & 0xfffff000 == 0xe5c01000:                       # strb r1, [r0, #off]
            return ('setb', w0 & 0xfff)
        if w0 & 0xfffff000 == 0xe2800000:                       # add r0, r0, #imm
            imm = w0 & 0xff
            rot = (w0 >> 8) & 0xf
            return ('addr', (imm >> (2 * rot)) | (imm << (32 - 2 * rot)) & 0xffffffff)
    return ('other',)


def mangle_fn(ns, cls, name, params):
    parts = ns + [cls]
    qual = 'N' + ''.join(f'{len(p)}{p}' for p in parts) + f'{len(name)}{name}E'
    return '_Z' + qual + (params or 'v')


def generate(full):
    lines = [l for l in open('rtti/classes.txt')]
    i = next(k for k, l in enumerate(lines) if l.startswith(full + '  typeinfo'))
    derived = '(class)' not in lines[i]
    owned = {int(l.split()[0], 16) for l in open('rtti/vfuncs.txt') if l.split(None, 1)[1].rsplit(None, 1)[0] == full}
    if i + 1 >= len(lines) or not lines[i + 1].startswith('    vtable'):
        print(f'{full}: no vtable')
        return
    vt = [int(x, 16) for x in lines[i + 1].split(':', 1)[1].split()]
    parts = full.split('::')
    ns, cls = parts[:-1], parts[-1]
    fields = {}
    bytefields = {}
    decls, defs, pairs = [], [], []
    for slot, a in enumerate(vt):
        if derived and (slot < 2 or a not in owned):
            continue                # flattened skeleton: only this class's own trivial overrides
        if slot == 0:
            decls.append(f'        virtual ~{cls}();')
            defs.append(f'{cls}::~{cls}() {{}}')
            pairs.append((f'_Z' + 'N' + ''.join(f'{len(p)}{p}' for p in parts) + 'D1Ev', a))
            continue
        if slot == 1:
            pairs.append((f'_Z' + 'N' + ''.join(f'{len(p)}{p}' for p in parts) + 'D0Ev', a))
            continue
        k = classify(a)
        name = f'slot{slot}'
        if k[0] == 'pure':
            decls.append(f'        virtual void {name}() = 0;')
        elif k[0] == 'empty':
            decls.append(f'        virtual void {name}();')
            defs.append(f'void {cls}::{name}() {{}}')
            pairs.append((mangle_fn(ns, cls, name, ''), a))
        elif k[0] == 'ret':
            v = k[1] - (1 << 32) if k[1] >= 0x80000000 else k[1]
            decls.append(f'        virtual int {name}();')
            defs.append(f'int {cls}::{name}() {{ return {v}; }}')
            pairs.append((mangle_fn(ns, cls, name, ''), a))
        elif k[0] == 'get':
            fields[k[1]] = f'field{k[1]:x}'
            decls.append(f'        virtual int {name}();')
            defs.append(f'int {cls}::{name}() {{ return field{k[1]:x}; }}')
            pairs.append((mangle_fn(ns, cls, name, ''), a))
        elif k[0] == 'set':
            fields[k[1]] = f'field{k[1]:x}'
            decls.append(f'        virtual void {name} (int value);')
            defs.append(f'void {cls}::{name} (int value) {{ field{k[1]:x} = value; }}')
            pairs.append((mangle_fn(ns, cls, name, 'i'), a))
        elif k[0] == 'getb':
            bytefields[k[1]] = f'flag{k[1]:x}'
            decls.append(f'        virtual bool {name}();')
            defs.append(f'bool {cls}::{name}() {{ return flag{k[1]:x}; }}')
            pairs.append((mangle_fn(ns, cls, name, ''), a))
        elif k[0] == 'setb':
            bytefields[k[1]] = f'flag{k[1]:x}'
            decls.append(f'        virtual void {name} (bool value);')
            defs.append(f'void {cls}::{name} (bool value) {{ flag{k[1]:x} = value; }}')
            pairs.append((mangle_fn(ns, cls, name, 'b'), a))
        elif k[0] == 'addr':
            decls.append(f'        virtual void* {name}();')
            defs.append(f'void* {cls}::{name}() {{ return (char*) this + {k[1]}; }}')
            pairs.append((mangle_fn(ns, cls, name, ''), a))
        else:
            decls.append(f'        virtual void {name}();          // {a:#x}, not trivial')
    # fields after the vptr, padded to their offsets
    body, off = [], 4
    allf = {o: ('int', n, 4) for o, n in fields.items()}
    allf.update({o: ('bool', n, 1) for o, n in bytefields.items() if o not in fields})
    for o in sorted(allf):
        t, n, size = allf[o]
        if o < off or (size == 4 and o % 4):
            continue
        if o > off:
            body.append(f'        char pad{off:x}[{o - off}];')
        body.append(f'        {t} {n};')
        off = o + size
    guard = '_'.join(parts).upper() + '_AUTO_H'
    open_ns = ''.join(f'namespace {n} {{ ' for n in ns)
    close_ns = '}' * len(ns)
    base = '_'.join(parts)
    h = [f'// Generated by scripts/autoclass.py from rbp\'s vtable for {full} (names are slot numbers).',
         f'#ifndef {guard}', f'#define {guard}', '', open_ns, f'    class {cls}', '    {', '    public:']
    h += decls
    if body:
        h += ['    protected:'] + body
    h += ['    };', close_ns, '', '#endif', '']
    c = [f'// Generated by scripts/autoclass.py for {full}.', f'#include "auto_{base}.h"', '', open_ns] + \
        ['    ' + d for d in defs] + [close_ns, '']
    os.makedirs('src/auto', exist_ok=True)
    open(f'src/auto/auto_{base}.h', 'w').write('\n'.join(h))
    open(f'src/auto/auto_{base}.cpp', 'w').write('\n'.join(c))
    open(f'obj/auto_{base}.pairs', 'w').write(''.join(f'{n}={a:#x}\n' for n, a in pairs))
    print(f'{full}: {len(pairs)} functions')


for full in sys.argv[1:]:
    if not re.match(r'^[A-Za-z_][\w:]*$', full):
        print(f'{full}: unusual name, skipped')
        continue
    try:
        generate(full)
    except StopIteration:
        print(f'{full}: not found')
