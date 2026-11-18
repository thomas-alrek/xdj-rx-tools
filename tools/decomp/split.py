"""Split a stripped ARM ELF executable into GNU as sources that relink to the identical bytes.

usage: split.py TARGET FSTARTS OUTDIR [--names FILE] [--chunk BYTES] [--no-disasm]

TARGET   the original executable (also .incbin'd for the sections kept raw)
FSTARTS  candidate function starts, one hex address per line
OUTDIR   receives one .s per data section, text_<addr>.s chunks of whole functions,
         link.ld and objects.txt (link order)
--names  optional `ADDR NAME` lines, or findsig `FOUND ADDR NAME OBJECT SIZE` lines: names, and
         for known sizes the end is a function start and starts inside are dropped

Code sections are emitted word by word as `.inst` (with a disassembly comment), except:
  - b/bl to another function, emitted as `b{cond} label`;
  - movw/movt pairs (same register) whose value is an address, emitted with :lower16:/:upper16:;
  - literal-pool words (targets of pc-relative ldr/ldrd/ldrh/vldr) and switch-table entries
    that hold an address, emitted as `.word label`.
Data sections (.rodata, .data, ...) are emitted as bytes and strings; aligned words whose
value is a known function start or an address inside the data/rodata/bss ranges become
`.word label`. The dynamic-linking tables, unwind tables and .got stay raw (`.incbin`).

Every target gets its own global label at its exact address (f_ / c_ / d_ / b_ / p_ prefixes),
so code can later move without breaking references. False positives (an integer that looks
like an address) still reproduce the same bytes; they only matter once things move.
"""
import os
import struct
import sys
from bisect import bisect_right

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from elfinfo import Elf  # noqa: E402

CODE = {'.init', '.plt', '.text', '.fini'}
DATA = {'.rodata', '.constdata', '.init_array', '.fini_array', '.jcr', '.data'}
COND = ['eq', 'ne', 'cs', 'cc', 'mi', 'pl', 'vs', 'vc', 'hi', 'ls', 'ge', 'lt', 'gt', 'le', '', '']


def literal_ref(w, a):
    """(target, size) when the instruction at a loads from a pc-relative address."""
    if w >> 28 == 0xf:
        return None
    if w & 0x0f7f0000 in (0x051f0000, 0x055f0000):            # ldr / ldrb rt, [pc, #±imm12]
        off = w & 0xfff
        return a + 8 + (off if w & 0x800000 else -off), 4 if not w & 0x400000 else 1
    if w & 0x0f6f0090 == 0x014f0090 and w & 0x60:             # ldrd / ldrh / ldrsb / ldrsh literal
        off = ((w >> 4) & 0xf0) | (w & 0xf)
        size = 8 if not w & 0x100000 and w & 0x60 == 0x40 else 2
        return a + 8 + (off if w & 0x800000 else -off), size
    if w & 0x0f3f0e00 == 0x0d1f0a00:                          # vldr s/d, [pc, #±imm8*4]
        off = (w & 0xff) * 4
        return ((a + 8) & ~3) + (off if w & 0x800000 else -off), 8 if w & 0x100 else 4
    return None


def branch_target(w, a):
    if w >> 28 != 0xf and (w >> 25) & 7 == 5:
        off = w & 0xffffff
        if off & 0x800000:
            off -= 0x1000000
        return a + 8 + off * 4, 'bl' if w & 0x1000000 else 'b'
    return None


NORETURN = {'abort', 'exit', '_exit', '__cxa_throw', '__cxa_rethrow', '__cxa_end_cleanup',
            '__cxa_call_unexpected', '__cxa_pure_virtual', '_Unwind_Resume', '__stack_chk_fail',
            '__assert_fail', 'pthread_exit', 'longjmp', 'siglongjmp', '__cxa_bad_cast',
            '__cxa_bad_typeid', '_ZSt9terminatev', '__aeabi_unwind_cpp_pr0'}


def ends_function(w):
    """Can execution not fall through past this instruction (unconditional)?"""
    if w in (0, 0xe1a00000):                                  # padding
        return True
    if w >> 28 != 0xe:
        return False
    return (w & 0x0ffffff0 == 0x012fff10 or                   # bx rM
            w & 0x0e108000 == 0x08108000 or                   # ldm/pop {..., pc}
            w & 0x0c10f000 == 0x0410f000 or                   # ldr pc, [...]
            w & 0x0f000000 == 0x0a000000 or                   # b
            w & 0x0ffff000 == 0x01a0f000)                     # mov pc, rM


class Splitter:
    def __init__(self, target, fstarts, names):
        self.e = Elf(target)
        self.secs = [s for s in self.e.section_list() if s['flags'] & 2 and s['addr']]
        self.secs.sort(key=lambda s: s['addr'])
        self.starts = [s['addr'] for s in self.secs]
        self.names = names
        self.labels = {}           # addr -> name
        self.used = set()
        self.code_data = set()     # code addresses (word aligned) holding data words
        self.refs = {}             # addr of word -> label target
        self.movw = {}             # addr of movw/movt -> (target, 'lower16' | 'upper16')
        code = [s for s in self.secs if s['name'] in CODE]
        fs = set(fstarts)
        for s in code:
            fs.add(s['addr'])
        self.plt = {}
        plt = self.e.sec('.plt')
        dyn = self.e.dynsyms()
        for i, (off, info) in enumerate(self._relplt()):
            self.plt[plt['addr'] + 20 + 12 * i] = dyn[info >> 8]['name']
        fs |= set(self.plt)
        fs.add(plt['addr'])
        self.fstarts = sorted(a for a in fs if self.code_sec(a))
        self.fset = set(self.fstarts)

    def _relplt(self):
        s = self.e.sec('.rel.plt')
        return [struct.unpack_from('<II', self.e.d, s['off'] + j) for j in range(0, s['size'], 8)]

    def owner(self, a):
        """Section that holds address a (or ends exactly at a)."""
        i = bisect_right(self.starts, a) - 1
        if i >= 0 and a < self.secs[i]['addr'] + self.secs[i]['size']:
            return self.secs[i]
        for s in self.secs:
            if s['addr'] + s['size'] == a:
                return s
        return None

    def code_sec(self, a):
        s = self.owner(a)
        return s if s and s['name'] in CODE and a < s['addr'] + s['size'] else None

    def word(self, a):
        return struct.unpack('<I', self.e.read_va(a, 4))[0]

    def label(self, a):
        if a not in self.labels:
            s = self.owner(a)
            if a in self.plt:
                n = 'p_' + self.plt[a]
            elif a in self.names:
                n = self.names[a]
            elif s['name'] in CODE:
                n = ('f_' if a in self.fset else 'c_') + f'{a:x}'
            elif s['type'] == 8:
                n = f'b_{a:x}'
            else:
                n = f'd_{a:x}'
            if n in self.used:
                n = f'{n}_{a:x}'
            self.used.add(n)
            self.labels[a] = n
        return self.labels[a]

    def func_of(self, a):
        i = bisect_right(self.fstarts, a) - 1
        s = self.code_sec(a)
        end = self.fstarts[i + 1] if i + 1 < len(self.fstarts) else s['addr'] + s['size']
        return self.fstarts[i], min(end, s['addr'] + s['size'])

    def pointer_ok(self, v, strict):
        """Is v a plausible address worth a label? strict: in data, require function starts."""
        s = self.owner(v)
        if s is None or s['addr'] < 0xd918:      # headers, dynamic-linking tables
            return False
        if s['name'] in CODE:
            return v in self.fset or not strict
        return True

    def code_words(self):
        """[(section, base, words)] for the code sections with instructions (.plt excluded)."""
        out = []
        for s in self.secs:
            if s['name'] in CODE and s['name'] != '.plt':
                out.append((s, s['addr'], struct.unpack_from(f'<{s["size"] // 4}I', self.e.d, s['off'])))
        return out

    def find_data(self):
        """Literal pools and switch tables inside code, to a fixed point: a word that is data
        does not count as an instruction (a double constant can decode as ldrh pc-relative)."""
        self.literal = {}          # instruction addr -> (target, size)
        data = set()
        for _ in range(8):
            new, lit = set(), {}
            for s, base, words in self.code_words():
                end = base + 4 * len(words)
                for i, w in enumerate(words):
                    a = base + 4 * i
                    if a in data:
                        continue
                    lr = literal_ref(w, a)
                    if lr:
                        t, n = lr
                        lit[a] = lr
                        new.update(k for k in range(t & ~3, t + n, 4) if base <= k < end)
                    elif w & 0x0ffffff0 == 0x079ff100:        # ldr{c} pc, [pc, rM, lsl #2]: switch table
                        k = a + 8
                        while k < end:
                            v = words[(k - base) // 4]
                            if not (base <= v < end and v % 4 == 0 and abs(v - a) < 0x10000):
                                break
                            new.add(k)
                            k += 4
            if new == data:
                break
            data = new
        self.code_data, self.literal = data, lit

    def prologue_starts(self):
        """`push {..., lr}` right after a function end (or a literal pool): a function start
        (GCC 4.6 never saves lr mid-function). Finds functions reached only through vtables
        whose unwind entries the linker merged with a neighbour's."""
        out = set()
        for s, base, words in self.code_words():
            for i in range(1, len(words)):
                a, w, p = base + 4 * i, words[i], words[i - 1]
                if a in self.code_data or w & 0xffff4000 != 0xe92d4000:
                    continue
                if a - 4 in self.code_data or ends_function(p) or \
                        self.plt.get((branch_target(p, a - 4) or (0,))[0]) in NORETURN:
                    out.add(a)
        return out

    def add_starts(self, starts):
        self.fstarts = sorted(set(self.fstarts) | {a for a in starts if self.code_sec(a)})
        self.fset = set(self.fstarts)

    def prune(self, evidence):
        """Drop candidate starts that can't be function starts. evidence: starts known from
        unwind entries or calls, never dropped for lacking a preceding function end."""
        dropped = {}
        crossed = set()
        starts = self.fstarts
        for a, (t, n) in self.literal.items():
            lo, hi = sorted((a, t))
            for k in range(bisect_right(starts, lo), bisect_right(starts, hi)):
                crossed.add(starts[k])
        keep = []
        for a in starts:
            prev = self.word(a - 4) if self.code_sec(a - 4) else None
            if a in self.plt or self.code_sec(a)['addr'] == a or a == self.e.sec('.plt')['addr']:
                keep.append(a)
            elif a in self.code_data:
                dropped[a] = 'inside a literal pool or switch table'
            elif a in crossed:
                dropped[a] = 'between an instruction and its literal'
            elif a not in evidence and not (prev is None or a - 4 in self.code_data or ends_function(prev)
                                            or self.plt.get((branch_target(prev, a - 4) or (0,))[0]) in NORETURN):
                dropped[a] = 'pointer only, not after a function end'
            else:
                keep.append(a)
        self.fstarts = keep
        self.fset = set(keep)
        return dropped

    def scan(self):
        self.warnings = []
        for a in self.plt:
            self.label(a)
        self.label(self.e.sec('.plt')['addr'])
        for a, (t, n) in self.literal.items():
            lo, hi = self.func_of(a)
            if not (lo <= t < hi):
                self.warnings.append(f'{a:#x}: literal at {t:#x} outside its function {lo:#x}')
        for s, base, words in self.code_words():
            for i, w in enumerate(words):
                a = base + 4 * i
                if a in self.code_data:
                    if self.pointer_ok(w, strict=False):
                        self.refs[a] = w
                    continue
                bt = branch_target(w, a)
                if bt:
                    t, _ = bt
                    lo, hi = self.func_of(a)
                    if not (lo <= t < hi) and self.code_sec(t):
                        self.refs[a] = t
                elif w >> 28 != 0xf and w & 0x0ff00000 == 0x03000000:    # movw rd, #lo16
                    rd = (w >> 12) & 0xf
                    _, hi = self.func_of(a)
                    for j in range(i + 1, min(i + 17, len(words))):
                        b = base + 4 * j
                        if b >= hi or b in self.code_data:
                            break
                        w2 = words[j]
                        if w2 >> 28 == 0xf or (w2 >> 12) & 0xf != rd:
                            continue
                        if w2 & 0x0ff00000 == 0x03400000:                # movt rd, #hi16
                            v = (w2 & 0xf0000) << 12 | (w2 & 0xfff) << 16 | (w & 0xf0000) >> 4 | (w & 0xfff)
                            if self.pointer_ok(v, strict=True):
                                self.movw[a] = (v, 'lower16')
                                self.movw[b] = (v, 'upper16')
                        break                                           # any other write to rd ends it
        for s in self.secs:
            if s['name'] in DATA:
                base = s['addr']
                for a in range((base + 3) & ~3, base + s['size'] - 3, 4):
                    v = self.word(a)
                    if self.pointer_ok(v, strict=True):
                        self.refs[a] = v
        for a, (t, _) in self.movw.items():
            self.label(t)
        for a, t in self.refs.items():
            self.label(t)
        for a in self.fstarts:
            self.label(a)

    # ---- emission ----

    def emit(self, outdir, target_rel, chunk, disasm):
        os.makedirs(outdir, exist_ok=True)
        dis = self._disasm() if disasm else {}
        by_sec = {}
        for a, n in self.labels.items():
            by_sec.setdefault(self.owner(a)['name'], []).append(a)
        files = []       # (filename, input section name, output section, addr)
        for s in self.secs:
            name = s['name']
            labs = sorted(by_sec.get(name, []))
            if name == '.text':
                starts = [a for a in self.fstarts if self.code_sec(a) is s]
                cuts = [starts[0]]
                for a in starts:
                    if a - cuts[-1] >= chunk:
                        cuts.append(a)
                cuts.append(s['addr'] + s['size'])
                for k in range(len(cuts) - 1):
                    lo, hi = cuts[k], cuts[k + 1]
                    fn = f'text_{lo:06x}.s'
                    isec = f'.rbp.text.{lo:08x}'
                    ls = [a for a in labs if lo <= a < hi or (a == hi and k == len(cuts) - 2)]
                    self._write(outdir, fn, isec, s, lo, hi, ls, dis, target_rel)
                    files.append((fn, isec, s))
                continue
            fn = f'sec{name.replace(".", "_")}.s'
            isec = f'.rbp{name}'
            self._write(outdir, fn, isec, s, s['addr'], s['addr'] + s['size'], labs, dis, target_rel)
            files.append((fn, isec, s))
        self._linker_script(outdir, files)
        return files

    def _disasm(self):
        try:
            from capstone import Cs, CS_ARCH_ARM, CS_MODE_ARM
        except ImportError:
            return {}
        cs = Cs(CS_ARCH_ARM, CS_MODE_ARM)
        cs.skipdata = True
        out = {}
        for s in self.secs:
            if s['name'] in CODE:
                code = self.e.d[s['off']:s['off'] + s['size']]
                for a, _, mn, op in cs.disasm_lite(code, s['addr']):
                    out[a] = f'{mn} {op}'.strip()
        return out

    def _write(self, outdir, fn, isec, s, lo, hi, labs, dis, target_rel):
        kind = 'code' if s['name'] in CODE else 'bss' if s['type'] == 8 else \
            'data' if s['name'] in DATA else 'raw'
        flags = {'code': '"ax",%progbits', 'bss': '"aw",%nobits'}.get(
            kind, '"aw",%progbits' if s['flags'] & 1 else '"a",%progbits')
        out = [f'@ {s["name"]} {lo:#x}-{hi:#x} (generated by tools/decomp/split.py)',
               '\t.syntax unified', '\t.arch armv7-a', '\t.fpu neon', '\t.arm',
               f'\t.section {isec},{flags}']
        li = 0
        labs = sorted(labs)

        def put_labels(upto):
            nonlocal li
            while li < len(labs) and labs[li] <= upto:
                n = self.labels[labs[li]]
                if labs[li] in self.fset:
                    out.append('')
                    if s['name'] == '.text':      # one section per function, sorted by address at link
                        out.append(f'\t.section .rbp.text.{labs[li]:08x},"ax",%progbits')
                out.append(f'\t.global {n}\n{n}:')
                li += 1

        a = lo
        while a < hi:
            put_labels(a)
            nxt = labs[li] if li < len(labs) else hi
            nxt = min(nxt, hi)
            if kind == 'bss':
                out.append(f'\t.space {nxt - a:#x}')
                a = nxt
            elif kind == 'raw':
                out.append(f'\t.incbin "{target_rel}", {s["off"] + a - s["addr"]:#x}, {nxt - a:#x}')
                a = nxt
            elif kind == 'code':
                if a % 4 or nxt - a < 4:
                    out.append(f'\t.byte {self.e.read_va(a, 1)[0]:#04x}')
                    a += 1
                    continue
                w = self.word(a)
                # padding before an 8-aligned function (the next object's section alignment)
                # gets its own section, so replacing the function before it keeps the padding
                if s['name'] == '.text' and a + 4 in self.fset and (a + 4) % 8 == 0 and \
                        a not in self.fset and w in (0, 0xe320f000, 0xe1a00000) and a not in self.refs and \
                        a not in self.code_data:
                    out.append(f'\t.section .rbp.text.{a:08x},"ax",%progbits\t@ alignment padding')
                if a in self.refs:
                    t = self.refs[a]
                    if a in self.code_data:
                        out.append(f'\t.word {self.label(t)}\t@ {a:x}')
                    else:
                        _, op = branch_target(w, a)
                        out.append(f'\t{op}{COND[w >> 28]} {self.label(t)}\t@ {a:x}')
                elif a in self.movw:
                    t, half = self.movw[a]
                    op = 'movw' if half == 'lower16' else 'movt'
                    out.append(f'\t{op}{COND[w >> 28]} r{(w >> 12) & 0xf}, #:{half}:{self.label(t)}\t@ {a:x}')
                elif a in self.code_data:
                    out.append(f'\t.word {w:#010x}\t@ {a:x}')
                else:
                    c = dis.get(a)
                    out.append(f'\t.inst {w:#010x}\t@ {a:x}' + (f': {c}' if c else ''))
                a += 4
            else:
                if a in self.refs and nxt - a >= 4:
                    out.append(f'\t.word {self.label(self.refs[a])}')
                    a += 4
                    continue
                # bytes up to the next label or pointer word
                end = nxt
                for k in range((a + 4) & ~3, nxt, 4):
                    if k in self.refs:
                        end = k
                        break
                out.extend(self._bytes(self.e.read_va(a, end - a)))
                a = end
        put_labels(hi)
        with open(os.path.join(outdir, fn), 'w') as f:
            f.write('\n'.join(out) + '\n')

    @staticmethod
    def _bytes(b):
        out, i = [], 0
        while i < len(b):
            j = i
            while j < len(b) and 0x20 <= b[j] < 0x7f:
                j += 1
            if j - i >= 4 and j < len(b) and b[j] == 0:
                txt = b[i:j].decode().replace('\\', '\\\\').replace('"', '\\"')
                out.append(f'\t.asciz "{txt}"')
                i = j + 1
                continue
            k = i + 1
            while k < len(b) and k - i < 16:
                m = k
                while m < len(b) and 0x20 <= b[m] < 0x7f:
                    m += 1
                if m - k >= 4 and m < len(b) and b[m] == 0:
                    break
                k += 1
            out.append('\t.byte ' + ','.join(f'{x:#04x}' for x in b[i:k]))
            i = k
        return out

    def _linker_script(self, outdir, files):
        out = ['/* generated by tools/decomp/split.py: every section at its original address */',
               'ENTRY(_start_rbp)', 'SECTIONS', '{']
        groups = []
        for fn, isec, s in files:
            if groups and groups[-1][0] is s:
                groups[-1][1].append(isec)
            else:
                groups.append((s, [isec]))
        for s, isecs in groups:
            if s['name'] == '.text':
                isecs = ['SORT_BY_NAME(.rbp.text.*)']
            out.append(f'  .rbp{s["name"]} {s["addr"]:#x} : {{ ' +
                       ' '.join(f'*({i})' for i in isecs) + ' }')
        out.append(f'  _start_rbp = {struct.unpack_from("<I", self.e.d, 0x18)[0]:#x};')
        out.append('  /DISCARD/ : { *(.ARM.attributes) }')
        out.append('}')
        with open(os.path.join(outdir, 'link.ld'), 'w') as f:
            f.write('\n'.join(out) + '\n')
        with open(os.path.join(outdir, 'objects.txt'), 'w') as f:
            f.write('\n'.join(fn[:-2] for fn, _, _ in files) + '\n')


def main(argv):
    names, known, chunk, disasm, pos = {}, {}, 0x10000, True, []
    it = iter(argv)
    for x in it:
        if x == '--names':
            for line in open(next(it)):
                p = line.split()
                if p and p[0] == 'FOUND':
                    p = p[1:]
                if len(p) >= 2 and p[0].startswith('0x'):
                    names.setdefault(int(p[0], 16), p[1])
                    if len(p) >= 4 and p[-1].isdigit():
                        known[int(p[0], 16)] = int(p[-1])
        elif x == '--chunk':
            chunk = int(next(it), 0)
        elif x == '--no-disasm':
            disasm = False
        else:
            pos.append(x)
    target, fstarts_path, outdir = pos
    fstarts = [int(line, 16) for line in open(fstarts_path) if line.strip()]
    sp = Splitter(target, fstarts, names)
    sp.find_data()
    evidence = {a for a, _ in sp.e.exidx()}
    for s, base, words in sp.code_words():
        for i, w in enumerate(words):
            bt = branch_target(w, base + 4 * i)
            if bt and bt[1] == 'bl' and base + 4 * i not in sp.code_data:
                evidence.add(bt[0])
    pro = sp.prologue_starts()
    # tail-call targets: a branch from one function into another function's range that lands
    # right after a function end (terminator or literal pool) starts a function of its own
    for s_, base, words in sp.code_words():
        for i, w in enumerate(words):
            a = base + 4 * i
            bt = branch_target(w, a)
            if not bt or a in sp.code_data:
                continue
            t = bt[0]
            ts = sp.code_sec(t)
            if not ts or t in sp.fset or t <= ts['addr'] or t in sp.code_data:
                continue
            lo, hi = sp.func_of(a)
            if lo <= t < hi:
                continue
            p_ = sp.word(t - 4)
            if t - 4 in sp.code_data or ends_function(p_):
                pro.add(t)
    found = len(pro - sp.fset)
    evidence |= pro
    sp.add_starts(pro)
    # functions of known size (matched library code): the end is a start, nothing inside is
    sp.add_starts(known)
    evidence |= set(known)
    inside = {a for a in sp.fstarts for k, n in known.items() if k < a < k + n}
    ends = {k + n for k, n in known.items()}
    sp.add_starts(e for e in ends if not any(k < e < k + n for k, n in known.items()))
    sp.fstarts = [a for a in sp.fstarts if a not in inside]
    sp.fset = set(sp.fstarts)
    dropped = sp.prune(evidence)
    dropped.update((a, 'inside a function of known size') for a in inside)
    print(f'{found} functions found by prologue or as tail-call targets')
    sp.scan()
    target_rel = os.path.relpath(os.path.abspath(target), os.path.abspath(outdir))
    files = sp.emit(outdir, target_rel, chunk, disasm)
    print(f'{len(files)} files, {len(sp.fstarts)} functions ({len(dropped)} candidates dropped), '
          f'{len(sp.labels)} labels, {len(sp.refs)} symbolic words, '
          f'{len(sp.code_data)} data words in code, {len(sp.movw) // 2} movw/movt pairs, '
          f'{len(sp.warnings)} warnings')
    with open(os.path.join(outdir, 'warnings.txt'), 'w') as f:
        f.write('\n'.join(sp.warnings) + '\n')
    with open(os.path.join(outdir, 'functions.txt'), 'w') as f:
        f.write(''.join(f'{a:#x} {sp.labels.get(a, "")}\n' for a in sp.fstarts))
    with open(os.path.join(outdir, 'labels.txt'), 'w') as f:
        f.write(''.join(f'{a:#x} {n}\n' for a, n in sorted(sp.labels.items())))
    with open(os.path.join(outdir, 'dropped.txt'), 'w') as f:
        f.write(''.join(f'{a:#x} {why}\n' for a, why in sorted(dropped.items())))


if __name__ == '__main__':
    main(sys.argv[1:])
