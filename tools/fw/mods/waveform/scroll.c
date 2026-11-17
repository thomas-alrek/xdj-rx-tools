/*
 * waveform mod: smooth scrolling of the detail waveform (UTILITY > WAVEFORM SCROLL:
 * ORIGINAL / SMOOTH, default SMOOTH).
 *
 * Credits: the idea of a smooth-scrolling waveform for the XDJ-RX comes from
 * JaNeinHub/XDJ-RX-Mod (https://github.com/JaNeinHub/XDJ-RX-Mod, GPL-3.0, "WAVEFORM SCROLL:
 * ORIGINAL / SMOOTH"). This is an independent implementation.
 *
 * Stock: each GUI frame the detail renderer (0x1cd0f8) clears the canvas and draws one
 * column per pixel, the column under the playhead line (x = 268) being
 * trunc(playhead_ms * 0.15 / zoom) (0x1cb58c, zoom = 1/150 s entries per column: 16, 8, 4
 * or 2). The playhead it uses is an integer-ms snapshot refreshed by another thread every
 * ~16 ms or more, so the waveform moves in whole pixels at irregular moments - judder,
 * worse at 60 fps.
 *
 * SMOOTH:
 *   1. 0x177cfc(deck) (the UI cycle task's per-deck playhead update) is detoured to
 *      timestamp every new playhead value (*(0x294e03c + d*0xb4), ms) and keep a
 *      position/velocity estimate per deck (snaps on jumps, stops on pause, never runs
 *      far past the last value).
 *   2. 0x1cb58c(deck) is detoured: after it, the position column is recomputed from the
 *      estimate at the current time - fractional, P = clamp(ms * 0.15, 0, count) / zoom -
 *      and its whole part stored where the renderer reads it (state block 0x10f0208 + d*32,
 *      +0x0c).
 *   3. 0x1cb3c4(deck) (the cue-icon overlay, the last step before the playhead line is
 *      drawn) is detoured: after it, the deck's 124 canvas rows are shifted left by the
 *      fraction (linear interpolation of neighbouring RGB565 pixels), so the waveform,
 *      beat ticks and cue icons glide while the playhead line stays sharp.
 *
 * Addresses: docs/firmware-mods.md section 10.
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#include "xdjmod.h"

#define NDECKS 2

#include <string.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

#define PH_UPDATE      0x177cfcu          /* Ui_CycleTask: per-deck playhead update */
#define PH_UPDATE_W0   0xe92d4ff0u        /* push {r4-fp, lr} */
#define PH_UPDATE_W1   0xe24dd00cu        /* sub sp, sp, #0xc */
#define PH_MS(d)       (0x294e03cu + (d) * 0xb4u)
#define RANGE_FN       0x1cb58cu          /* visible range of the detail view */
#define RANGE_W0       0xe0801100u        /* add r1, r0, r0, lsl #2 */
#define RANGE_W1       0xe59f2100u        /* ldr r2, [pc, #0x100] */
#define ICONS_FN       0x1cb3c4u          /* cue-icon overlay, per deck */
#define ICONS_W0       0xe59f2088u        /* ldr r2, [pc, #0x88] */
#define ICONS_W1       0xe3a03efau        /* mov r3, #0xfa0 */
#define STATE(d)       (0x10f0208u + (d) * 0x20u)
#define S_POS          0x0c               /* u32: column under the playhead line */
#define S_ZOOM         0x18               /* float: entries per column */
#define BLOCK_COUNT(d) (0x28d78a0u + (d) * 0x2ee1cu)
#define CANVAS_PTR     0x10f0248u         /* u16 *canvas */
#define DECK_BYTES     0x20fa0u
#define PITCH_PX       (0x430 / 2)
#define WIDTH_PX       0x218              /* 536 */
#define ROWS           124

enum { SCROLL_ORIGINAL, SCROLL_SMOOTH };
static volatile int scroll_mode = SCROLL_SMOOTH;

static uint64_t now_us(void)
{
    struct timespec ts;
    syscall(SYS_clock_gettime, CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000u + (uint64_t)ts.tv_nsec / 1000u;
}

/* ---- the estimate (written by the UI cycle task, read by gui_task) ---- */
struct est {
    volatile uint32_t seq;             /* odd while being written */
    double base_ms;                    /* position at base_us */
    uint64_t base_us;
    double v;                          /* ms of track per ms of time */
    /* writer only */
    uint32_t last_ms;
    uint64_t last_us;
    int still;                         /* updates without movement */
};
static struct est est[NDECKS];

#define SNAP_MS    60.0                /* larger errors are jumps: no smoothing */
#define MAX_AHEAD  100000u             /* never extrapolate more than 100 ms */

static double predict(const struct est *e, uint64_t t)
{
    uint64_t dt = t > e->base_us ? t - e->base_us : 0;
    if (dt > MAX_AHEAD)
        dt = MAX_AHEAD;
    return e->base_ms + e->v * (double)dt / 1000.0;
}

static void observe(int d, uint32_t ms)
{
    struct est *e = &est[d];
    uint64_t t = now_us();
    if (ms == e->last_ms && e->last_us) {
        if (++e->still < 3)
            return;                    /* the source may simply not have advanced yet */
    } else {
        e->still = 0;
    }
    double vi = 0;
    if (e->last_us && t > e->last_us && !e->still)
        vi = ((double)ms - (double)e->last_ms) * 1000.0 / (double)(t - e->last_us);
    double p = predict(e, t), err = (double)ms - p;
    e->seq++;
    __sync_synchronize();
    if (e->still || err > SNAP_MS || err < -SNAP_MS || !e->last_us) {
        e->base_ms = ms;               /* jump, start or pause: take it as is */
        e->v = e->still ? 0 : (vi > -8 && vi < 8 ? vi : 0);
    } else {
        e->base_ms = p + err * 0.15;   /* glide towards the source */
        if (vi > -8 && vi < 8)
            e->v += (vi - e->v) * 0.25;
    }
    e->base_us = t;
    __sync_synchronize();
    e->seq++;
    e->last_ms = ms;
    e->last_us = t;
}

static double estimate(int d, uint64_t t)
{
    const struct est *e = &est[d];
    for (;;) {
        uint32_t s = e->seq;
        __sync_synchronize();
        double r = predict(e, t);
        __sync_synchronize();
        if (!(s & 1) && s == e->seq)
            return r;
    }
}

typedef void (*deck_fn)(int);
static deck_fn ph_orig, range_orig, icons_orig;

static void ph_hook(int d)
{
    ph_orig(d);
    if ((unsigned)d < NDECKS)
        observe(d, *(volatile uint32_t *)PH_MS(d));
}

/* ---- per frame ---- */
static float frac[NDECKS];             /* sub-column part of the position, this frame */

static void range_hook(int d)
{
    range_orig(d);
    if ((unsigned)d >= NDECKS)
        return;
    frac[d] = 0;
    if (scroll_mode != SCROLL_SMOOTH)
        return;
    uint8_t *s = (uint8_t *)STATE(d);
    float zoom = *(float *)(s + S_ZOOM);
    uint32_t count = *(volatile uint32_t *)BLOCK_COUNT(d);
    if (!(zoom > 0.1f) || !count)
        return;
    double e = estimate(d, now_us()) * 0.15;
    if (e < 0)
        e = 0;
    if (e > count)
        e = count;
    double p = e / zoom;
    uint32_t col = (uint32_t)p;
    uint32_t stock = *(uint32_t *)(s + S_POS);
    if (col + 8 < stock || col > stock + 8)
        return;                        /* far from rbp's own value: trust rbp */
    *(uint32_t *)(s + S_POS) = col;
    frac[d] = (float)(p - col);
}

/* blend one row left by f32/32 of a pixel */
static void shift_row(uint16_t *px, unsigned f32, unsigned g)
{
    int x = 0;
    while (x < WIDTH_PX - 2) {
        /* black is most of the canvas: skip it two pixels at a time */
        if (!(x & 1) && *(const uint32_t *)(px + x) == 0 && px[x + 2] == 0) {
            x += 2;
            continue;
        }
        uint32_t a = px[x], b = px[x + 1];
        if (a != b) {
            /* RGB565 spread as 0000 0ggg ggg0 0000 rrrr r000 000b bbbb, blend, pack */
            uint32_t ea = (a | a << 16) & 0x07e0f81fu, eb = (b | b << 16) & 0x07e0f81fu;
            uint32_t m = ((ea * g + eb * f32) >> 5) & 0x07e0f81fu;
            px[x] = (uint16_t)(m | m >> 16);
        }
        x++;
    }
    if (px[WIDTH_PX - 2] != px[WIDTH_PX - 1]) {
        uint32_t a = px[WIDTH_PX - 2], b = px[WIDTH_PX - 1];
        uint32_t ea = (a | a << 16) & 0x07e0f81fu, eb = (b | b << 16) & 0x07e0f81fu;
        uint32_t m = ((ea * g + eb * f32) >> 5) & 0x07e0f81fu;
        px[WIDTH_PX - 2] = (uint16_t)(m | m >> 16);
    }
    px[WIDTH_PX - 1] = 0;              /* nothing beyond the right edge */
}

/*
 * Shift the deck's rows left by f32/32 pixel. rbp draws rows 0-61 (ticks, the bar with its
 * foot on row 61), mirrors rows 61..5 into 62..118 (and 4..0 into 119..123), then overlays
 * cue icons on rows 0-4 and 119-123: so blend rows 0-61 and 119-123, and copy the mirror.
 */
#define TOP_ROWS     62
#define MIRROR_FIRST 62
#define MIRROR_LAST  118
static void shift_rows(int d, unsigned f32)
{
    uint16_t *canvas = *(uint16_t **)CANVAS_PTR;
    if (!canvas)
        return;
    uint16_t *base = canvas + (DECK_BYTES / 2) * (unsigned)d;
    unsigned g = 32 - f32;
    for (int r = 0; r < TOP_ROWS; r++)
        shift_row(base + PITCH_PX * r, f32, g);
    for (int r = MIRROR_FIRST; r <= MIRROR_LAST; r++)
        memcpy(base + PITCH_PX * r, base + PITCH_PX * (2 * MIRROR_FIRST - 1 - r), WIDTH_PX * 2);
    for (int r = MIRROR_LAST + 1; r < ROWS; r++)
        shift_row(base + PITCH_PX * r, f32, g);
}

void wf_detail_icons(int d);              /* waveform.c: hot-cue colours */

static void icons_hook(int d)
{
    icons_orig(d);
    wf_detail_icons(d);
    if ((unsigned)d >= NDECKS || scroll_mode != SCROLL_SMOOTH)
        return;
    unsigned f32 = (unsigned)(frac[d] * 32.0f + 0.5f);
    if (f32 == 0 || f32 >= 32)
        return;
    shift_rows(d, f32);
}

static void set_scroll(int choice)
{
    scroll_mode = choice;
}

static const char *const SCROLLS[] = { "ORIGINAL", "SMOOTH" };

XM_MODULE(scroll)
{
    ph_orig = (deck_fn)xm_detour(PH_UPDATE, PH_UPDATE_W0, PH_UPDATE_W1, (void *)ph_hook);
    if (!ph_orig)
        return;
    range_orig = (deck_fn)xm_detour(RANGE_FN, RANGE_W0, RANGE_W1, (void *)range_hook);
    icons_orig = (deck_fn)xm_detour(ICONS_FN, ICONS_W0, ICONS_W1, (void *)icons_hook);
    if (!range_orig || !icons_orig) {
        xm_log("scroll: not installed");
        return;
    }
    xm_menu_add("WAVEFORM SCROLL", SCROLLS, 2, "wfscroll", SCROLL_SMOOTH, set_scroll);
    xm_log("scroll: smooth detail scrolling");
}
