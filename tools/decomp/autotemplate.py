"""Find library template instances (std:: and juce containers) in rbp by compiling one member at a time.

An explicit instantiation of a single member (`template void std::vector<A*>::_M_insert_aux (...);`)
in a file of its own reproduces the implicit instance the compiler emitted wherever the template
was used; instantiating the whole class changes the inlining, because the helpers gain callers.
Every (member, element type) pair below is compiled into its own object under obj/autotemplate/,
findsig.py locates each function in rbp.orig (masked signatures, every hit), and the hits that
grafts.txt does not cover yet are printed as `ADDR  member  type`. The element type only matters
for its size and for what the member does with it (copy, compare, destroy), so a placeholder
type stands in for every pointer type.

usage (from decomp/): autotemplate.py [--juce INCLUDE_DIR]
"""
import itertools
import os
import subprocess
import sys

FLAGS = '-O2 -mcpu=cortex-a8 -marm -mfpu=neon -mfloat-abi=softfp'.split()
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = 'obj/autotemplate'

STD_TYPES = {'int': 'int', 'short': 'short', 'uchar': 'unsigned char', 'float': 'float',
             'double': 'double', 'll': 'long long', 'ptr': 'P*', 'str': 'std::string'}
STD_MEMBERS = {
    'vector': [
        'template void std::vector<{T}>::_M_insert_aux (std::vector<{T}>::iterator, {T} const&);',
        'template void std::vector<{T}>::_M_fill_insert (std::vector<{T}>::iterator, std::size_t, {T} const&);',
        'template void std::vector<{T}>::reserve (std::size_t);',
        'template std::vector<{T}>& std::vector<{T}>::operator= (const std::vector<{T}>&);',
        'template std::vector<{T}>::iterator std::vector<{T}>::erase (std::vector<{T}>::iterator);',
    ],
    'list': [
        'template void std::_List_base<{T}, std::allocator<{T}> >::_M_clear ();',
        'template void std::list<{T}>::remove (const {T}&);',
    ],
    'deque': [
        'template void std::deque<{T}>::_M_push_back_aux (const {T}&);',
        'template void std::_Deque_base<{T}, std::allocator<{T}> >::_M_initialize_map (std::size_t);',
        'template void std::deque<{T}>::_M_reallocate_map (std::size_t, bool);',
    ],
}
JUCE_TYPES = {'ptr': 'P*', 'int': 'int', 'float': 'float', 'str': 'juce::String'}
JUCE_MEMBERS = [
    'template juce::Array<{T}>::~Array ();',
    'template void juce::Array<{T}>::minimiseStorageOverheads ();',
    'template juce::OwnedArray<V>::~OwnedArray ();',
]


def compile_one(name, source, extra):
    src, obj = f'{OUT}/{name}.cpp', f'{OUT}/{name}.o'
    with open(src, 'w') as f:
        f.write(source)
    r = subprocess.run(['./cc11.sh', src, obj] + extra + FLAGS, capture_output=True, text=True)
    return obj if r.returncode == 0 and os.path.exists(obj) else None


def main():
    os.makedirs(OUT, exist_ok=True)
    juce = sys.argv[sys.argv.index('--juce') + 1] if '--juce' in sys.argv else None
    objs = []
    for (header, insts), (tk, t) in itertools.product(STD_MEMBERS.items(), STD_TYPES.items()):
        for i, inst in enumerate(insts):
            name = f'{header}{i}_{tk}'
            source = f'#include <{header}>\n#include <string>\nstruct P;\n' + inst.replace('{T}', t) + '\n'
            obj = compile_one(name, source, [])
            if obj:
                objs.append(obj)
    if juce:
        for (i, inst), (tk, t) in itertools.product(enumerate(JUCE_MEMBERS), JUCE_TYPES.items()):
            name = f'juce{i}_{tk}'
            source = ('#include <juce.h>\nstruct P;\nstruct V { virtual ~V(); };\n'
                      + inst.replace('{T}', t) + '\n')
            obj = compile_one(name, source, [f'-I{juce}'])
            if obj:
                objs.append(obj)
    grafted = {int(l.split()[0], 16) for l in open('grafts.txt') if l.strip() and l[0] != '#'}
    env = dict(os.environ, FINDSIG_ALL='1')
    out = subprocess.run([sys.executable, os.path.join(HERE, 'findsig.py'), 'rbp.orig'] + objs,
                         capture_output=True, text=True, env=env).stdout
    seen = set()
    for line in out.splitlines():
        parts = line.split()
        if not parts or parts[0] not in ('FOUND', 'MULTI') or '_GLOBAL__sub_I' in line:
            continue
        hits = [int(parts[1], 16)] if parts[0] == 'FOUND' else [int(h, 16) for h in parts[3:]]
        for h in hits:
            if h not in grafted and h not in seen:
                seen.add(h)
                print(f'{h:#x}  {parts[2]}')
    print(f'{len(seen)} ungrafted instances', file=sys.stderr)


if __name__ == '__main__':
    main()
