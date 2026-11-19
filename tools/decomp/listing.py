"""Print the split listing for an address range compactly: dis.py START END (hex). Run from decomp/."""
import os
import glob, re, sys
lo, hi = int(sys.argv[1], 16), int(sys.argv[2], 16)
for f in sorted(glob.glob('split/text_*.s')):
    first = int(f.split('_')[1][:-2], 16)
    if first >= hi:
        continue
    lab = None
    for line in open(f):
        m = re.search(r'@ ([0-9a-f]+)', line)
        if re.match(r'^[A-Za-z_][\w$.]*:$', line.strip()):
            lab = line.strip()
            continue
        if not m:
            continue
        a = int(m.group(1), 16)
        if lo <= a < hi:
            if lab:
                print(lab)
                lab = None
            line = re.sub(r'^\t\.inst 0x[0-9a-f]+\t@ ', '  ', line.rstrip())
            print(re.sub(r'^\t', '  ', line))
