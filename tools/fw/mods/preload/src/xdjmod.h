/*
 * xdjmod - the framework every rbp feature mod is built on (libxdjmod.so).
 *
 * The library is LD_PRELOADed into the stock rbp (firmware 2.21). A feature is one
 * source file that declares a module; the core runs it only inside a verified rbp:
 *
 *   #include "xdjmod.h"
 *   static const char *const RATES[] = { "20 (STOCK)", "30", "60" };
 *   static void set_rate(int idx) { ... patch rbp for this choice ... }
 *   XM_MODULE(fps) {
 *       if (!xm_expect32(0xb3aac, 0xe3540031)) return;     // wrong firmware: stay off
 *       xm_menu_add("UI FRAME RATE", RATES, 3, "fps", 0, set_rate);
 *   }
 *
 * - Modules: XM_MODULE(name) { init } - init runs once at startup, after the core
 *   checked that this is rbp 2.21. Check your own patch sites with xm_expect32().
 * - Patching: xm_patch32() rewrites one aligned word (code or literal) and flushes the
 *   cache; xm_detour() sends a function to a C hook and returns a stub that runs the
 *   original. Every write is checked against the bytes it expects.
 * - Settings: xm_cfg_get/xm_cfg_set - integers by key in /root/rbp/xdjmod.conf.
 * - UTILITY menu: xm_menu_add() adds a native row (before VERSION No.); the core
 *   renders it, saves the choice under `key`, and calls apply(choice) at startup, on
 *   every change and after a factory reset (which restores each row's default).
 *
 * Research and addresses: docs/firmware-mods.md.
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#ifndef XDJMOD_H
#define XDJMOD_H

#include <stdint.h>

/* ---- logging: one console line, "### xdjmod: ..." ---- */
void xm_log(const char *fmt, ...) __attribute__((format(printf, 1, 2)));

/* ---- modules ---- */
struct xm_module {
    const char *name;
    void (*init)(void);
    struct xm_module *next;
};
void xm_register(struct xm_module *m);
#define XM_MODULE(n)                                                            \
    static void xm_init_##n(void);                                              \
    static struct xm_module xm_mod_##n = { #n, xm_init_##n, 0 };                \
    __attribute__((constructor(200))) static void xm_reg_##n(void)              \
    { xm_register(&xm_mod_##n); }                                               \
    static void xm_init_##n(void)

/* ---- patching (rbp is non-PIE: fixed addresses) ---- */
uint32_t xm_read32(uintptr_t addr);
/* 1 if the word at addr is `expect`; logs the mismatch otherwise */
int xm_expect32(uintptr_t addr, uint32_t expect);
/* write `val` where `expect` is (or `val` already is); 1 on success */
int xm_patch32(uintptr_t addr, uint32_t expect, uint32_t val);
/*
 * Detour the ARM function at `addr` to `hook` (same calling convention). The first
 * two instructions are moved into a stub, so they must not be PC-relative; they must
 * equal expect0/expect1. Returns the stub (call it to run the original), or 0.
 */
void *xm_detour(uintptr_t addr, uint32_t expect0, uint32_t expect1, void *hook);

/* ---- settings: /root/rbp/xdjmod.conf (key=integer per line) ---- */
int  xm_cfg_get(const char *key, int def);
void xm_cfg_set(const char *key, int val);

/* ---- UTILITY menu ---- */
typedef void (*xm_apply_fn)(int choice);
/*
 * Add a UTILITY row: `label` and `values` are ASCII (kept short: ~18 / ~15 chars),
 * the choice is saved under `key`, `def` is the default (and the factory-reset value).
 * apply(choice) is called at startup with the saved choice and on every change, on the
 * display thread - keep it short. Returns 1 if added.
 */
int xm_menu_add(const char *label, const char *const *values, int nvalues,
                const char *key, int def, xm_apply_fn apply);

/* ---- time ---- */
uint32_t xm_now_ms(void);   /* CLOCK_MONOTONIC in milliseconds */

#endif
