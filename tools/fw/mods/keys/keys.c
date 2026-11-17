/*
 * keys mod: keys shown in Camelot notation (UTILITY > KEY DISPLAY: CLASSIC (default) /
 * CAMELOT), e.g. "8A" instead of "Am".
 *
 * rbp's key index (1..24) is already in Camelot order: n -> number (n + 1) / 2, letter A for
 * odd n, B for even (1 = 1A = Abm ... 24 = 12B = E). Displayed key text comes from two places:
 *   - the index -> name table 0x462dd8 (24 UTF-16 pointers, .data): the track-info panel and
 *     the harmonic "matching keys" labels. CAMELOT points it at our strings, CLASSIC restores
 *     rbp's (one 32-bit store per entry);
 *   - the library's own key name, fetched by 0x139c4c(category, id, u16 **out, flag) (category
 *     10 = djdbKey): browser KEY lists and key columns. Detoured: with CAMELOT, the name is
 *     parsed with rbp's own parser (0x105e84 -> 0x106e68, which accepts every spelling rbp
 *     knows) and replaced by the Camelot string (rbp's pool free 0x1158f8 ignores pointers
 *     outside the pool, so returning a static string is safe). Its 16-entry name cache
 *     (0x1080f2c, stride 0x110) is cleared when the setting changes.
 * Searching, harmonic matching, sorting tables and saving use the names directly and stay
 * classic; nothing is written to the stick.
 *
 * Addresses: docs/firmware-mods.md section 10c.
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#include "xdjmod.h"

#include <stdint.h>
#include <string.h>

#define KEY_TABLE      0x462dd8u          /* const u16 *[24] */
#define KEY_TABLE_0    0x003a1f70u        /* "Abm" */
#define KEY_TABLE_1    0x003a1f68u        /* "B" */
#define DB_NAME        0x139c4cu          /* int (cat, id, u16 **out, flag) */
#define DB_NAME_W0     0xe92d4ff0u        /* push {r4-fp, lr} */
#define DB_NAME_W1     0xe24dd054u        /* sub sp, sp, #0x54 */
#define CAT_KEY        10
#define KEY_TO_ASCII   0x105e84u          /* char *(const u16 *, uint8_t *type) */
#define KEY_INDEX      0x106e68u          /* int (const char *, int type): 1..24 */
#define POOL_FREE      0x1158f8u
#define NAME_CACHE     0x1080f2cu         /* 16 x 0x110: +0 category, +4 id */

enum { CLASSIC, CAMELOT };
static volatile int mode = CLASSIC;
static uint16_t cam[24][4];               /* "1A".."12B", UTF-16, NUL-terminated */
static const uint16_t *classic[24];

typedef int (*db_name_fn)(int cat, int id, uint16_t **out, int flag);
static db_name_fn db_name_orig;

static int db_name_hook(int cat, int id, uint16_t **out, int flag)
{
    int r = db_name_orig(cat, id, out, flag);
    if (mode != CAMELOT || cat != CAT_KEY || r <= 1 || !out || !*out)
        return r;
    uint8_t type = 0;
    char *a = ((char *(*)(const uint16_t *, uint8_t *))KEY_TO_ASCII)(*out, &type);
    if (!a)
        return r;
    int n = ((int (*)(const char *, int))KEY_INDEX)(a, type);
    ((void (*)(void *))POOL_FREE)(a);
    if (n < 1 || n > 24)
        return r;
    ((void (*)(void *))POOL_FREE)(*out);
    *out = cam[n - 1];
    int len = 0;
    while (cam[n - 1][len])
        len++;
    return len + 1;
}

static void set_mode(int choice)
{
    mode = choice;
    const uint16_t **tbl = (const uint16_t **)KEY_TABLE;
    for (int i = 0; i < 24; i++)
        tbl[i] = choice == CAMELOT ? cam[i] : classic[i];
    for (int i = 0; i < 16; i++) {          /* drop cached key names */
        uint32_t *e = (uint32_t *)(NAME_CACHE + 0x110u * i);
        if (e[0] == CAT_KEY)
            e[0] = e[1] = 0;
    }
}

static const char *const MODES[] = { "CLASSIC", "CAMELOT" };

XM_MODULE(keys)
{
    if (!xm_expect32(KEY_TABLE, KEY_TABLE_0) || !xm_expect32(KEY_TABLE + 4, KEY_TABLE_1))
        return;
    for (int i = 0; i < 24; i++) {
        classic[i] = ((const uint16_t **)KEY_TABLE)[i];
        int n = i / 2 + 1, k = 0;
        if (n >= 10)
            cam[i][k++] = (uint16_t)('0' + n / 10);
        cam[i][k++] = (uint16_t)('0' + n % 10);
        cam[i][k++] = (uint16_t)(i % 2 ? 'B' : 'A');
        cam[i][k] = 0;
    }
    db_name_orig = (db_name_fn)xm_detour(DB_NAME, DB_NAME_W0, DB_NAME_W1, (void *)db_name_hook);
    if (!db_name_orig)
        xm_log("keys: library key names stay classic");
    xm_menu_add("KEY DISPLAY", MODES, 2, "keydisplay", CLASSIC, set_mode);
    xm_log("keys: KEY DISPLAY row (classic / Camelot)");
}
