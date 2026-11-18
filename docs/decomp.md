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

- About 13,000 functions in 3.3 MB of ARM code (ARM mode only, no Thumb).
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

## Tools

- `tools/decomp/elfinfo.py`: a dependency-free ELF32 reader (sections, symbols, relocations,
  `.dynsym`, the `.ARM.exidx` function index).
- `tools/decomp/match.py TARGET OBJ.o [NAME=ADDR …]`: compares compiled functions with the
  binary, word by word. Relocated words and call offsets are masked.
- `tools/decomp/findsig.py TARGET OBJ.o …`: finds each function of the objects in the binary
  by masked signature. Use it to name library code and to test compiler flags.
- The 2012.01 toolchain is 32-bit x86 Linux. On macOS it runs in Docker
  (`--platform linux/386`, e.g. `i386/debian:bullseye-slim`).
