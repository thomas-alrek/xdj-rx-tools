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
- A second compiler, Freescale's Linaro 2011.07 GCC 4.6.2, is named in `.comment`. It probably
  built the C runtime objects rbp was linked with (rbp has no Thumb code, but the 2012.01
  `libgcc.a` is Thumb-2). The binary isn't downloadable any more; see Status for building it.
- zlib 1.2.3 is the copy bundled in JUCE, compiled with it.

| Part | Approx. functions | Size | Plan |
|---|---|---|---|
| Pioneer's application (plus template code) | ~10,350 | 2.8 MB | decompile |
| JUCE 1.51.16 (commit `27506c2`) | ~1,470 | 238 KB | upstream source |
| zlib 1.2.3 (bundled in JUCE) | 35 | 27 KB | upstream source |
| embedded database engine (likely McObject eXtremeDB) | ~830 | 149 KB | binary blob, or replace |
| iPod / iAP / MFi stack | ~290 | 116 KB | decompile or drop |
| AAC decoder (built with ARM's armcc) | 59 | 23 KB | binary blob, or replace |

## Status

- **Byte-identical rebuild works.** `split.py` → assemble → link reproduces every loaded
  section of `rbp` byte for byte, with about 180,000 symbolic references.
- **Relocation works.** Moving one function to a different address (test: a JUCE function moved
  into the free gap between the two segments) retargets its callers, and the moved copy still
  calls the same functions.
- **C++ functions build into the binary.** 642 JUCE functions are compiled from the upstream
  JUCE source and grafted in place of their assembly, and the rebuilt binary is still
  byte-identical. A graft's external references (other functions, string literals, statics) are
  resolved from the original bytes at the same offsets. Each matching graft reveals the addresses
  of the functions it calls, which names more functions for the next round.
- **Pioneer patched JUCE's atomics.** The JUCE block's atomic operations are inline
  `ldrex`/`strex` loops without memory barriers. Pioneer's application code uses GCC's
  `__sync` builtins, which come with `dmb`. The asm block is reconstructed exactly (a C loop around
  one `ldrex; add; strex` statement with register-pinned outputs). The register allocation around
  it doesn't match yet, so about 90 JUCE functions with atomics (`String`, `StringArray`, …) are
  still assembly. zlib is JUCE's bundled copy and comes in with JUCE.
- The binary's `.comment` also names Freescale's Linaro 2011.07 GCC 4.6.2. It can be built from
  Linaro's source release (only `all-gcc`, with the 2012.01 toolchain's headers). For the code
  tested so far it produces the same output as 2012.01.
- **Pioneer's own code from source.** 376 of Pioneer's functions, from 42 reconstructed source
  files, recompile byte-identically and are built into rbp. They cover the DSP primitives
  (`dsp::LagrangeSRC`, the IIR filters, time-stretch base classes) and much of `mixerengine::`:
  output devices and channels, the input channels (mic, deck, USB), the master channel, the WAV
  recorder, the compressor buffer and the beat/colour effect bases. In the UI layer they cover
  `uif::UiObject`/`UiTimer`, the panel TX path (`ui::PanelComController`), `ui::KeyInput` (the key
  table in [key-codes.md](key-codes.md)) and `ui::JogSpeedGuesser`. In `common::` they cover the
  file input streams (local files, another player's media, and the page-buffered stream that reads
  ahead on a thread), the file-reader base, the sample-rate converter, the message queue and small
  helpers (debug print, thread priority, the i.MX6 timer used as a clock). Another 499 trivial
  virtual functions (empty bodies, constant returns, field getters/setters) are generated from
  rbp's vtables. With JUCE (including five files that need Pioneer's ARM byte-swap patch) that
  makes about 1,540 functions built from source. Pioneer's code needs Linaro 2011.07 GCC (built
  from source with the cross binutils present, so it knows COMDAT groups and mergeable string
  sections) and `-mfpu=neon -mfloat-abi=softfp`. Functions that don't match yet stay as assembly.
  The usual reasons are register allocation, block order, or a source detail not yet recovered.
- **Class map.** RTTI gives 497 classes in Pioneer namespaces (`mixerengine::`, `dsp::`,
  `common::` file readers, `ui::`, `network::`, `playengine::`, `db::` …) with their vtables. 23
  source paths in strings show the source tree layout (`Source/Engine/PlayEngine/…`,
  `Source/Common/reader/…`, `Source/Allinone/Nexus/DB/…`). The large regions without classes are
  C: the database task and mailbox code, GUI glue, and text conversion.
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
  by masked signature (prints address, name, object and size). Use it to name library code and to test compiler flags.
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
- `tools/decomp/graft.py prep|resolve`: grafts compiled functions into a split tree. `prep`
  removes the function's assembly and extracts the function from the compiler's `-S` output into
  the same section slot (unwind directives dropped; `C1`/`D1` aliases followed). `resolve`
  infers the addresses of the graft's undefined references from the original bytes. It also
  checks that they agree, reports MATCH or DIFF per function, and writes the linker script and
  the names it learnt.
- `tools/decomp/rtti.py TARGET FUNCTIONS OUTDIR`: recovers classes from RTTI (typeinfo objects,
  base classes, vtables). Each virtual function is attributed to the most basic class that has it.
- `tools/decomp/cmp.py ORIGINAL BUILT`: compares the relinked binary with the original section by
  section, at the original addresses, and names the nearest label for each difference.
- The 2012.01 toolchain is 32-bit x86 Linux. On macOS it runs in Docker
  (`--platform linux/386`, e.g. `i386/debian:bullseye-slim`).
