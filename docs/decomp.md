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
- Built with GCC 4.6.3 (Linaro 2012.01, public binary) and GCC 4.6.2 (Freescale Linaro 2011.07).
  Settings: ARMv7-A, VFPv3 + NEON, soft-float ABI.
- Statically linked libraries:
  - **JUCE 1.51.16**: audio device (ALSA), MIDI, timers, streams
  - **zlib**
  - an **AAC decoder built with ARM's armcc compiler**: about 110 KB of code plus the
    `.constdata` tables. GCC cannot reproduce it byte for byte, so it stays a binary blob or
    gets replaced.

## Tools

- `tools/decomp/elfinfo.py`: a dependency-free ELF32 reader (sections, `.dynsym`, the
  `.ARM.exidx` function index).
