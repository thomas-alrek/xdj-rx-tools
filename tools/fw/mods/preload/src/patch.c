/*
 * xdjmod patching: checked word writes and function detours on rbp's code.
 * rbp is non-PIE (fixed addresses); its text is mapped R-X, so each write briefly
 * makes the page writable, then flushes the caches.
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#include "xdjmod.h"

#include <sys/mman.h>
#include <unistd.h>

#define ARM_LDR_PC_PC_M4 0xe51ff004u     /* ldr pc, [pc, #-4] : jump to the next word */

static uintptr_t page_of(uintptr_t a)
{
    return a & ~((uintptr_t)sysconf(_SC_PAGESIZE) - 1);
}

uint32_t xm_read32(uintptr_t addr)
{
    return *(volatile const uint32_t *)addr;
}

int xm_expect32(uintptr_t addr, uint32_t expect)
{
    uint32_t v = xm_read32(addr);
    if (v == expect)
        return 1;
    xm_log("0x%08lx holds 0x%08x, expected 0x%08x", (unsigned long)addr, v, expect);
    return 0;
}

/* Write `n` words at addr (same page or two). */
static int write_words(uintptr_t addr, const uint32_t *w, int n)
{
    uintptr_t p0 = page_of(addr), p1 = page_of(addr + 4u * n - 1);
    size_t len = p1 - p0 + (size_t)sysconf(_SC_PAGESIZE);
    if (mprotect((void *)p0, len, PROT_READ | PROT_WRITE | PROT_EXEC) != 0) {
        xm_log("mprotect 0x%08lx failed", (unsigned long)addr);
        return 0;
    }
    for (int i = 0; i < n; i++)
        ((volatile uint32_t *)addr)[i] = w[i];
    __builtin___clear_cache((char *)addr, (char *)addr + 4 * n);
    mprotect((void *)p0, len, PROT_READ | PROT_EXEC);
    return 1;
}

int xm_patch32(uintptr_t addr, uint32_t expect, uint32_t val)
{
    uint32_t v = xm_read32(addr);
    if (v == val)
        return 1;
    if (v != expect) {
        xm_log("not patching 0x%08lx: holds 0x%08x, expected 0x%08x", (unsigned long)addr, v,
               expect);
        return 0;
    }
    return write_words(addr, &val, 1);
}

/* Executable memory for detour stubs (one page, handed out in 32-byte slots). */
#define STUB_WORDS 8
static uint32_t *stub_alloc(void)
{
    static uint32_t *page;
    static int used;
    const int slots = 4096 / (STUB_WORDS * 4);
    if (!page || used == slots) {
        void *p = mmap(0, 4096, PROT_READ | PROT_WRITE | PROT_EXEC, MAP_PRIVATE | MAP_ANONYMOUS,
                       -1, 0);
        if (p == MAP_FAILED)
            return 0;
        page = p;
        used = 0;
    }
    return page + STUB_WORDS * used++;
}

/* `ldr rt, [pc, #+-imm12]` (any condition, rt != pc) */
static int is_ldr_pc_literal(uint32_t w)
{
    return (w & 0x0f7f0000u) == 0x051f0000u && ((w >> 12) & 15) != 15;
}

/*
 * The instruction moved from `from` into stub word `slot`: copied as is, except a
 * PC-relative literal load, which is rewritten to load the same value from a literal
 * kept in the stub (word 4 + slot).
 */
static uint32_t relocate(uintptr_t from, uint32_t w, uint32_t *stub, int slot)
{
    if (!is_ldr_pc_literal(w))
        return w;
    uint32_t imm = w & 0xfff;
    uintptr_t lit = (w & (1u << 23)) ? from + 8 + imm : from + 8 - imm;
    stub[4 + slot] = xm_read32(lit);
    /* ldr rt, [pc, #8]: from stub word `slot`, pc = slot*4 + 8, literal at (4+slot)*4 */
    return (w & 0xf000f000u) | 0x059f0000u | 8u;
}

void *xm_detour(uintptr_t addr, uint32_t expect0, uint32_t expect1, void *hook)
{
    if (!xm_expect32(addr, expect0) || !xm_expect32(addr + 4, expect1))
        return 0;
    if ((expect0 & 0x0e000000u) == 0x0a000000u || (expect1 & 0x0e000000u) == 0x0a000000u) {
        xm_log("detour 0x%08lx: a branch can't move into the stub", (unsigned long)addr);
        return 0;                                  /* b/bl are PC-relative */
    }
    uint32_t *stub = stub_alloc();
    if (!stub)
        return 0;
    /* stub: the two displaced instructions (literal loads relocated), then jump back to
     * addr+8; words 4/5 hold relocated literals */
    stub[0] = relocate(addr, expect0, stub, 0);
    stub[1] = relocate(addr + 4, expect1, stub, 1);
    stub[2] = ARM_LDR_PC_PC_M4;
    stub[3] = (uint32_t)(addr + 8);
    __builtin___clear_cache((char *)stub, (char *)(stub + STUB_WORDS));
    /* entry: jump to the hook (ldr pc interworks, so a Thumb hook works too) */
    uint32_t entry[2] = { ARM_LDR_PC_PC_M4, (uint32_t)(uintptr_t)hook };
    if (!write_words(addr, entry, 2))
        return 0;
    return stub;
}
