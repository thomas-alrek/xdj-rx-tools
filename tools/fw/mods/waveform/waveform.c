/*
 * waveform mod: RGB and 3-band colour waveforms (UTILITY > WAVEFORM COLOR:
 * BLUE (stock) / RGB / 3-BAND).
 *
 * Credits: JaNeinHub/XDJ-RX-Mod (https://github.com/JaNeinHub/XDJ-RX-Mod, GPL-3.0) was the
 * first RGB / 3-band waveform mod for the XDJ-RX and showed these hook points; the
 * 3-band layering and colours follow Deep Symmetry's beat-link (CDJ-3000 look). This is
 * an independent implementation.
 *
 * Data. rbp reads rekordbox's analysis files itself but only knows the blue tags (PWV3
 * etc.). When it opens a track's ANLZ*.EXT, our fopen/open interposers parse that file
 * and its .2EX sibling and cache, per track: PWV3 (to recognise the track), PWV5 (RGB,
 * 2 bytes per 1/150 s: R 15-13, G 12-10, B 9-7, height 6-2) and PWV7 (3-band, 3 bytes:
 * mid, high, low). All three have one entry per PWV3 entry.
 *
 * Decks. rbp decodes PWV3 into a per-deck array of {height = (b & 31) * 46 / 31,
 * whiteness = b >> 5} (0x12a034); deck d's block is **(0x10f020c + d*32) - count at +0,
 * entries at +0x2ee18, 12-byte marker flags at +0x2ee10. We match a deck to a cached
 * track by comparing sampled entries with the cached PWV3, and re-check periodically.
 *
 * Drawing. The scrolling detail is drawn column by column by 0x1caf58(s0 = zoom, r0 =
 * deck, r1 = x, r2/r3 = first/last entry): bar height = rbp's tallest entry in the range
 * (clamped 1..45), 45 rows on an RGB565 canvas (*0x10f0248, 536 px per row, deck d at
 * d*0x107d0 px, the bar's foot at byte 0xff70, rows going up by 0x430 bytes); above the
 * bar, black or a marker colour (0xb300 / 0x3186 by flag bits 1/2). We detour it: in
 * BLUE, or for a deck we can't match, the original runs; otherwise we draw the same
 * column - same geometry and markers - in the track's colours. Zoom arrives in s0 (a
 * VFP register), so two small assembly shims carry it.
 *
 * Addresses: docs/firmware-mods.md section 8.
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#define _GNU_SOURCE
#include "xdjmod.h"

#include <dlfcn.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>

/* stat/fstat are header wrappers around __xstat in glibc 2.13 (libc_nonshared); the
 * stat64 syscalls need nothing from libc. dlsym binds to the firmware's libdl (the
 * library is linked against the firmware's own libraries - preload/build.sh). */

static int file_info(const char *path, int fd, long long *size, long *mtime)
{
    struct stat64 st;
    long r = path ? syscall(SYS_stat64, path, &st) : syscall(SYS_fstat64, fd, &st);
    if (r != 0)
        return -1;
    *size = st.st_size;
    *mtime = st.st_mtime;
    return 0;
}

/* rbp 2.21 */
#define DETAIL_FN     0x1caf58u
#define DETAIL_W0     0xe59fc19cu        /* ldr ip, [pc, #0x19c]  (=0x10f0208) */
#define DETAIL_W1     0xeef77a00u        /* vmov.f32 s15, #1.0                 */
#define DECK_TABLE    0x10f020cu         /* deck d's block: **(DECK_TABLE + d*32) */
#define CANVAS_PTR    0x10f0248u         /* u16 *canvas                         */
#define DECK_PIXELS   0x107d0u
#define ROW_PIXELS    (0x430 / 2)
#define FOOT_PIXEL    (0xff70 / 2)
#define BAR_ROWS      45
#define BLK_COUNT     0
#define BLK_FLAGS     0x2ee10
#define BLK_ENTRIES   0x2ee18
#define NDECKS        2
/* overview (whole-track) strip: 0x1f8518 draws the current deck's (*0x123cd50) 300 x 34
 * px bar strip into DirectFB surface ((void **)0x123cd60)[deck], locked with 0x13ca98(surf,
 * &pixels, &pitch_bytes) and unlocked with 0x13cb00(surf); column x shows PWAV entry
 * x + x/3 of 400, its foot on row 33. */
#define OVERVIEW_FN   0x1f8518u
#define OVERVIEW_W0   0xe92d4ff0u        /* push {r4-fp, lr} */
#define OVERVIEW_W1   0xe24dd01cu        /* sub sp, sp, #0x1c */
#define OV_DECK       0x123cd50u
#define OV_SURFACES   0x123cd60u
#define OV_LOCK       0x13ca98u
#define OV_UNLOCK     0x13cb00u
#define OV_COLS       300
#define OV_ROWS       34
#define OV_ENTRIES    400

enum { MODE_BLUE, MODE_RGB, MODE_3BAND };
static volatile int mode = MODE_3BAND;  /* the default (and factory-reset) choice */
static volatile int active;            /* the module is installed in rbp */

/* ---- cached tracks ---- */
struct track {
    char path[256];
    long long size;
    long mtime;
    uint32_t n;                        /* entries (PWV3) */
    uint8_t *pwv3;                     /* raw PWV3 bytes */
    uint16_t *rgb;                     /* RGB565 per entry, or 0 */
    uint8_t *band;                     /* per entry: low, mid, high bar rows (0..45), or 0 */
    uint32_t used;                     /* LRU stamp */
};
#define NTRACKS 8
static struct track tracks[NTRACKS];
static uint32_t lru_clock;
static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;

/* per-deck match (read by the drawing thread without the lock: one aligned pointer) */
struct deckmatch {
    struct track *t;
    const uint8_t *entries;            /* rbp's entry array when matched */
    uint32_t count;
    uint32_t checked_ms;
};
static struct deckmatch decks[NDECKS];

/* ---- ANLZ parsing ---- */
static uint32_t be32(const uint8_t *p) { return (uint32_t)p[0] << 24 | p[1] << 16 | p[2] << 8 | p[3]; }
static uint16_t be16(const uint8_t *p) { return (uint16_t)(p[0] << 8 | p[1]); }

typedef int (*open_fn)(const char *, int, ...);
typedef FILE *(*fopen_fn)(const char *, const char *);
static open_fn real_open, real_open64;
static fopen_fn real_fopen, real_fopen64;

static void resolve(void)
{
    if (!real_open) {
        real_open = (open_fn)dlsym(RTLD_NEXT, "open");
        real_open64 = (open_fn)dlsym(RTLD_NEXT, "open64");
        real_fopen = (fopen_fn)dlsym(RTLD_NEXT, "fopen");
        real_fopen64 = (fopen_fn)dlsym(RTLD_NEXT, "fopen64");
    }
}

/* Read a whole (small) file with the real open. */
static uint8_t *slurp(const char *path, size_t *len)
{
    int fd = real_open(path, O_RDONLY);
    if (fd < 0)
        return 0;
    long long fsize;
    long mt;
    uint8_t *buf = 0;
    if (file_info(0, fd, &fsize, &mt) == 0 && fsize > 0 && fsize < (16 << 20) &&
        (buf = malloc(fsize))) {
        size_t got = 0;
        while (got < (size_t)fsize) {
            ssize_t r = read(fd, buf + got, fsize - got);
            if (r <= 0)
                break;
            got += r;
        }
        if (got != (size_t)fsize) {
            free(buf);
            buf = 0;
        } else {
            *len = got;
        }
    }
    close(fd);
    return buf;
}

/* Find tag `name` in an ANLZ file: entry data, entry count, entry size. */
static const uint8_t *find_tag(const uint8_t *d, size_t len, const char *name, uint32_t *count,
                               uint32_t *esize)
{
    if (len < 12 || memcmp(d, "PMAI", 4))
        return 0;
    size_t o = be32(d + 4);
    while (o + 12 <= len) {
        uint32_t lh = be32(d + o + 4), lt = be32(d + o + 8);
        if (lt < 12 || o + lt > len)
            return 0;
        if (!memcmp(d + o, name, 4) && lh >= 20 && lh <= lt) {
            *esize = be32(d + o + 12);
            *count = be32(d + o + 16);
            if ((uint64_t)*esize * *count > lt - lh)
                return 0;
            return d + o + lh;
        }
        o += lt;
    }
    return 0;
}

static uint16_t rgb565(int r, int g, int b)
{
    return (uint16_t)((r >> 3) << 11 | (g >> 2) << 5 | (b >> 3));
}

/*
 * PWV5's 3-bit channels are mostly high on all three (e.g. 7,6,5), so a linear map is
 * near-white pastel; rekordbox shows saturated hues. Normalise the strongest channel to
 * full brightness and stretch the others away from it (x2.5): 7,6,5 -> orange-salmon,
 * 5,7,6 -> green, 6,6,7 -> lavender-blue; equal channels stay neutral.
 */
static uint16_t pwv5_color(unsigned r, unsigned g, unsigned b)
{
    unsigned m = r > g ? (r > b ? r : b) : (g > b ? g : b);
    if (!m)
        return 0;
    int c[3] = { (int)r, (int)g, (int)b };
    for (int i = 0; i < 3; i++) {
        int v = c[i] * 255 / (int)m;                  /* strongest channel -> 255 */
        v = 255 - (255 - v) * 5 / 2;                  /* saturate */
        c[i] = v < 0 ? 0 : v;
    }
    return rgb565(c[0], c[1], c[2]);
}

static uint8_t rows_of(float px)
{
    int r = (int)(px * 1.5f + 0.5f);   /* beat-link half-heights -> our 45 rows */
    return (uint8_t)(r > BAR_ROWS ? BAR_ROWS : r);
}

static void load_track(const char *ext_path)
{
    long long fsize;
    long mt;
    if (file_info(ext_path, -1, &fsize, &mt) != 0)
        return;
    pthread_mutex_lock(&lock);
    for (int i = 0; i < NTRACKS; i++)
        if (tracks[i].pwv3 && !strcmp(tracks[i].path, ext_path) && tracks[i].size == fsize &&
            tracks[i].mtime == mt) {
            tracks[i].used = ++lru_clock;      /* already cached */
            pthread_mutex_unlock(&lock);
            return;
        }
    pthread_mutex_unlock(&lock);

    size_t len = 0, len2 = 0;
    uint8_t *d = slurp(ext_path, &len);
    if (!d)
        return;
    uint32_t n3 = 0, n5 = 0, n7 = 0, s3 = 0, s5 = 0, s7 = 0;
    const uint8_t *p3 = find_tag(d, len, "PWV3", &n3, &s3);
    const uint8_t *p5 = find_tag(d, len, "PWV5", &n5, &s5);
    char path2[256];
    snprintf(path2, sizeof(path2), "%.*s2EX", (int)strlen(ext_path) - 3, ext_path);
    uint8_t *d2 = slurp(path2, &len2);
    const uint8_t *p7 = d2 ? find_tag(d2, len2, "PWV7", &n7, &s7) : 0;
    if (!p3 || s3 != 1 || !n3) {
        free(d);
        free(d2);
        return;
    }
    struct track t = { .size = fsize, .mtime = mt, .n = n3 };
    snprintf(t.path, sizeof(t.path), "%s", ext_path);
    t.pwv3 = malloc(n3);
    if (t.pwv3)
        memcpy(t.pwv3, p3, n3);
    if (p5 && s5 == 2 && n5 >= n3 && (t.rgb = malloc(n3 * 2))) {
        for (uint32_t i = 0; i < n3; i++) {
            uint16_t v = be16(p5 + 2 * i);
            t.rgb[i] = pwv5_color((v >> 13) & 7, (v >> 10) & 7, (v >> 7) & 7);
        }
    }
    if (p7 && s7 == 3 && n7 >= n3 && (t.band = malloc(n3 * 3))) {
        for (uint32_t i = 0; i < n3; i++) {
            const uint8_t *e = p7 + 3 * i;     /* mid, high, low */
            t.band[3 * i + 0] = rows_of(e[2] * 0.4f);
            t.band[3 * i + 1] = rows_of(e[0] * 0.3f);
            t.band[3 * i + 2] = rows_of(e[1] * 0.06f);
        }
    }
    free(d);
    free(d2);
    if (!t.pwv3)
        return;

    pthread_mutex_lock(&lock);
    int slot = 0;                               /* least recently used, not on a deck */
    for (int i = 1; i < NTRACKS; i++)
        if (tracks[i].used < tracks[slot].used)
            slot = i;
    for (int k = 0; k < NDECKS; k++)
        if (decks[k].t == &tracks[slot])
            decks[k].t = 0;
    struct track old = tracks[slot];
    t.used = ++lru_clock;
    tracks[slot] = t;
    pthread_mutex_unlock(&lock);
    free(old.pwv3);
    free(old.rgb);
    free(old.band);
    xm_log("waveform: cached %s (%u entries%s%s)", ext_path, t.n, t.rgb ? ", RGB" : "",
           t.band ? ", 3-band" : "");
}

static int is_ext(const char *path)
{
    size_t n = path ? strlen(path) : 0;
    return n > 4 && !strcasecmp(path + n - 4, ".EXT");
}

/* ---- interposers: rbp opens ANLZ*.EXT through one of these ---- */
FILE *fopen(const char *path, const char *m)
{
    resolve();
    if (active && is_ext(path))
        load_track(path);
    return real_fopen(path, m);
}

FILE *fopen64(const char *path, const char *m)
{
    resolve();
    if (active && is_ext(path))
        load_track(path);
    return real_fopen64(path, m);
}

int open(const char *path, int flags, ...)
{
    va_list ap;
    va_start(ap, flags);
    int m = (flags & O_CREAT) ? va_arg(ap, int) : 0;
    va_end(ap);
    resolve();
    if (active && is_ext(path) && !(flags & (O_WRONLY | O_RDWR)))
        load_track(path);
    return real_open(path, flags, m);
}

int open64(const char *path, int flags, ...)
{
    va_list ap;
    va_start(ap, flags);
    int m = (flags & O_CREAT) ? va_arg(ap, int) : 0;
    va_end(ap);
    resolve();
    if (active && is_ext(path) && !(flags & (O_WRONLY | O_RDWR)))
        load_track(path);
    return real_open64(path, flags, m);
}

/* ---- deck <-> track ---- */
static uint8_t *deck_block(int d)
{
    uint8_t **ref = *(uint8_t ***)(DECK_TABLE + 32u * d);    /* two levels, as 0x1caf78/80 */
    return ref ? *ref : 0;
}

static int entries_match(const struct track *t, const uint8_t *e, uint32_t count)
{
    /* rbp's array runs a little past the file's PWV3 (26513 vs 26493 seen): pad allowed */
    if (!t->pwv3 || count == 0 || count + 1 < t->n || count > t->n + 256)
        return 0;
    uint32_t n = count < t->n ? count : t->n;
    for (int k = 0; k < 48; k++) {
        uint32_t i = (uint32_t)((uint64_t)n * k / 48);
        uint8_t b = t->pwv3[i];
        if (e[2 * i] != (uint8_t)((b & 31) * 46 / 31) || e[2 * i + 1] != (b >> 5))
            return 0;
    }
    return 1;
}

/* The cached track shown on deck d, or 0 (then the stock drawing runs). */
static struct track *deck_track(int d)
{
    if (d < 0 || d >= NDECKS)
        return 0;
    uint8_t *blk = deck_block(d);
    if (!blk)
        return 0;
    const uint8_t *e = *(const uint8_t **)(blk + BLK_ENTRIES);
    uint32_t count = *(const uint32_t *)(blk + BLK_COUNT);
    struct deckmatch *m = &decks[d];
    uint32_t now = xm_now_ms();
    if (m->t && m->entries == e && m->count == count && now - m->checked_ms < 500)
        return m->t;
    m->checked_ms = now;
    if (!e || !count) {
        m->t = 0;
        return 0;
    }
    if (m->t && entries_match(m->t, e, count)) {
        m->entries = e;
        m->count = count;
        return m->t;
    }
    struct track *found = 0;
    pthread_mutex_lock(&lock);
    for (int i = 0; i < NTRACKS && !found; i++)
        if (tracks[i].pwv3 && entries_match(&tracks[i], e, count))
            found = &tracks[i];
    pthread_mutex_unlock(&lock);
    if (found != m->t)
        xm_log("waveform: deck %d -> %s", d + 1, found ? found->path : "(no colour data)");
    m->t = found;
    m->entries = e;
    m->count = count;
    return found;
}

/* ---- the detail column ---- */
void *wf_detail_orig;   /* the stub: rbp's own column routine */
__attribute__((visibility("hidden"))) void wf_call_orig(int deck, int x, unsigned first,
                                                        unsigned last, uint32_t zoom_bits,
                                                        void *orig);

#define RGB565(r, g, b) (uint16_t)(((r) >> 3) << 11 | ((g) >> 2) << 5 | ((b) >> 3))

/* Called (via wf_detail_entry) for every column: draws it in colour, or runs the original. */
__attribute__((visibility("hidden"))) void wf_detail_c(int deck, int x, unsigned first,
                                                       unsigned last, uint32_t zoom_bits)
{
    static int seen;
    if (!seen && mode != MODE_BLUE) {
        seen = 1;
        float z;
        memcpy(&z, &zoom_bits, 4);
        xm_log("waveform: column hook live: deck %d x %d entries %u..%u zoom %d/100", deck, x,
               first, last, (int)(z * 100));
    }
    struct track *t = mode == MODE_BLUE ? 0 : deck_track(deck);
    if (!t || (mode == MODE_RGB && !t->rgb) || (mode == MODE_3BAND && !t->band) ||
        first >= t->n) {
        wf_call_orig(deck, x, first, last, zoom_bits, wf_detail_orig);
        return;
    }
    uint8_t *blk = deck_block(deck);
    const uint8_t *e = *(const uint8_t **)(blk + BLK_ENTRIES);
    float zoom;
    memcpy(&zoom, &zoom_bits, 4);

    /* the bar: rbp's height (tallest entry in range when zoomed out), as stock */
    unsigned h = e[2 * first], best = first;
    unsigned lo = 0, mid = 0, hi = 0;
    if (zoom > 1.0f && zoom <= 32.0f)
        for (unsigned i = first; i < last && i < t->n; i++)
            if (e[2 * i] > h) {
                h = e[2 * i];
                best = i;
            }
    if (h == 0)
        h = 1;
    else if (h > 44)
        h = BAR_ROWS;
    if (mode == MODE_3BAND) {
        unsigned end = (zoom > 1.0f && zoom <= 32.0f && last > first) ? last : first + 1;
        for (unsigned i = first; i < end && i < t->n; i++) {
            const uint8_t *b = t->band + 3 * i;
            if (b[0] > lo) lo = b[0];
            if (b[1] > mid) mid = b[1];
            if (b[2] > hi) hi = b[2];
        }
    }

    uint16_t *col = *(uint16_t **)CANVAS_PTR + DECK_PIXELS * deck + x + FOOT_PIXEL;
    uint8_t flags = (*(const uint8_t **)(blk + BLK_FLAGS))[12 * first + 8];
    uint16_t marker = (flags & 4) ? 0x3186 : (flags & 2) ? 0xb300 : 0;
    uint16_t rgb = mode == MODE_RGB ? t->rgb[best] : 0;
    unsigned bar = mode == MODE_3BAND ? (lo > mid ? lo : mid) : h;
    if (mode == MODE_3BAND && hi > bar)
        bar = hi;

    for (unsigned r = 0; r < BAR_ROWS; r++, col -= ROW_PIXELS) {
        uint16_t c;
        if (r >= bar) {
            c = marker;
        } else if (mode == MODE_RGB) {
            c = rgb;
        } else if (r < hi) {
            c = RGB565(255, 255, 255);            /* high: white, on top */
        } else if (r < lo && r < mid) {
            c = RGB565(169, 107, 39);             /* low + mid overlap: brown */
        } else if (r < lo) {
            c = RGB565(32, 83, 217);              /* low: blue */
        } else {
            c = RGB565(242, 170, 60);             /* mid: amber */
        }
        *col = c;
    }
}

/* Entry: rbp calls with s0 = zoom and r0-r3 = deck, x, first, last; pass the zoom on as a
 * fifth (stack) argument. Call-orig: put the zoom back in s0 and jump to the stub. */
__asm__(".text\n.arm\n.align 2\n"
        ".global wf_detail_entry\n.hidden wf_detail_entry\n.type wf_detail_entry, %function\n"
        "wf_detail_entry:\n"
        "  push {r4, lr}\n"
        "  sub sp, sp, #8\n"
        "  vmov r4, s0\n"
        "  str r4, [sp]\n"
        "  bl wf_detail_c\n"
        "  add sp, sp, #8\n"
        "  pop {r4, pc}\n"
        ".global wf_call_orig\n.hidden wf_call_orig\n.type wf_call_orig, %function\n"
        "wf_call_orig:\n"
        "  ldr ip, [sp]\n"
        "  vmov s0, ip\n"
        "  ldr ip, [sp, #4]\n"
        "  bx ip\n");
void wf_detail_entry(void);

/* ---- the overview strip: let rbp draw it, then repaint each bar in colour ---- */
typedef void (*void_fn)(void);
typedef int (*ov_lock_fn)(void *surf, uint16_t **pixels, unsigned *pitch);
typedef void (*ov_unlock_fn)(void *surf);
static void_fn ov_orig;

static void unpack565(uint16_t c, unsigned *r, unsigned *g, unsigned *b)
{
    *r = (c >> 11) << 3;
    *g = ((c >> 5) & 63) << 2;
    *b = (c & 31) << 3;
}

static void ov_hook(void)
{
    ov_orig();
    if (mode == MODE_BLUE)
        return;
    unsigned deck = *(volatile uint32_t *)OV_DECK;
    struct track *t = deck < NDECKS ? deck_track(deck) : 0;
    if (!t || (mode == MODE_RGB && !t->rgb) || (mode == MODE_3BAND && !t->band))
        return;
    void *surf = ((void **)OV_SURFACES)[deck];
    uint16_t *px = 0;
    unsigned pitch = 0;
    if (!surf || ((ov_lock_fn)OV_LOCK)(surf, &px, &pitch) != 0)
        return;
    if (px) {
        unsigned stride = pitch / 2;
        for (unsigned x = 0; x < OV_COLS; x++) {
            uint16_t *foot = px + stride * (OV_ROWS - 1) + x;
            unsigned h = 0;                         /* the bar rbp drew */
            while (h < OV_ROWS && foot[-(int)(stride * h)])
                h++;
            if (!h)
                continue;
            unsigned e = x + x / 3;                 /* PWAV entry of this column */
            unsigned i0 = (unsigned)((uint64_t)e * t->n / OV_ENTRIES);
            unsigned i1 = (unsigned)((uint64_t)(e + 1) * t->n / OV_ENTRIES);
            if (i1 <= i0)
                i1 = i0 + 1;
            if (i1 > t->n)
                i1 = t->n;
            if (i0 >= i1)
                continue;
            if (mode == MODE_RGB) {
                unsigned sr = 0, sg = 0, sb = 0, n = i1 - i0;
                for (unsigned i = i0; i < i1; i++) {
                    unsigned r, g, b;
                    unpack565(t->rgb[i], &r, &g, &b);
                    sr += r; sg += g; sb += b;
                }
                uint16_t c = RGB565(sr / n, sg / n, sb / n);
                for (unsigned r = 0; r < h; r++)
                    foot[-(int)(stride * r)] = c;
            } else {
                unsigned lo = 0, mid = 0, hi = 0, n = i1 - i0;
                for (unsigned i = i0; i < i1; i++) {
                    lo += t->band[3 * i];
                    mid += t->band[3 * i + 1];
                    hi += t->band[3 * i + 2];
                }
                lo = lo * OV_ROWS / (n * BAR_ROWS);  /* 45-row units -> 34 rows */
                mid = mid * OV_ROWS / (n * BAR_ROWS);
                hi = hi * OV_ROWS / (n * BAR_ROWS);
                unsigned bar = lo > mid ? lo : mid;
                if (hi > bar)
                    bar = hi;
                for (unsigned r = 0; r < OV_ROWS; r++) {
                    uint16_t c = r >= bar ? 0
                               : r < hi ? RGB565(255, 255, 255)
                               : (r < lo && r < mid) ? RGB565(169, 107, 39)
                               : r < lo ? RGB565(32, 83, 217) : RGB565(242, 170, 60);
                    foot[-(int)(stride * r)] = c;
                }
            }
        }
    }
    ((ov_unlock_fn)OV_UNLOCK)(surf);
}

static void set_mode(int choice)
{
    mode = choice;
}

static const char *const MODES[] = { "BLUE (STOCK)", "RGB", "3-BAND" };

XM_MODULE(waveform)
{
    resolve();
    wf_detail_orig = xm_detour(DETAIL_FN, DETAIL_W0, DETAIL_W1, (void *)wf_detail_entry);
    if (!wf_detail_orig)
        return;
    active = 1;
    ov_orig = (void_fn)xm_detour(OVERVIEW_FN, OVERVIEW_W0, OVERVIEW_W1, (void *)ov_hook);
    if (!ov_orig)
        xm_log("waveform: overview hook not installed (detail only)");
    xm_menu_add("WAVEFORM COLOR", MODES, 3, "waveform", MODE_3BAND, set_mode);
}
