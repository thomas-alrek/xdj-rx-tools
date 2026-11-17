/*
 * folders mod: the browser's [FOLDER] view lists the Contents folder of a rekordbox stick.
 *
 * rbp's folder lister (0x1f1ed0, readdir-based: every filesystem goes through it) skips two
 * root folders by name: PIONEER (always, on FAT32/exFAT; HFS+ uses .PIONEER, a dot name) and
 * Contents - the latter only when the stick has a rekordbox library (the per-slot flag
 * 0x2954d40[slot], set when PIONEER/rekordbox/export.pdb opens at mount). The Contents test
 * starts with that flag: 0x1f2498 bne 0x1f2154 (flag != 1: list the entry). Making the
 * branch unconditional lists Contents always; PIONEER (rekordbox's own files) stays hidden.
 * Tracks under Contents load as plain files (no library cues or grid).
 *
 * Addresses: docs/firmware-mods.md section 7a.
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#include "xdjmod.h"

#define CONTENTS_TEST    0x1f2498u
#define BNE_LIST         0x1affff2du      /* bne 0x1f2154 */
#define B_LIST           0xeaffff2du      /* b   0x1f2154 */

XM_MODULE(folders)
{
    if (xm_patch32(CONTENTS_TEST, BNE_LIST, B_LIST))
        xm_log("folders: [FOLDER] lists Contents");
}
