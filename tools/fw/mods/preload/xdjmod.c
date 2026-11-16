/*
 * libxdjmod.so - the XDJ-RX mod library, LD_PRELOADed into the stock rbp by apl_start
 * (tools/fw/mods/preload/apply.sh). rbp itself is never modified: features hook the
 * shared-library calls rbp makes (interposed symbols) or patch rbp's code in memory
 * from the constructor (rbp is non-PIE, so its addresses are fixed).
 *
 * Built for the firmware's userland (ARM EABI5 soft-float, glibc 2.13) by build.sh.
 * Research, hook addresses and the plan per feature: docs/firmware-mods.md.
 *
 * Credits: the LD_PRELOAD approach for rbp mods and the waveform hook points follow
 * JaNeinHub/XDJ-RX-Mod (https://github.com/JaNeinHub/XDJ-RX-Mod, GPL-3.0), the first
 * RGB / 3-band waveform mod for the XDJ-RX. This is an independent implementation.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#include <stdio.h>
#include <unistd.h>

__attribute__((constructor)) static void xdjmod_init(void)
{
    /* A console marker so a boot log shows the library is in. */
    fprintf(stderr, "### xdjmod: loaded into pid %d ###\n", (int)getpid());
}
