/*
 * keys mod: keys shown in Camelot notation (UTILITY > KEY DISPLAY: CLASSIC (default) /
 * CAMELOT), e.g. "8A" instead of "Am".
 *
 * rbp's key index (1..24) is already in Camelot order: n -> number (n + 1) / 2, letter A for
 * odd n, B for even (1 = 1A = Abm ... 24 = 12B = E). Keys are shown from four places:
 *   - the index -> name table 0x462dd8 (24 UTF-16 pointers, .data): the track-info panel and
 *     the harmonic "matching keys" labels. CAMELOT points it at our strings, CLASSIC restores
 *     rbp's (one 32-bit store per entry);
 *   - the library's own key name, fetched by 0x139c4c(category, id, u16 **out, flag) (category
 *     10 = djdbKey): browser KEY lists and key columns. Detoured: with CAMELOT, the name is
 *     parsed with rbp's own parser (0x105e84 -> 0x106e68, which accepts every spelling rbp
 *     knows, Camelot included) and replaced by the Camelot string (rbp's pool free 0x1158f8
 *     ignores pointers outside the pool, so returning a static string is safe). Its 16-entry
 *     name cache (0x1080f2c, stride 0x110) is cleared when the setting changes;
 *   - every other read of a library key name - track lists' KEY column, breadcrumbs, the sort
 *     builder - goes through rbp's database field read 0x31a538(row, 1) on a row of the djdbKey
 *     table. Cursors opened on djdbKey (0x319b8c / 0x319e1c) and the rows they fetch (0x31a4a0)
 *     are tagged; with CAMELOT a name read from such a row comes back as the Camelot string
 *     ("8A"), which rbp's parser turns back into the same key index (harmonic matching);
 *   - the deck panel's key is an image, not text (imagedata.dat 0x28e-0x2df, key_img_remap);
 *   - sorting by KEY: the browser's list builder 0x1134bc (mode & 0x3f = 12) sorts the rows by
 *     the key name it reads; while it runs (CAMELOT) names come back zero-padded ("08A"), so the
 *     list reads 1A, 1B, 2A ... 12B (the KEY column itself is drawn from 0x139c4c: "8A"). The
 *     sort-table builder 0x1128c8 writes djdbTrackSort rows through 0x319488(table, 7, values)
 *     {&track id, title, key name, &key id, ...}: there the row gets the padded name and the key
 *     index (1..24, Camelot order) as its number.
 *   - a name read with rbp's "needs free" flag clear counts as no name for rbp's callers, so the
 *     flag is left as the read set it (rbp's pool free ignores pointers outside its pool).
 * Nothing is written to the stick.
 *
 * Addresses: docs/firmware-mods.md section 10c.
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#include "xdjmod.h"

#include <pthread.h>
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
#define DB_OPEN        0x319b8cu          /* cursor (int *err, table, index, ... 9 args) */
#define DB_OPEN_W0     0xe92d4ff0u
#define DB_OPEN_W1     0xe24dd04cu
#define DB_OPEN2       0x319e1cu          /* cursor (int *err, table, ... 8 args): whole table */
#define DB_OPEN2_W0    0xe92d4ff0u
#define DB_OPEN2_W1    0xe24dd04cu
#define DB_FETCH       0x31a4a0u          /* row (cursor, int *err) */
#define DB_FETCH_W0    0xe92d41f0u
#define DB_FETCH_W1    0xe24dd020u
#define DB_FIELD       0x31a538u          /* void *(row, field, char *needfree) */
#define DB_FIELD_W0    0xe92d40f8u
#define DB_FIELD_W1    0xe1a06002u
#define DB_CLOSE       0x31a24cu          /* int (cursor) */
#define DB_CLOSE_W0    0xe92d40f0u
#define DB_CLOSE_W1    0xe24dd01cu
#define SORT_BUILD     0x1128c8u          /* int (a0, mode, a2, short *) - mode & 0x7f == 12: KEY */
#define SORT_BUILD_W0  0xe92d4ff0u
#define SORT_BUILD_W1  0xe2535000u
#define SORT_KEY       12

enum { CLASSIC, CAMELOT };
static volatile int mode = CLASSIC;
static uint16_t cam[24][4];               /* "1A".."12B", UTF-16, NUL-terminated */
static uint16_t cam_sort[24][4];          /* "01A".."12B": the same, sortable as strings */
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

/* ---- library key names at the database: cursors / rows of djdbKey ---- */
#define NTAG 16
static void *volatile key_cursor[NTAG];
static struct { void *volatile row, *volatile cursor; } key_row[NTAG];
static volatile unsigned key_row_next;
static volatile pthread_t sort_thread;
static volatile int sort_active;

typedef void *(*db_open_fn)(int *, const char *, const char *, int, int, int, void *, int, void *);
typedef void *(*db_fetch_fn)(void *, int *);
typedef void *(*db_field_fn)(void *, int, char *);
typedef int (*db_close_fn)(void *);
typedef int (*sort_build_fn)(int, unsigned, int, short *);
static db_open_fn db_open_orig;
typedef void *(*db_open2_fn)(int *, const char *, int, int, int, int, int, int);
static db_open2_fn db_open2_orig;
static db_fetch_fn db_fetch_orig;
static db_field_fn db_field_orig;
static db_close_fn db_close_orig;
static sort_build_fn sort_build_orig;

static void tag_cursor(void *c, const char *table)
{
    if (c && table && !strcmp(table, "djdbKey"))
        for (int i = 0; i < NTAG; i++)
            if (!key_cursor[i] && __sync_bool_compare_and_swap(&key_cursor[i], 0, c))
                break;
}

static void *db_open_hook(int *err, const char *table, const char *index, int a3, int a4, int a5,
                          void *a6, int a7, void *a8)
{
    void *c = db_open_orig(err, table, index, a3, a4, a5, a6, a7, a8);
    tag_cursor(c, table);
    return c;
}

static void *db_open2_hook(int *err, const char *table, int a2, int a3, int a4, int a5, int a6, int a7)
{
    void *c = db_open2_orig(err, table, a2, a3, a4, a5, a6, a7);
    tag_cursor(c, table);
    return c;
}

static int is_key_cursor(void *c)
{
    for (int i = 0; i < NTAG; i++)
        if (key_cursor[i] == c)
            return 1;
    return 0;
}

static void *db_fetch_hook(void *cursor, int *err)
{
    void *row = db_fetch_orig(cursor, err);
    if (row && cursor && is_key_cursor(cursor)) {
        unsigned k = __sync_fetch_and_add(&key_row_next, 1) % NTAG;
        key_row[k].cursor = cursor;
        key_row[k].row = row;
    }
    return row;
}

static int db_close_hook(void *cursor)
{
    if (cursor) {
        for (int i = 0; i < NTAG; i++)
            if (key_cursor[i] == cursor)
                key_cursor[i] = 0;
        for (int i = 0; i < NTAG; i++)
            if (key_row[i].cursor == cursor)
                key_row[i].row = key_row[i].cursor = 0;
    }
    return db_close_orig(cursor);
}

/* rbp's key index (1..24) for a UTF-16 key name, or 0 */
static int name_index(const uint16_t *name)
{
    uint8_t type = 0;
    char *a = ((char *(*)(const uint16_t *, uint8_t *))KEY_TO_ASCII)(name, &type);
    if (!a)
        return 0;
    int n = ((int (*)(const char *, int))KEY_INDEX)(a, type);
    ((void (*)(void *))POOL_FREE)(a);
    return n >= 1 && n <= 24 ? n : 0;
}

static void *db_field_hook(void *row, int field, char *needfree)
{
    void *v = db_field_orig(row, field, needfree);
    if (mode != CAMELOT || field != 1 || !v || !row)
        return v;
    int tagged = 0;
    for (int i = 0; i < NTAG && !tagged; i++)
        tagged = key_row[i].row == row;
    if (!tagged)
        return v;
    int n = name_index((const uint16_t *)v);
    if (!n)
        return v;
    /* "needs free" stays as rbp's read set it: callers take a name with the flag clear as no
     * name at all (the list sorter 0x1134bc does). rbp's pool free ignores our static strings. */
    if (needfree && *needfree == 1)
        ((void (*)(void *))POOL_FREE)(v);
    int sorting = sort_active && pthread_equal(pthread_self(), sort_thread);
    return sorting ? cam_sort[n - 1] : cam[n - 1];
}

/* rows of rbp's sort table while a KEY sort runs: the Camelot order */
#define DB_INSERT      0x319488u          /* int (table, nfields, void **values) */
#define DB_INSERT_W0   0xe92d47f0u        /* push {r4-sl, lr} */
#define DB_INSERT_W1   0xe24dd028u        /* sub sp, sp, #0x28 */
typedef int (*db_insert_fn)(const char *, int, void **);
static db_insert_fn db_insert_orig;
static int sort_rank;                     /* the row's number while it is inserted (one sort at a time) */

static int db_insert_hook(const char *table, int nf, void **v)
{
    if (sort_active && nf == 7 && v && v[2] && v[3] && pthread_equal(pthread_self(), sort_thread) &&
        table && !strcmp(table, "djdbTrackSort")) {
        int n = 0;                         /* the name is one of ours (db_field_hook) */
        for (int i = 0; i < 24 && !n; i++)
            if (v[2] == cam_sort[i] || v[2] == cam[i])
                n = i + 1;
        if (n) {
            void *name = v[2], *num = v[3];
            sort_rank = n;
            v[2] = cam_sort[n - 1];
            v[3] = &sort_rank;
            int r = db_insert_orig(table, nf, v);
            v[2] = name;
            v[3] = num;
            return r;
        }
    }
    return db_insert_orig(table, nf, v);
}

/* the browser's list builder (sorted lists): mode & 0x3f = 12 is KEY; it sorts the rows by the
 * key name it reads, so names come back zero-padded meanwhile ("08A": 1A, 1B, 2A ... 12B) */
#define LIST_BUILD     0x1134bcu          /* int (a0, mode, cursor, int *filter) */
#define LIST_BUILD_W0  0xe92d4ff0u        /* push {r4-fp, lr} */
#define LIST_BUILD_W1  0xe24dd09cu        /* sub sp, sp, #0x9c */
typedef int (*list_build_fn)(int, unsigned, int, int);
static list_build_fn list_build_orig;

static int list_build_hook(int a0, unsigned m, int a2, int a3)
{
    int key = mode == CAMELOT && (m & 0x3f) == SORT_KEY;
    if (key) {
        sort_thread = pthread_self();
        sort_active = 1;
    }
    int r = list_build_orig(a0, m, a2, a3);
    if (key)
        sort_active = 0;
    return r;
}

static int sort_build_hook(int a0, unsigned m, int a2, short *a3)
{
    int key = mode == CAMELOT && (m & 0x7f) == SORT_KEY;
    if (key) {
        sort_thread = pthread_self();
        sort_active = 1;
    }
    int r = sort_build_orig(a0, m, a2, a3);
    if (key)
        sort_active = 0;
    return r;
}



/* ---- the deck panel's key: an image ----
 * The deck panel (left of the waveforms) shows the key as an imagedata.dat picture (72 x 24),
 * not as text. rbp's images 0x28e-0x2df hold every key name, one group per Camelot key in
 * Camelot order: "1A", "01A", then the classic spelling(s) ("Abm", "G#m"), "2A", "02A", ...
 * With CAMELOT, an image lookup of a classic spelling is answered with its group's first
 * image ("Am" 0x2a8 -> "8A" 0x2a6). */
#define IMG_LOOKUP     0x1694bcu          /* record *(id) */
#define IMG_LOOKUP_W0  0xe30033f5u        /* movw r3, #0x3f5 */
#define IMG_LOOKUP_W1  0xe1500003u        /* cmp r0, r3 */
#define KEY_IMG_END    0x2e0u
static const uint16_t KEY_IMG[24] = {     /* first image of each group: 1A, 2A ... 12A, 1B ... 12B */
    0x28e, 0x292, 0x296, 0x29a, 0x29d, 0x2a0, 0x2a3, 0x2a6, 0x2a9, 0x2ac, 0x2af, 0x2b3,
    0x2b7, 0x2ba, 0x2be, 0x2c2, 0x2c6, 0x2ca, 0x2ce, 0x2d1, 0x2d4, 0x2d7, 0x2da, 0x2dd };

static unsigned key_img_remap(unsigned id)
{
    if (mode != CAMELOT || id < KEY_IMG[0] || id >= KEY_IMG_END)
        return id;
    int g = 23;
    while (id < KEY_IMG[g])
        g--;
    return id >= KEY_IMG[g] + 2u ? KEY_IMG[g] : id;   /* classic spelling -> "nA" / "nB" */
}

typedef void *(*img_lookup_fn)(unsigned);
static img_lookup_fn img_lookup_orig;

static void *img_lookup_hook(unsigned id)   /* used when no other mod detours the lookup */
{
    return img_lookup_orig(key_img_remap(id));
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
        cam_sort[i][0] = (uint16_t)('0' + n / 10);
        cam_sort[i][1] = (uint16_t)('0' + n % 10);
        cam_sort[i][2] = (uint16_t)(i % 2 ? 'B' : 'A');
        cam_sort[i][3] = 0;
    }
    db_name_orig = (db_name_fn)xm_detour(DB_NAME, DB_NAME_W0, DB_NAME_W1, (void *)db_name_hook);
    if (!db_name_orig)
        xm_log("keys: library key names stay classic");
    db_open_orig = (db_open_fn)xm_detour(DB_OPEN, DB_OPEN_W0, DB_OPEN_W1, (void *)db_open_hook);
    db_open2_orig = (db_open2_fn)xm_detour(DB_OPEN2, DB_OPEN2_W0, DB_OPEN2_W1, (void *)db_open2_hook);
    db_fetch_orig = (db_fetch_fn)xm_detour(DB_FETCH, DB_FETCH_W0, DB_FETCH_W1, (void *)db_fetch_hook);
    db_close_orig = (db_close_fn)xm_detour(DB_CLOSE, DB_CLOSE_W0, DB_CLOSE_W1, (void *)db_close_hook);
    db_field_orig = (db_field_fn)xm_detour(DB_FIELD, DB_FIELD_W0, DB_FIELD_W1, (void *)db_field_hook);
    list_build_orig = (list_build_fn)xm_detour(LIST_BUILD, LIST_BUILD_W0, LIST_BUILD_W1, (void *)list_build_hook);
    db_insert_orig = (db_insert_fn)xm_detour(DB_INSERT, DB_INSERT_W0, DB_INSERT_W1, (void *)db_insert_hook);
    if (db_insert_orig)
        sort_build_orig = (sort_build_fn)xm_detour(SORT_BUILD, SORT_BUILD_W0, SORT_BUILD_W1, (void *)sort_build_hook);
    if (!db_open_orig || !db_open2_orig || !db_fetch_orig || !db_close_orig || !db_field_orig || !sort_build_orig ||
        !list_build_orig)
        xm_log("keys: database hooks incomplete (%d%d%d%d%d%d) - column / sort keys may stay classic",
               !!db_open_orig, !!db_open2_orig, !!db_fetch_orig, !!db_close_orig, !!db_field_orig,
               !!sort_build_orig);
    /* the deck panel's key image: our own lookup detour, or the one another mod installed */
    if (xm_read32(IMG_LOOKUP) == IMG_LOOKUP_W0)
        img_lookup_orig = (img_lookup_fn)xm_detour(IMG_LOOKUP, IMG_LOOKUP_W0, IMG_LOOKUP_W1, (void *)img_lookup_hook);
    xm_img_remap = key_img_remap;
    xm_menu_add("KEY DISPLAY", MODES, 2, "keydisplay", CLASSIC, set_mode);
    xm_log("keys: KEY DISPLAY row (classic / Camelot)");
}
