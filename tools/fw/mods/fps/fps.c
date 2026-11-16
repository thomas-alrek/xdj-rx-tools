/*
 * fps mod: the UI frame-rate cap (UTILITY > UI FRAME RATE: 20 (STOCK) / 30 / 60).
 *
 * rbp's gui_task (0xb3a68) draws a frame, then sleeps (50 - frame time) ms if the frame
 * took <= 49 ms: a fixed 50 ms period, 20 fps at most. The two immediates are
 * 0xb3aac `cmp r4, #49` and 0xb3ad8 `rsb r0, r4, #50`; we rewrite them for a period P.
 *
 * Each frame also advances rbp's UI timers (blinks, pop-up timeouts, ...) by a fixed 50
 * ms (0x12a314 `mov r0, #50` -> 0x1655b0). Above 20 fps that would run them fast, so at
 * 30/60 the detour on 0x1655b0 passes the real elapsed time instead. At 20 (STOCK)
 * nothing is changed: the immediates are stock and the timers count frames as before.
 *
 * Every 10 s it logs the GUI frame interval (the time between two frames) for
 * benchmarking: "fps: P=.. ms  23.4 fps  interval avg/min/max ..".
 *
 * Addresses: docs/firmware-mods.md section 5.
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#include "xdjmod.h"

#define LOOP_CMP   0xb3aacu                   /* cmp r4, #(P-1) */
#define LOOP_RSB   0xb3ad8u                   /* rsb r0, r4, #P */
#define CMP_WORD(p) (0xe3540000u | (uint32_t)((p) - 1))
#define RSB_WORD(p) (0xe2640000u | (uint32_t)(p))
#define TIMERS_FN  0x1655b0u                  /* ui timers: advance all by r0 ms */
#define TIMERS_W0  0xe92d45f0u                /* push {r4-r8, sl, lr} */
#define TIMERS_W1  0xe1a07000u                /* mov r7, r0           */

static const char *const RATES[] = { "20 (STOCK)", "30", "60" };
static const int PERIOD_MS[] = { 50, 33, 17 };
#define NRATES (int)(sizeof(PERIOD_MS) / sizeof(PERIOD_MS[0]))

static volatile int period_ms = 50;
typedef void (*timers_fn)(int ms);
static timers_fn timers_orig;

/* frame-interval statistics, logged every 10 s */
static uint32_t win_start, win_frames, win_sum, win_min = ~0u, win_max;

static void stats(uint32_t now, uint32_t interval)
{
    if (!win_start)
        win_start = now;
    win_frames++;
    win_sum += interval;
    if (interval < win_min)
        win_min = interval;
    if (interval > win_max)
        win_max = interval;
    uint32_t span = now - win_start;
    if (span >= 10000 && win_frames > 1) {
        unsigned fps10 = (unsigned)((uint64_t)win_frames * 10000u / span);
        xm_log("fps: P=%d ms  %u.%u fps  interval avg %u min %u max %u ms (%u frames)",
               period_ms, fps10 / 10, fps10 % 10, win_sum / win_frames, win_min, win_max,
               win_frames);
        win_start = now;
        win_frames = win_sum = win_max = 0;
        win_min = ~0u;
    }
}

/* Called once per GUI frame with 50; at a raised rate pass the real elapsed time. */
static void timers_hook(int ms)
{
    static uint32_t last;
    uint32_t now = xm_now_ms();
    uint32_t elapsed = last ? now - last : (uint32_t)ms;
    last = now;
    stats(now, elapsed);
    if (period_ms != 50) {
        ms = elapsed > 1000 ? 1000 : (int)elapsed;   /* cap after a stall */
    }
    timers_orig(ms);
}

static int known_word(uint32_t w, int cmp)
{
    for (int i = 0; i < NRATES; i++)
        if (w == (cmp ? CMP_WORD(PERIOD_MS[i]) : RSB_WORD(PERIOD_MS[i])))
            return 1;
    return 0;
}

static void set_rate(int choice)
{
    int p = PERIOD_MS[choice];
    uint32_t c = xm_read32(LOOP_CMP), r = xm_read32(LOOP_RSB);
    if (!known_word(c, 1) || !known_word(r, 0)) {
        xm_log("fps: loop words changed (0x%08x 0x%08x) - not touching", c, r);
        return;
    }
    /* sleep first (so a lower P never meets the old compare), then the compare */
    xm_patch32(LOOP_RSB, r, RSB_WORD(p));
    xm_patch32(LOOP_CMP, c, CMP_WORD(p));
    period_ms = p;
    xm_log("fps: UI frame period %d ms (%s fps)", p, RATES[choice]);
}

XM_MODULE(fps)
{
    if (!xm_expect32(LOOP_CMP, CMP_WORD(50)) || !xm_expect32(LOOP_RSB, RSB_WORD(50)))
        return;
    timers_orig = (timers_fn)xm_detour(TIMERS_FN, TIMERS_W0, TIMERS_W1, (void *)timers_hook);
    if (!timers_orig)
        return;
    xm_menu_add("UI FRAME RATE", RATES, NRATES, "fps", 2, set_rate);   /* default: 60 */
}
