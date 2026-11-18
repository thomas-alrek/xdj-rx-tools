# rbp decompilation

An effort to turn `rbp`, the XDJ-RX main application, back into C/C++ source, so that features
can be developed in source instead of as binary patches.

## Where things live

| Path | Contents | Tracked |
|---|---|---|
| `tools/decomp/` | our own tooling (ELF reader, function inventory, later the splitter and build harness) | yes |
| `decomp/` | everything derived from `rbp`: notes, split assembly, reconstructed source, toolchains | **no**: in `.gitignore` from the first byte; a local git repo of its own with no remote |

Reconstructed source is a derivative of Pioneer's code. It is never committed to this repo,
pushed or published. Releases keep shipping the stock image plus our add-on library.

## Approach

1. **Exact rebuild first.** Split `rbp` into per-function assembly and data. Every address is
   symbolised (literal pools, vtables, jump tables). A linker script puts each piece back at
   its original address, and the build must reproduce the original binary byte for byte.
2. **Replace assembly with C++ function by function.** Compile each function with the original
   compiler and diff it against the original bytes. A match proves the function correct. The
   emulator stays the end-to-end test.
3. **Functionally equivalent where matching is impractical.** Some C++ (templates, inlining,
   exception tables) won't match. That code is accepted as equivalent, tested against stock in
   the emulator.
4. **Libraries come from upstream, not from decompiling.** JUCE, zlib and the compiler runtime.

## What rbp is made of

- About 14,100 functions in 3.3 MB of ARM code (ARM mode only, no Thumb), plus 435 PLT stubs.
- **Compiler and flags, confirmed by byte-identical matches:** Linaro GCC 2012.01 (4.6.3, the
  public binary `gcc-linaro-arm-linux-gnueabi-2012.01-20120125`) with
  `-O2 -mcpu=cortex-a8 -marm`. A JUCE function (368 bytes) recompiles to the identical bytes.
  A scan of 115 JUCE source files finds about 700 of their functions in rbp at `-O2`, against
  500 at `-O3`.
- A second compiler, Freescale's Linaro 2011.07 GCC 4.6.2, appears to have built zlib and
  supplied the runtime libraries rbp was linked with. It is no longer downloadable.

| Part | Approx. functions | Size | Plan |
|---|---|---|---|
| Pioneer's application (plus template code) | ~10,350 | 2.8 MB | decompile |
| JUCE 1.51.16 (commit `27506c2`) | ~1,470 | 238 KB | upstream source |
| zlib 1.2.3 | 35 | 27 KB | upstream source |
| embedded database engine (likely McObject eXtremeDB) | ~830 | 149 KB | binary blob, or replace |
| iPod / iAP / MFi stack | ~290 | 116 KB | decompile or drop |
| AAC decoder (built with ARM's armcc) | 59 | 23 KB | binary blob, or replace |

## Status

- **Byte-identical rebuild works.** `split.py` → assemble → link reproduces every loaded
  section of `rbp` byte for byte, with about 180,000 symbolic references.
- **Relocation works.** Moving one function to a different address (test: a JUCE function moved
  into the free gap between the two segments) retargets its callers, and the moved copy still
  calls the same functions.
- Not yet symbolic: the unwind tables (`.ARM.exidx` / `.ARM.extab`) and `.got`. Moved code has no
  unwind entries until those are generated too.
- `bl` reaches ±32 MB. The 38 MB `.bss` sits between the code and any address beyond it, so code
  placed past `.bss` gets linker veneers. The free gap before the data segment
  (0x458e2c–0x461000, 33 KB) is in range.

## Tools

- `tools/decomp/elfinfo.py`: a dependency-free ELF32 reader (sections, symbols, relocations,
  `.dynsym`, the `.ARM.exidx` function index).
- `tools/decomp/match.py TARGET OBJ.o [NAME=ADDR …]`: compares compiled functions with the
  binary, word by word. Relocated words and call offsets are masked.
- `tools/decomp/findsig.py TARGET OBJ.o …`: finds each function of the objects in the binary
  by masked signature. Use it to name library code and to test compiler flags.
- `tools/decomp/split.py TARGET FSTARTS OUTDIR [--names FILE]`: the splitter. It writes GNU as
  sources (one file per data section, 64 KB chunks of whole functions for `.text`) and a linker
  script that puts every section back at its original address. Code is emitted word by word as
  `.inst` with a disassembly comment. Calls and branches to other functions, literal-pool words
  and switch-table entries that hold addresses, and pointers in the data sections become
  labels. Every target gets its own global label (`f_`, `c_`, `d_`, `b_`, `p_<import>`, or a name
  from `--names`). The dynamic-linking tables, unwind tables and `.got` stay raw (`.incbin` from
  the original file). It also refines the function list:
  - It drops candidates that sit inside a literal pool or between an instruction and its literal.
  - It drops candidates evidenced only by a data pointer that don't follow a function end.
  - It adds `push {…, lr}` prologues that follow a function end.
- `tools/decomp/cmp.py ORIGINAL BUILT`: compares the relinked binary with the original section by
  section, at the original addresses, and names the nearest label for each difference.
- The 2012.01 toolchain is 32-bit x86 Linux. On macOS it runs in Docker
  (`--platform linux/386`, e.g. `i386/debian:bullseye-slim`).
