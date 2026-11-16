/*
 * xdjmod UTILITY menu: native rows for the mods, and the factory-reset hook.
 *
 * rbp 2.21 drives UTILITY (hold MENU) from one static table: a u32 count at 0x4621a4
 * followed by 0x34-byte items (label, value strings, count, current/edit index, enabled,
 * 7 callbacks). Table order is screen order; the screen code reaches the table only
 * through 7 literal words. We copy the table with our rows inserted before VERSION No.
 * (the last row), add a zeroed sentinel (some bound checks read index == count) and
 * repoint the 7 words. Labels and values are UTF-16 text drawn with rbp's font.
 *
 * Factory reset (DELETE + TEMPO RANGE at power-on) is decided by rbp's first-frame
 * handler 0x282630; we detour it, reset our settings to the defaults on that key
 * combination, then run it as usual.
 *
 * Addresses: docs/firmware-mods.md section 4.
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#include "xdjmod.h"
#include "internal.h"

#include <stdlib.h>
#include <string.h>

/* rbp 2.21 */
#define TBL_COUNT      0x4621a4u
#define TBL_ITEMS      0x4621a8u
#define STOCK_ITEMS    16
static const uintptr_t COUNT_REFS[] = { 0xe9014, 0xe91c0, 0xe92fc, 0xe949c, 0xe9500, 0xe9c5c };
#define ITEMS_REF      0xe9c54u
/* generic item callbacks */
#define CB_NOP_INIT    0xe7f3cu   /* init: nothing                                  */
#define CB_BEGIN_EDIT  0xe7f40u   /* beginEdit: edit = current                      */
#define CB_CHANGE      0xe7f68u   /* change(idx): bounds-checked edit = idx         */
#define CB_ENABLED     0xe7fa0u   /* isDisabled: 0                                  */
#define CB_END_EDIT    0xe7fb0u   /* endEdit: edit = -1                             */
#define CB_RET0_A      0xe7f60u   /* VERSION No.'s non-editable callbacks: return 0 */
#define CB_RET0_B      0xe7f90u
#define CB_RET0_C      0xe7f98u
#define CB_RET0_D      0xe7fa8u
#define CB_RET0_E      0xe7fc0u
/* first-frame boot-key handler (test mode / factory reset) */
#define BOOTKEY_FN     0x282630u
#define BOOTKEY_W0     0xe92d4070u   /* push {r4, r5, r6, lr} */
#define BOOTKEY_W1     0xe1a06001u   /* mov r6, r1            */

typedef uint16_t u16;
struct item {                 /* 0x34 bytes, rbp's layout */
    const u16 *label;
    const u16 *const *values;
    uint32_t nvalues;         /* read as a halfword */
    int32_t cur;              /* current choice (cached for rendering) */
    int32_t edit;             /* choice being edited, -1 when not editing */
    uint32_t enabled;
    uintptr_t cb_init, cb_render, cb_begin, cb_change, cb_commit, cb_disabled, cb_end;
};
_Static_assert(sizeof(struct item) == 0x34, "UTILITY item layout");

struct cell {                 /* a drawn text cell: u16 length at +0x20, text at +0x22 */
    uint8_t pad[0x20];
    u16 len;
    u16 text[0x100];
};

#define MAX_ROWS 16
struct row {
    const char *key;
    int def;
    xm_apply_fn apply;
    const u16 *label;
    const u16 **values;
    int nvalues;
};
static struct row rows[MAX_ROWS];
static int nrows;

static struct {
    uint32_t count;
    struct item it[STOCK_ITEMS + MAX_ROWS + 1];
} table __attribute__((aligned(8)));
static int first_row = STOCK_ITEMS - 1;   /* our rows sit before VERSION No. */

static const u16 empty16[1];

static const u16 *to_u16(const char *s)
{
    size_t n = strlen(s);
    u16 *w = calloc(n + 1, sizeof(u16));
    for (size_t i = 0; w && i < n; i++)
        w[i] = (unsigned char)s[i];
    return w ? w : empty16;
}

static int len16(const u16 *s)
{
    int n = 0;
    while (s[n])
        n++;
    return n;
}

static void set_cell(struct cell *c, const u16 *s)
{
    memset(c->text, 0, sizeof(c->text));
    int n = len16(s);
    if (n > (int)(sizeof(c->text) / 2) - 1)
        n = sizeof(c->text) / 2 - 1;
    c->len = (u16)n;
    memcpy(c->text, s, n * sizeof(u16));
}

static struct row *row_of(struct item *it)
{
    int i = (int)(it - table.it) - first_row;
    return (i >= 0 && i < nrows) ? &rows[i] : 0;
}

static int clamp_choice(const struct row *r, int v)
{
    return (v >= 0 && v < r->nvalues) ? v : r->def;
}

/* render(item, cells[2]): label in cells[0], value in cells[1] (the edit choice while
 * editing, else the saved one) - as rbp's own items do. */
static int cb_render(struct item *it, struct cell **cells)
{
    struct row *r = row_of(it);
    if (!r) {
        set_cell(cells[0], empty16);
        set_cell(cells[1], empty16);
        return 0;
    }
    set_cell(cells[0], r->label);
    int v = it->edit;
    if (v == -1) {
        v = clamp_choice(r, xm_cfg_get(r->key, r->def));
        it->cur = v;
    }
    set_cell(cells[1], (v >= 0 && v < r->nvalues) ? r->values[v] : empty16);
    return 0;
}

/* commit(item): store a changed choice and apply it; 1 = changed (rbp redraws). */
static int cb_commit(struct item *it)
{
    struct row *r = row_of(it);
    int v = it->edit;
    if (!r || v < 0 || v >= r->nvalues || v == it->cur)
        return 0;
    it->cur = v;
    xm_cfg_set(r->key, v);
    xm_log("%s = %d", r->key, v);
    if (r->apply)
        r->apply(v);
    return 1;
}

int xm_core_sites_ok(void)
{
    int ok = xm_expect32(TBL_COUNT, STOCK_ITEMS) && xm_expect32(ITEMS_REF, TBL_ITEMS) &&
             xm_expect32(BOOTKEY_FN, BOOTKEY_W0) && xm_expect32(BOOTKEY_FN + 4, BOOTKEY_W1);
    for (unsigned i = 0; ok && i < sizeof(COUNT_REFS) / sizeof(COUNT_REFS[0]); i++)
        ok = xm_expect32(COUNT_REFS[i], TBL_COUNT);
    return ok;
}

void xm_menu_begin(void)
{
    nrows = 0;
}

int xm_menu_add(const char *label, const char *const *values, int nvalues,
                const char *key, int def, xm_apply_fn apply)
{
    if (nrows == MAX_ROWS || nvalues <= 0 || def < 0 || def >= nvalues)
        return 0;
    struct row *r = &rows[nrows];
    r->key = key;
    r->def = def;
    r->apply = apply;
    r->label = to_u16(label);
    r->values = calloc(nvalues, sizeof(*r->values));
    if (!r->values)
        return 0;
    for (int i = 0; i < nvalues; i++)
        r->values[i] = to_u16(values[i]);
    r->nvalues = nvalues;
    nrows++;
    return 1;
}

int xm_menu_rows(void)
{
    return nrows;
}

void xm_menu_apply_all(void)
{
    for (int i = 0; i < nrows; i++)
        if (rows[i].apply)
            rows[i].apply(clamp_choice(&rows[i], xm_cfg_get(rows[i].key, rows[i].def)));
}

void xm_menu_install(void)
{
    xm_menu_apply_all();            /* every mod starts in its saved state */
    if (!nrows)
        return;
    const struct item *stock = (const struct item *)TBL_ITEMS;
    int n = 0;
    for (int i = 0; i < first_row; i++)
        table.it[n++] = stock[i];
    for (int i = 0; i < nrows; i++) {
        struct item *it = &table.it[n++];
        memset(it, 0, sizeof(*it));
        it->label = rows[i].label;
        it->values = rows[i].values;
        it->nvalues = rows[i].nvalues;
        it->cur = clamp_choice(&rows[i], xm_cfg_get(rows[i].key, rows[i].def));
        it->edit = -1;
        it->enabled = 1;
        it->cb_init = CB_NOP_INIT;
        it->cb_render = (uintptr_t)cb_render;
        it->cb_begin = CB_BEGIN_EDIT;
        it->cb_change = CB_CHANGE;
        it->cb_commit = (uintptr_t)cb_commit;
        it->cb_disabled = CB_ENABLED;
        it->cb_end = CB_END_EDIT;
    }
    for (int i = first_row; i < STOCK_ITEMS; i++)
        table.it[n++] = stock[i];
    /* sentinel: never selectable, draws nothing */
    struct item *s = &table.it[n];
    memset(s, 0, sizeof(*s));
    s->label = empty16;
    s->edit = -1;
    s->cb_init = CB_NOP_INIT;
    s->cb_render = (uintptr_t)cb_render;
    s->cb_begin = CB_RET0_A;
    s->cb_change = CB_RET0_B;
    s->cb_commit = CB_RET0_C;
    s->cb_disabled = CB_RET0_D;
    s->cb_end = CB_RET0_E;
    table.count = n;

    int ok = 1;
    for (unsigned i = 0; i < sizeof(COUNT_REFS) / sizeof(COUNT_REFS[0]); i++)
        ok &= xm_patch32(COUNT_REFS[i], TBL_COUNT, (uint32_t)(uintptr_t)&table.count);
    ok &= xm_patch32(ITEMS_REF, TBL_ITEMS, (uint32_t)(uintptr_t)&table.it[0]);
    if (!ok)
        xm_log("UTILITY rows not installed");
}

/* ---- factory reset ---- */
typedef void (*bootkey_fn)(void *self, const uint8_t *frame);
static bootkey_fn bootkey_orig;

static void bootkey_hook(void *self, const uint8_t *frame)
{
    /* TEMPO RANGE ([0x12] & 0x08) + DELETE ([0x13] & 0x08), not MEMORY (test mode) */
    if ((frame[0x12] & 0x08) && (frame[0x13] & 0x0c) == 0x08) {
        xm_log("factory reset: mod settings back to defaults");
        xm_cfg_reset();
        for (int i = 0; i < nrows; i++)
            table.it[first_row + i].cur = rows[i].def;
        xm_menu_apply_all();
    }
    bootkey_orig(self, frame);
}

void xm_factory_reset_install(void)
{
    bootkey_orig = (bootkey_fn)xm_detour(BOOTKEY_FN, BOOTKEY_W0, BOOTKEY_W1, (void *)bootkey_hook);
    if (!bootkey_orig)
        xm_log("factory-reset hook not installed");
}
