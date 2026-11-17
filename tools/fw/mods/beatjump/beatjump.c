/*
 * beatjump mod: BEAT JUMP on the performance pads, opt-in (UTILITY > BEAT JUMP: OFF
 * (default) / ON; BEAT JUMP A / BEAT JUMP B: 1, 2, 4, 8, 16 or 32 beats).
 *
 * Credits: beat jump on the XDJ-RX's pads via SHIFT + BEAT LOOP comes from
 * JaNeinHub/XDJ-RX-Mod (https://github.com/JaNeinHub/XDJ-RX-Mod, GPL-3.0, "BEAT JUMP A / B").
 * This is an independent implementation.
 *
 * Use (with BEAT JUMP = ON): SHIFT + BEAT LOOP switches a deck's pads to beat jump (the
 * BEAT LOOP LED blinks); HOT CUE, BEAT LOOP or LOOP SLICE switch back to rbp's own pad
 * modes. In beat jump: pad A jumps back by BEAT JUMP A, pad B forward by A, pad C back by
 * BEAT JUMP B, pad D forward by B. The jump keeps the position within the beat (it moves
 * by whole beats of the track's beat grid; without a grid, by rbp's detected BPM, else
 * 120 BPM) and keeps playing.
 *
 * rbp: every deck key reaches ui::PlayerInnards::onKey (0x236784, vtable slot 0x421f08):
 * KeyEvent +8 code (0x4114 BEAT LOOP, 0x4113 HOT CUE, 0x4115 LOOP SLICE, 0x4117-0x411a pads
 * A-D), +0xb low nibble 0 press / 2 release; PlayerInnards +0x26 deck (1/2), +0x34 SHIFT
 * held, +0x80 the list of held pads. We point the slot at our handler, which takes the
 * keys above in beat jump mode (press and release, so rbp's held-pad list stays
 * consistent) and passes everything else on. Position: PlayEngine::getPosition 0x4410c
 * (samples at 44.1 kHz); jump: Player::jump 0x6b274(player, samples) - the same call a hot
 * cue makes. Beat grid: the deck's GUI block (0x28d78a0 + d*0x2ee1c: +8 has grid, +0xc
 * beats, +0x10 {u32 ms, u32 beat}). LEDs: the panel LED encoder 0x21e6fc is detoured to
 * blink BEAT LOOP and light the pads of a deck in beat jump mode (TX byte 0x02 + 4*deck).
 *
 * Addresses: docs/firmware-mods.md section 10a.
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#include "xdjmod.h"

#include <pthread.h>
#include <stdint.h>
#include <string.h>

#define NDECKS        2
#define ONKEY_SLOT    0x421f08u           /* PlayerInnards vtable, slot 2 */
#define ONKEY_STOCK   0x00236784u
#define LED_FN        0x21e6fcu           /* panel LED encoder */
#define LED_W0        0xe92d40f0u         /* push {r4-r7, lr} */
#define LED_W1        0xe24dd00cu         /* sub sp, sp, #0xc */
#define PLAYENGINE    0x1079cb0u
#define GET_POS       0x4410cu            /* int (PlayEngine*, deck): samples */
#define PLAYER_JUMP   0x6b274u            /* void (Player*, int samples) */
#define HAS_BPM       0x5e1e8u            /* int (Player*) */
#define GET_BPM100    0x5e200u            /* int (Player*): bpm x 100 */
#define BLOCK(d)      (0x28d78a0u + (d) * 0x2ee1cu)
#define B_HASGRID     0x8
#define B_BEATS       0xc
#define B_BEAT0       0x10
#define DECK_STATE(d) (0x17ec92cu + 0x5cu * (d))
#define STATE_LOADED  0x12

#define K_HOTCUE      0x4113
#define K_BEATLOOP    0x4114
#define K_SLICE       0x4115
#define K_PAD_A       0x4117
#define K_PAD_D       0x411a
#define PI_DJ         0x30                /* PlayerInnards: DjEngineIF* */
#define PI_PADMODE    0x7c                /* 0/1 HOT CUE, 2 BEAT LOOP, 3 SLICE */
#define PLAY_HOT_CUE  0x544f0u            /* DjEngineIF::playHotCue(dj, deck, id, loop, 0) */
#define SLIP_ON       0x55d48u            /* (dj, deck) */
#define SLIP_HOT      0x55df8u            /* (dj, deck) */
#define EXIT_SLIP     0x55ea8u            /* (dj, deck) */
#define GETCUE_SLOT   0x35db78u           /* PcmReader vtable +0x58: getCue(pcm, id) */
#define GETCUE_STOCK  0x0003d384u         /* *(pcm + 0xb4) + id * 64 */
#define P_PCM         0xbc                /* Player: PcmReader holder */

static volatile int enabled;           /* UTILITY > BEAT JUMP */
static volatile int size_a = 2, size_b = 4;   /* indices into SIZES */
static volatile int mode[NDECKS];      /* deck's pads are in beat jump */
static volatile int eh_enabled;        /* UTILITY > HOT CUE E-H */
static void *volatile innards_of[NDECKS];
static const int SIZES[] = { 1, 2, 4, 8, 16, 32 };
static const char *const SIZE_NAMES[] = { "1 BEAT", "2 BEATS", "4 BEATS", "8 BEATS",
                                          "16 BEATS", "32 BEATS" };

typedef int (*onkey_fn)(void *innards, void *event);
static onkey_fn onkey_orig = (onkey_fn)ONKEY_STOCK;

static void *player(int d)
{
    uint8_t *pe = *(uint8_t **)PLAYENGINE;
    if (!pe)
        return 0;
    void **begin = *(void ***)(pe + 0xc), **end = *(void ***)(pe + 0x10);
    return begin && end - begin > d ? begin[d] : 0;
}

/* ms of track time to move by `beats` (signed) from `ms` */
static double beat_delta(int d, void *p, double ms, int beats)
{
    const uint8_t *b = (const uint8_t *)BLOCK(d);
    uint32_t n = *(const uint32_t *)(b + B_BEATS);
    const uint32_t *beat = (const uint32_t *)(b + B_BEAT0);   /* {ms, beat#} pairs */
    if (*(const uint32_t *)(b + B_HASGRID) && n >= 2 && n < 24000) {
        uint32_t lo = 0, hi = n - 1;   /* last beat at or before ms */
        if (ms < beat[0])
            hi = 0;
        while (lo < hi) {
            uint32_t mid = (lo + hi + 1) / 2;
            if (beat[2 * mid] <= ms)
                lo = mid;
            else
                hi = mid - 1;
        }
        long i = lo, j = i + beats;
        double t_i = beat[2 * i], t_j;
        if (j >= 0 && j < (long)n) {
            t_j = beat[2 * j];
        } else if (j < 0) {            /* before the grid: its first interval */
            double iv = (double)beat[2] - beat[0];
            t_j = beat[0] + iv * (double)j;
        } else {                       /* past the grid: its last interval */
            double iv = (double)beat[2 * (n - 1)] - beat[2 * (n - 2)];
            t_j = beat[2 * (n - 1)] + iv * (double)(j - (long)(n - 1));
        }
        return t_j - t_i;
    }
    double bpm = 120.0;
    if (((int (*)(void *))HAS_BPM)(p)) {
        int b100 = ((int (*)(void *))GET_BPM100)(p);
        if (b100 >= 4000 && b100 <= 40000)
            bpm = b100 / 100.0;
    }
    return beats * 60000.0 / bpm;
}

static void jump(int d, int beats)
{
    if (*(volatile int *)DECK_STATE(d) != STATE_LOADED)
        return;
    void *p = player(d);
    uint8_t *pe = *(uint8_t **)PLAYENGINE;
    if (!p || !pe)
        return;
    int cur = ((int (*)(void *, int))GET_POS)(pe, d);
    double ms = cur / 44.1;
    double delta = beat_delta(d, p, ms, beats);
    long target = cur + (long)(delta * 44.1 + (delta < 0 ? -0.5 : 0.5));
    if (target < 0)
        target = 0;
    ((void (*)(void *, int))PLAYER_JUMP)(p, (int)target);
    xm_log("beatjump: deck %d: %+d beats, %.0f -> %.0f ms", d + 1, beats, ms, target / 44.1);
}

/* ---- hot cues E-H on SHIFT + pads ----
 * rbp's hot-cue engine has room for A-D only (cue records and LoopQuantize slots 1..4), so
 * E-H borrow slot A-D of the same pad for one call: the PcmReader's getCue (vtable slot
 * 0x35db78) is pointed at getcue_hook, which - only on the calling thread, only during the
 * call, only for that reader and slot - returns a copy of the slot's record holding the E-H
 * position (samples at 44.1 kHz, +0x20; loop end +0x24 or -1; +0x3e set). The stock
 * DjEngineIF::playHotCue then does exactly what a hot cue press does (quantize, play from a
 * pause, hot loops). E-H positions come from the waveform mod (rekordbox's PCO2 tag). */
int wf_hot_cue(int d, unsigned hc, uint32_t *ms, uint32_t *end_ms) __attribute__((weak));

static struct {
    volatile int active;
    pthread_t thread;
    void *pcm;
    int k;
    uint8_t rec[64];
} ovr;
static uint8_t eh_down[NDECKS][4];     /* pad pressed as E-H: its release is ours too */

static void *getcue_hook(void *pcm, int id)
{
    if (ovr.active && pcm == ovr.pcm && id == ovr.k && pthread_equal(pthread_self(), ovr.thread))
        return ovr.rec;
    return *(uint8_t **)((uint8_t *)pcm + 0xb4) + id * 64;
}

/* SHIFT + pad `pad` (0..3) on deck d: play hot cue E+pad if the track has it. 1 = handled. */
static int eh_press(uint8_t *pi, int d, int pad)
{
    uint32_t ms = 0, end = 0;
    int kind = wf_hot_cue ? wf_hot_cue(d, 5u + (unsigned)pad, &ms, &end) : 0;
    uint8_t *p = player(d);
    uint8_t *pcm = p ? *(uint8_t **)(p + P_PCM) : 0;
    uint8_t *recs = pcm ? *(uint8_t **)(pcm + 0xb4) : 0;
    if (!kind || !recs || *(volatile int *)DECK_STATE(d) != STATE_LOADED)
        return 0;
    int k = pad + 1;
    memcpy(ovr.rec, recs + k * 64, 64);
    *(uint32_t *)(ovr.rec + 0x20) = (uint32_t)(ms * 44.1);
    *(uint32_t *)(ovr.rec + 0x24) = kind == 2 && end > ms ? (uint32_t)(end * 44.1) : 0xffffffffu;
    ovr.rec[0x3e] = 1;
    ovr.pcm = pcm;
    ovr.k = k;
    ovr.thread = pthread_self();
    __sync_synchronize();
    ovr.active = 1;
    void *dj = *(void **)(pi + PI_DJ);
    int r = ((int (*)(void *, int, int, int, int))PLAY_HOT_CUE)(dj, d, k, 1, 0);
    ovr.active = 0;
    xm_log("beatjump: deck %d: hot cue %c (%s) at %u ms -> %d", d + 1, 'E' + pad,
           kind == 2 ? "loop" : "cue", ms, r);
    return 1;
}

static void eh_release(uint8_t *pi, int d, int pad)
{
    void *dj = *(void **)(pi + PI_DJ);
    if (((int (*)(void *, int))SLIP_ON)(dj, d) && ((int (*)(void *, int))SLIP_HOT)(dj, d)) {
        uint8_t *p = player(d);
        ovr.pcm = p ? *(uint8_t **)(p + P_PCM) : 0;
        ovr.k = pad + 1;
        ovr.thread = pthread_self();
        __sync_synchronize();
        ovr.active = 1;                            /* SLIP returns via getCue(slot) too */
        ((void (*)(void *, int))EXIT_SLIP)(dj, d);
        ovr.active = 0;
    }
}

static int onkey_hook(void *innards, void *event)
{
    uint8_t *pi = innards, *ev = event;
    unsigned code = *(uint16_t *)(ev + 8);
    unsigned state = ev[0xb] & 0xf;
    int d = pi[0x26] == 2;
    int shift = pi[0x34] != 0 || (ev[0xb] & 0x10);
    int press = state == 0;
    innards_of[d] = pi;

    /* beat jump (its own pad mode: SHIFT + BEAT LOOP) */
    if (enabled) {
        if (code == K_BEATLOOP && shift) {
            void *held = *(void **)(pi + 0x80);
            if (press && held == (void *)(pi + 0x80)) {   /* no pad held */
                mode[d] = !mode[d];
                xm_log("beatjump: deck %d pads: %s", d + 1, mode[d] ? "BEAT JUMP" : "rbp");
            }
            return press;
        }
        if (code >= K_HOTCUE && code <= K_SLICE && press)
            mode[d] = 0;               /* back to rbp's pad modes, and let rbp switch */
        if (mode[d] && code >= K_PAD_A && code <= K_PAD_D) {
            if (press) {
                int k = (int)(code - K_PAD_A);
                int n = SIZES[k < 2 ? size_a : size_b];
                jump(d, k & 1 ? n : -n);
            }
            return 1;
        }
    }

    /* hot cues E-H: SHIFT + pads in HOT CUE mode (not in beat jump) */
    if (code >= K_PAD_A && code <= K_PAD_D && !mode[d]) {
        int pad = (int)(code - K_PAD_A);
        if (!press && eh_down[d][pad]) {           /* release of an E-H press */
            eh_down[d][pad] = 0;
            eh_release(pi, d, pad);
            return 1;
        }
        if (press && eh_enabled && shift && pi[PI_PADMODE] <= 1 && eh_press(pi, d, pad)) {
            eh_down[d][pad] = 1;
            return 1;
        }
    }
    return onkey_orig(innards, event);
}

/* ---- LEDs ---- */
typedef int (*led_fn)(void *, void *, void *, void *);
static led_fn led_orig;

static int led_hook(void *obj, void *a1, void *a2, void *a3)
{
    int r = led_orig(obj, a1, a2, a3);
    int blink = (xm_now_ms() / 250) & 1;
    for (int d = 0; d < NDECKS; d++) {
        uint8_t *pi = innards_of[d];
        if (eh_enabled && !mode[d] && pi && pi[0x34] && pi[PI_PADMODE] <= 1 && wf_hot_cue) {
            uint8_t *b = (uint8_t *)obj + 0x10 + 2 + 4 * d;   /* SHIFT held: pads with E-H */
            uint8_t v = (uint8_t)(*b & 0x0f);
            for (int pad = 0; pad < 4; pad++) {
                uint32_t ms, end;
                if (wf_hot_cue(d, 5u + (unsigned)pad, &ms, &end))
                    v |= (uint8_t)(0x80 >> pad);           /* .7 = pad A ... .4 = pad D */
            }
            *b = v;
            continue;
        }
        if (!enabled || !mode[d])
            continue;
        uint8_t *b = (uint8_t *)obj + 0x10 + 2 + 4 * d;
        uint8_t v = (uint8_t)(*b & 0x08);          /* keep bit 3 */
        v |= 0xf0;                                 /* pads A-D lit */
        if (blink)
            v |= 0x02;                             /* BEAT LOOP blinks */
        *b = v;
    }
    return r;
}

static void set_enabled(int c)
{
    enabled = c;
    if (!c)
        mode[0] = mode[1] = 0;
}
static void set_eh(int c) { eh_enabled = c; }
static void set_a(int c) { size_a = c; }
static void set_b(int c) { size_b = c; }
static const char *const ONOFF[] = { "OFF", "ON" };
static const char *const EH_MODES[] = { "OFF", "SHIFT+PAD" };

XM_MODULE(beatjump)
{
    if (!xm_expect32(ONKEY_SLOT, ONKEY_STOCK))
        return;
    led_orig = (led_fn)xm_detour(LED_FN, LED_W0, LED_W1, (void *)led_hook);
    if (!led_orig)
        xm_log("beatjump: no LED feedback");
    if (!xm_patch32(ONKEY_SLOT, ONKEY_STOCK, (uint32_t)(uintptr_t)onkey_hook))
        return;
    xm_menu_add("BEAT JUMP", ONOFF, 2, "beatjump", 0, set_enabled);
    xm_menu_add("BEAT JUMP A", SIZE_NAMES, 6, "beatjump_a", 2, set_a);
    xm_menu_add("BEAT JUMP B", SIZE_NAMES, 6, "beatjump_b", 4, set_b);
    if (wf_hot_cue && xm_patch32(GETCUE_SLOT, GETCUE_STOCK, (uint32_t)(uintptr_t)getcue_hook))
        xm_menu_add("HOT CUE E-H", EH_MODES, 2, "hotcue_eh", 0, set_eh);
    else
        xm_log("beatjump: hot cues E-H on the pads not installed");
    xm_log("beatjump: SHIFT + BEAT LOOP -> beat jump pads (opt-in)");
}
