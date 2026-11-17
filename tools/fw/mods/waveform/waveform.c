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
 * low, mid, high, 0..127 each - measured against the audio, docs/firmware-mods.md section
 * 8). All three have one entry per PWV3 entry.
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

#define NDECKS        2

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
    uint8_t ncue;                      /* hot cues from PCO2 (type 1) */
    struct cue { uint8_t hc, loop; uint32_t ms, end_ms; uint16_t rgb; } cue[8];   /* rgb: RGB565, 0 = none */
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
    int r = (int)(px + 0.5f);
    return (uint8_t)(r > BAR_ROWS ? BAR_ROWS : r);
}

/* PWV7 entry (low, mid, high: 0..127) -> bar rows. The bytes are physically low / mid / high
 * (byte 0 follows < 400 Hz, byte 2 > 2.5 kHz); the weights give rekordbox's look - a broad
 * light core of highs, mids around it, bass outermost. */
static void band_rows(const uint8_t *e, uint8_t *out)
{
    out[0] = rows_of(e[0] * 0.6f);     /* low: blue */
    out[1] = rows_of(e[1] * 0.45f);    /* mid: amber */
    out[2] = rows_of(e[2] * 0.75f);    /* high: white */
}

/* PCO2 type 1 (hot cues A-H, rekordbox's extended cue list in the .EXT): entries PCP2 with
 * hot cue number +0x0c (1 = A), type +0x10 (1 point, 2 loop), time in ms +0x14, loop end +0x18,
 * comment length
 * +0x28 (UTF-16BE comment from +0x2c), then the colour: code, R, G, B (rekordbox's palette). */
static void parse_hot_cues(const uint8_t *d, size_t len, struct track *t)
{
    if (len < 12 || memcmp(d, "PMAI", 4))
        return;
    size_t o = be32(d + 4);
    while (o + 20 <= len) {
        uint32_t lh = be32(d + o + 4), lt = be32(d + o + 8);
        if (lt < 12 || o + lt > len)
            return;
        if (!memcmp(d + o, "PCO2", 4) && be32(d + o + 12) == 1) {
            unsigned n = be16(d + o + 16);
            size_t q = o + lh;
            for (unsigned i = 0; i < n && q + 0x18 <= o + lt && t->ncue < 8; i++) {
                uint32_t el = be32(d + q + 8);
                if (el < 0x18 || q + el > o + lt || memcmp(d + q, "PCP2", 4))
                    break;
                uint32_t hc = be32(d + q + 12);
                uint16_t rgb = 0;
                if (el >= 0x2c) {
                    uint32_t lc = be32(d + q + 0x28);
                    if (lc <= el && 0x2c + lc + 4 <= el) {
                        const uint8_t *c = d + q + 0x2c + lc;     /* code, R, G, B */
                        if (c[0] || c[1] || c[2] || c[3])
                            rgb = rgb565(c[1], c[2], c[3]);
                    }
                }
                if (hc >= 1 && hc <= 8)
                    t->cue[t->ncue++] = (struct cue){ (uint8_t)hc, d[q + 0x10] == 2,
                                                      be32(d + q + 0x14), be32(d + q + 0x18), rgb };
                q += el;
            }
        }
        o += lt;
    }
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
            band_rows(p7 + 3 * i, t.band + 3 * i);
        }
    }
    parse_hot_cues(d, len, &t);
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


/* ---- hot cues A-H: E-H shown, all in their rekordbox colours ----
 * rbp shows hot cues A-D only, always green (loops orange): its parser keeps E-H and the PCO2
 * colours, but nothing displays them. We parse PCO2 ourselves (parse_hot_cues) and:
 *  - detail: set the hot-cue bits for E-H in rbp's per-entry marker array (block +0x2ee10, 12
 *    bytes per entry; byte +2: 1 / 2 = top triangle (images 0x285 / 0x286), 4 / 8 = bottom one
 *    (0x272 / 0x273), the second of each for hot loops), so rbp draws its triangles there and
 *    it scrolls with the rest. rbp rebuilds the array when a deck's cues change (last builder
 *    0x12792c): re-applied after it, and when a deck's track / waveform changes. Colours:
 *    wf_detail_icons() recolours the triangles at each cue's column (called from scroll.c
 *    after rbp overlays the icon strips).
 *  - overview: rbp's cue layer 0x1f8a50 blits images with 0x129024(surface, x, y, id):
 *    hot-cue triangles 0x285 (top, y 4) / 0x272 (bottom, y 0x33), loops 0x284 / 0x273, letters
 *    A 0x274 B 0x27e C 0x280 D 0x282 (8 x 9 px, +1 = orange). While it runs, a blit detour
 *    drops the A-D letters; afterwards we draw all hot cues' triangles (over rbp's) and
 *    letters, laid out left to right without overlaps, in the cue's colour. The images are
 *    ours: ids from IMG_OURS, answered by a detour of the image lookup 0x1694bc with copies of
 *    rbp's records (same size and RGB565 format, 0xf81f = transparent) whose pixels we
 *    recolour - rbp's own triangle and A-D shapes, our E-H bitmaps in the same style. */
#define IMG_LOOKUP     0x1694bcu
#define IMG_LOOKUP_W0  0xe30033f5u        /* movw r3, #0x3f5 */
#define IMG_LOOKUP_W1  0xe1500003u        /* cmp r0, r3 */
#define IMG_BLIT       0x129024u          /* int (surface, x, y, image id) */
#define IMG_BLIT_W0    0xe92d47f0u        /* push {r4-sl, lr} */
#define IMG_BLIT_W1    0xe1a04000u        /* mov r4, r0 */
#define IMG_TRI_TOP    0x285
#define IMG_TRI_BOT    0x272
#define IMG_KEY        0xf81fu
#define IMG_OURS       0x400              /* deck*24 + cue*3 + {top, bottom, letter} */
#define CUE_LAYER      0x1f8a50u
#define CUE_LAYER_W0   0xe92d4ff0u        /* push {r4-fp, lr} */
#define CUE_LAYER_W1   0xe24dd0bcu        /* sub sp, sp, #0xbc */
#define CUE_SURFACES   0x123cd78u         /* per deck */
#define TRACK_MS       0x18a4510u         /* per deck: track length, ms */
#define CUE_BUILD      0x12792cu          /* last marker builder (deck, short *) */
#define CUE_BUILD_W0   0xe92d4ff8u        /* push {r3-fp, lr} */
#define CUE_BUILD_W1   0xe1a08001u        /* mov r8, r1 */
#define MK_HOTCUE      4
#define MK_TOP_CUE     1                  /* byte +2: top triangle (image 0x285) */
#define MK_TOP_LOOP    2                  /* (0x286) */
#define MK_HOTLOOP     8
#define CUE_GREEN      0x07e0u            /* rbp's hot cue / loop colours, for cues without one */
#define CUE_ORANGE     0xfaa0u

static const unsigned LETTER_IMG[4] = { 0x274, 0x27e, 0x280, 0x282 };   /* A-D */
static const char *const GLYPH_EH[4][9] = {
    { ".######.", ".#......", ".#......", ".#......", ".#####..", ".#......", ".#......", ".#......", ".######." },
    { ".######.", ".#......", ".#......", ".#......", ".#####..", ".#......", ".#......", ".#......", ".#......" },
    { "..###...", ".#...#..", "#.....#.", "#.......", "#...###.", "#.....#.", "#.....#.", ".#...##.", "..###.#." },
    { ".#....#.", ".#....#.", ".#....#.", ".#....#.", ".######.", ".#....#.", ".#....#.", ".#....#.", ".#....#." },
};

struct img { uint8_t rec[0x2c]; uint16_t px[16 * 9]; };
static struct img imgs[NDECKS][8][3];
static struct track *imgs_for[NDECKS];  /* the track whose colours imgs[deck] hold */
static int imgs_ok[NDECKS][8];

typedef void *(*img_lookup_fn)(unsigned id);
static img_lookup_fn img_lookup_orig;

static void *img_lookup_hook(unsigned id)
{
    if (id < IMG_OURS || id >= IMG_OURS + NDECKS * 24)
        return img_lookup_orig(id);
    unsigned k = id - IMG_OURS, d = k / 24, c = (k % 24) / 3, v = k % 3;
    return imgs_ok[d][c] ? imgs[d][c][v].rec : 0;
}


/* image of rbp's `src` (or our E-H glyph `eh` 0..3 when src == 0), every visible pixel in
 * colour c */
static int make_img(struct img *m, unsigned src, int eh, uint16_t c)
{
    const uint8_t *r = img_lookup_orig(src ? src : LETTER_IMG[0]);
    if (!r)
        return 0;
    unsigned w = ((const uint16_t *)r)[2], h = ((const uint16_t *)r)[3];
    const uint16_t *p = *(const uint16_t *const *)(r + 0xc);
    if (w * h > sizeof(m->px) / 2 || !p || (!src && (w != 8 || h != 9)))
        return 0;
    memcpy(m->rec, r, 0x2c);
    for (unsigned i = 0; i < w * h; i++) {
        if (src) {
            m->px[i] = p[i] == IMG_KEY ? IMG_KEY : c;
        } else {
            m->px[i] = GLYPH_EH[eh][i / 8][i % 8] == '#' ? c : IMG_KEY;
        }
    }
    *(uint16_t **)(m->rec + 0xc) = m->px;
    return 1;
}

static uint16_t cue_color(const struct cue *q)
{
    return q->rgb ? q->rgb : q->loop ? CUE_ORANGE : CUE_GREEN;
}

static void make_imgs(unsigned d, struct track *t)
{
    imgs_for[d] = t;
    for (unsigned i = 0; i < 8; i++) {
        imgs_ok[d][i] = 0;
        if (i >= t->ncue)
            continue;
        const struct cue *q = &t->cue[i];
        uint16_t c = cue_color(q);
        unsigned hc = q->hc;
        imgs_ok[d][i] =
            make_img(&imgs[d][i][0], q->loop ? 0x284 : IMG_TRI_TOP, 0, c) &&
            make_img(&imgs[d][i][1], q->loop ? 0x273 : IMG_TRI_BOT, 0, c) &&
            make_img(&imgs[d][i][2], hc <= 4 ? LETTER_IMG[hc - 1] : 0, (int)hc - 5, c);
    }
}

/* deck d's marker array: set E-H */
static void cues_apply(int d)
{
    struct track *t = deck_track(d);
    uint8_t *blk = deck_block(d);
    if (!t || !t->ncue || !blk)
        return;
    uint8_t *mk = *(uint8_t **)(blk + BLK_FLAGS);
    uint32_t count = *(uint32_t *)(blk + BLK_COUNT);
    for (unsigned i = 0; mk && i < t->ncue; i++) {
        if (t->cue[i].hc < 5)
            continue;                                   /* A-D: rbp's own */
        uint32_t e = (uint32_t)((uint64_t)t->cue[i].ms * 150 / 1000);
        if (e < count)                                  /* top + bottom triangles */
            mk[12 * e + 2] |= t->cue[i].loop ? MK_TOP_LOOP | MK_HOTLOOP : MK_TOP_CUE | MK_HOTCUE;
    }
}

static struct { struct track *t; uint32_t count; } cues_shown[NDECKS];

/* called per detail column: re-apply when a deck's track or waveform changed */
static void cues_check(int d)
{
    if (d < 0 || d >= NDECKS)
        return;
    struct track *t = deck_track(d);
    uint8_t *blk = deck_block(d);
    uint32_t count = blk ? *(uint32_t *)(blk + BLK_COUNT) : 0;
    if (t == cues_shown[d].t && count == cues_shown[d].count)
        return;
    cues_shown[d].t = t;
    cues_shown[d].count = count;
    cues_apply(d);
}

/* The detail's icon strips are on the canvas (rows 0-4 top, 119-123 bottom): recolour the
 * hot-cue triangles at each cue's column. Called by scroll.c after rbp's overlay (0x1cb3c4),
 * before its sub-pixel shift. */
#define DET_STATE(d)   (0x10f0208u + (d) * 0x20u)
#define DET_DECK_PX    (0x20fa0u / 2)
#define DET_PITCH      (0x430 / 2)
#define DET_PLAYHEAD_X 268
void wf_detail_icons(int d)
{
    if (d < 0 || d >= NDECKS)
        return;
    struct track *t = deck_track(d);
    uint16_t *canvas = *(uint16_t **)CANVAS_PTR;
    if (!t || !t->ncue || !canvas)
        return;
    const uint8_t *st = (const uint8_t *)DET_STATE(d);
    float zoom = *(const float *)(st + 0x18);
    int pos = (int)*(const uint32_t *)(st + 0x0c);
    if (!(zoom > 0.1f))
        return;
    uint16_t *base = canvas + DET_DECK_PX * (unsigned)d;
    for (unsigned i = 0; i < t->ncue; i++) {
        const struct cue *q = &t->cue[i];
        if (!q->rgb)
            continue;                                   /* stock colour: nothing to do */
        int col = (int)((float)((uint64_t)q->ms * 150 / 1000) / zoom);
        int x = DET_PLAYHEAD_X + col - pos;
        for (int r = 0; r < 124; r++) {
            if (r == 5)
                r = 119;                                /* icon rows: 0-4, 119-123 */
            uint16_t *row = base + DET_PITCH * r;
            for (int dx = -5; dx <= 5; dx++) {
                int xx = x + dx;
                if (xx < 0 || xx >= 536)
                    continue;
                uint16_t v = row[xx];
                if (v == 0x0700 || v == CUE_GREEN || v == CUE_ORANGE)   /* rbp's icon greens */
                    row[xx] = q->rgb;
            }
        }
    }
}

typedef int (*cue_build_fn)(int deck, short *n);
static cue_build_fn cue_build_orig;

static int cue_build_hook(int deck, short *n)
{
    int r = cue_build_orig(deck, n);
    if (deck >= 0 && deck < NDECKS)
        cues_apply(deck);
    return r;
}

typedef int (*blit_fn)(void *surf, int x, int y, unsigned id);
static blit_fn blit_orig;
static volatile int in_cue_layer;       /* gui_task only */

static int blit_hook(void *surf, int x, int y, unsigned id)
{
    if (in_cue_layer && (id == 0x274 || id == 0x275 || (id >= 0x27e && id <= 0x283)))
        return 0;                                      /* rbp's A-D letters: we draw them */
    return blit_orig(surf, x, y, id);
}

typedef void (*cue_layer_fn)(void);
static cue_layer_fn cue_layer_orig;

static void cue_layer_hook(void)
{
    unsigned deck = *(volatile uint32_t *)OV_DECK;
    struct track *t = deck < NDECKS ? deck_track(deck) : 0;
    uint32_t len = deck < NDECKS ? ((volatile uint32_t *)TRACK_MS)[deck] : 0;
    void *surf = deck < NDECKS ? ((void **)CUE_SURFACES)[deck] : 0;
    int ours = t && t->ncue && len && surf;
    if (ours && imgs_for[deck] != t)
        make_imgs(deck, t);
    in_cue_layer = ours;                               /* only then replace rbp's letters */
    cue_layer_orig();
    in_cue_layer = 0;
    if (!ours)
        return;
    /* cues by position */
    unsigned order[8], n = 0;
    for (unsigned i = 0; i < t->ncue; i++)
        if (imgs_ok[deck][i] && t->cue[i].ms <= len)
            order[n++] = i;
    for (unsigned a = 1; a < n; a++)
        for (unsigned b = a; b > 0 && t->cue[order[b]].ms < t->cue[order[b - 1]].ms; b--) {
            unsigned tmp = order[b]; order[b] = order[b - 1]; order[b - 1] = tmp;
        }
    int lx[8], next = 0;
    for (unsigned k = 0; k < n; k++) {                 /* labels left to right, no overlap */
        int x = (int)((uint64_t)t->cue[order[k]].ms * OV_COLS / len);
        lx[k] = x + 9 > next ? x + 9 : next;
        next = lx[k] + 8;
    }
    if (n && lx[n - 1] > OV_COLS - 8) {                /* off the right end: pack leftwards */
        int edge = OV_COLS - 8;
        for (int k = (int)n - 1; k >= 0 && lx[k] > edge; k--, edge -= 8)
            lx[k] = edge;
    }
    for (unsigned k = 0; k < n; k++) {                 /* triangles, over rbp's */
        unsigned i = order[k];
        int x = (int)((uint64_t)t->cue[i].ms * OV_COLS / len);
        unsigned id = IMG_OURS + deck * 24 + i * 3;
        blit_orig(surf, x + 1, 4, id);
        blit_orig(surf, x + 1, 0x33, id + 1);
    }
    for (unsigned k = 0; k < n; k++) {                 /* then the letters, on top */
        unsigned id = IMG_OURS + deck * 24 + order[k] * 3 + 2;
        blit_orig(surf, lx[k], 1, id);
        blit_orig(surf, lx[k], 0x31, id);
    }
}

static void cues_init(void)
{
    img_lookup_orig = (img_lookup_fn)xm_detour(IMG_LOOKUP, IMG_LOOKUP_W0, IMG_LOOKUP_W1,
                                               (void *)img_lookup_hook);
    cue_build_orig = (cue_build_fn)xm_detour(CUE_BUILD, CUE_BUILD_W0, CUE_BUILD_W1,
                                             (void *)cue_build_hook);
    if (img_lookup_orig)
        blit_orig = (blit_fn)xm_detour(IMG_BLIT, IMG_BLIT_W0, IMG_BLIT_W1, (void *)blit_hook);
    if (blit_orig)
        cue_layer_orig = (cue_layer_fn)xm_detour(CUE_LAYER, CUE_LAYER_W0, CUE_LAYER_W1,
                                                 (void *)cue_layer_hook);
    xm_log("waveform: hot cues A-H in colour on %s%s", cue_build_orig ? "detail" : "",
           cue_layer_orig ? " + overview" : "");
}

/* For the pad handler (beatjump.c): hot cue hc (5..8 = E..H) of the track on deck d, from
 * the deck's current match (no re-matching: callable from the key thread). Returns 0 if
 * there is none, 1 for a cue point, 2 for a hot loop (end_ms set). */
int wf_hot_cue(int d, unsigned hc, uint32_t *ms, uint32_t *end_ms)
{
    if (d < 0 || d >= NDECKS)
        return 0;
    struct track *t = decks[d].t;
    if (!t)
        return 0;
    for (unsigned i = 0; i < t->ncue && i < 8; i++)
        if (t->cue[i].hc == hc) {
            *ms = t->cue[i].ms;
            *end_ms = t->cue[i].end_ms;
            return t->cue[i].loop ? 2 : 1;
        }
    return 0;
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
    cues_check(deck);
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
            c = RGB565(248, 238, 218);            /* high: cream, on top */
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
                               : r < hi ? RGB565(248, 238, 218)
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
    cues_init();
}
