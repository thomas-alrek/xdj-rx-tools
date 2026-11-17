/*
 * xdjmod core: process gate, module registry, logging, time.
 *
 * libxdjmod.so is LD_PRELOADed into rbp from apl_start, and so is inherited by every
 * process rbp starts (sh, mount, ...). Everything here only activates in rbp itself
 * and only when the binary is the one the mods were written for (checked word by word
 * at each patch site); otherwise the library does nothing.
 *
 * Constructor order: modules register at priority 200 (XM_MODULE); this core runs at
 * 300, after all of them: gate -> settings -> each module's init -> menu install.
 *
 * Credits: the LD_PRELOAD approach for rbp mods and the waveform hook points follow
 * JaNeinHub/XDJ-RX-Mod (https://github.com/JaNeinHub/XDJ-RX-Mod, GPL-3.0), the first
 * RGB / 3-band waveform mod for the XDJ-RX. This is an independent implementation.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#include "xdjmod.h"
#include "internal.h"

#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

static struct xm_module *modules;

void xm_log(const char *fmt, ...)
{
    char buf[256];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);
    fprintf(stderr, "### xdjmod: %s ###\n", buf);
}

/* Modules init (and add their UTILITY rows) in this order; others after them, in link
 * order. */
static const char *const MODULE_ORDER[] = { "fps", "waveform", "scroll", "keys", "beatjump", "wav",
                                            "flac", "folders" };

static int rank(const struct xm_module *m)
{
    int n = (int)(sizeof(MODULE_ORDER) / sizeof(MODULE_ORDER[0]));
    for (int i = 0; i < n; i++)
        if (!strcmp(m->name, MODULE_ORDER[i]))
            return i;
    return n;
}

void xm_register(struct xm_module *m)
{
    struct xm_module **p = &modules;
    while (*p && rank(*p) <= rank(m))
        p = &(*p)->next;
    m->next = *p;
    *p = m;
}

uint32_t xm_now_ms(void)
{
    struct timespec ts;
    /* the raw syscall: glibc 2.13's clock_gettime lives in librt (2.17+ has it in libc) */
    syscall(SYS_clock_gettime, CLOCK_MONOTONIC, &ts);
    return (uint32_t)ts.tv_sec * 1000u + (uint32_t)(ts.tv_nsec / 1000000);
}

/* rbp itself (rbp_solo is a symlink to the same file), not a child process. */
static int is_rbp(void)
{
    char exe[256];
    ssize_t n = readlink("/proc/self/exe", exe, sizeof(exe) - 1);
    if (n <= 0)
        return 0;
    exe[n] = 0;
    const char *base = strrchr(exe, '/');
    return strcmp(base ? base + 1 : exe, "rbp") == 0;
}

__attribute__((constructor(300))) static void xm_core_init(void)
{
    if (!is_rbp())
        return;                                   /* a child process: stay out */
    if (!xm_core_sites_ok()) {
        xm_log("rbp is not firmware 2.21's - mods disabled");
        return;
    }
    xm_cfg_load();
    xm_menu_begin();
    int n = 0;
    for (struct xm_module *m = modules; m; m = m->next, n++)
        m->init();
    xm_menu_install();
    xm_factory_reset_install();
    xm_log("loaded into rbp (pid %d): %d module(s), %d menu row(s)", (int)getpid(), n,
           xm_menu_rows());
}
