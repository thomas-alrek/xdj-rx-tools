"""Insert peephole2-rotation padding before functions that differ only in a scratch register.

usage: peep2pad.py SRC PAIRS PADTEXT FLAGS...   (run from decomp/)

Why: GCC 4.6 picks peephole2 scratch registers starting at a static offset that advances after
every use in the file, so a function only gets the original register if the same number of
scratches were taken before it.
For each pair (in address order) whose function doesn't match, tries 1..3 copies of PADTEXT
(a function definition with `@` where a unique number goes) right before the function's
definition in SRC, keeping the count that makes it match without losing earlier matches.
PADTEXT should take its scratch the same way the functions do. Writes SRC in place."""
import re, subprocess, sys
src, pairs_path, pad = sys.argv[1:4]
flags = sys.argv[4:]
pairs = [l.strip() for l in open(pairs_path) if l.strip()]
pairs.sort(key=lambda p: int(p.split('=')[1], 16))
counter = [0]

def matches(text):
    open('obj/_peep2.cpp', 'w').write(text)
    r = subprocess.run(['./cc11.sh', 'obj/_peep2.cpp', 'obj/_peep2.o'] + flags, capture_output=True, text=True)
    if r.returncode:
        return set()
    out = subprocess.run(['.venv/bin/python', '../tools/decomp/match.py', 'rbp.orig', 'obj/_peep2.o'] + pairs,
                         capture_output=True, text=True).stdout
    return {l.split()[1] for l in out.split('\n') if l.startswith('MATCH')}

text = open(src).read().replace('#include "', '#include "../' + src.rsplit('/', 1)[0] + '/')
base = matches(text)
counter[0] = text.count("peep2pad") + 100
print(len(base), 'match before')
for p in pairs:
    name = p.split('=')[0]
    if name in base:
        continue
    parts, i = [], name.find('N') + 1
    while i and i < len(name) and name[i].isdigit():
        j = i
        while name[j].isdigit():
            j += 1
        n = int(name[i:j])
        parts.append(name[j:j + n])
        i = j + n
    short = parts[-1] if parts else None
    d = re.search(r'\n[^\n]*::' + re.escape(short) + r' \(', text) if short else None
    if not d:
        continue
    for k in (1, 2, 3):
        pads = ''.join(pad.replace('@', str(counter[0] + i)) + '\n' for i in range(k))
        trial = text[:d.start() + 1] + '// peephole2 rotation padding (stands in for functions not reconstructed yet)\n' + pads + text[d.start() + 1:]
        got = matches(trial)
        if name in got and len(got) > len(base):
            text, base = trial, got
            counter[0] += k
            print(f'{short}: {k} pad(s) -> {len(base)} match')
            break
open(src, 'w').write(text.replace('#include "../' + src.rsplit('/', 1)[0] + '/', '#include "'))
print(len(base), 'match after')
