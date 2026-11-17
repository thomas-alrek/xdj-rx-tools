/*
 * SCREEN SYNC: keep each frame's copy into the screen out of the panel's readout (no tearing).
 *
 * rbp draws into a single, front-only framebuffer. For every frame DirectFB waits for the
 * panel's end of frame (ioctl FBIO_WAITFORVSYNC on /dev/fb0, from gui_task) and then copies
 * the finished picture in (~2 ms on the unit). The panel reads the framebuffer top to bottom
 * over each refresh (16.8 ms, CVT 800x480@60), and the copy - started just after the panel
 * began reading - overtakes that readout part-way down the screen: the picture shows the old
 * frame above that row and the new one below, a tear (row ~93 on a real unit, through the
 * waveform; measured by JaNeinHub/XDJ-RX-Mod, whose idea this is - our own implementation).
 *
 * With SCREEN SYNC on, a thread of ours does the refresh waits and keeps the refresh period
 * and phase, and gui_task's wait becomes a sleep until LEAD before the next refresh, so the
 * copy runs while the readout is in the last rows / the blanking and stays ahead of it.
 * The kernel serialises framebuffer calls during a wait, so our thread enters the driver only
 * shortly (WAKE_EARLY) before each expected refresh. Until the thread has a trustworthy model,
 * and whenever it stops (driver error, a wait that doesn't wait), gui_task waits as Pioneer's
 * code does.
 *
 * UTILITY > SCREEN SYNC = OFF (default) / 1.5 / 3.0 / 4.5 / 6.0 MS: the lead. Opt-in and for
 * testing on real units: the emulator can't judge the lead (its guest copies at TCG speed and
 * wakes earlier than the unit; there, any lead tears more than OFF - docs/firmware-mods.md).
 * 4.5 ms is what JaNeinHub measured on a unit; OFF is exactly stock behaviour.
 *
 * Every 10 s: "sync: period .. ms, gui waits .. (synced ..), driver waits .., resets ..".
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#include "xdjmod.h"

#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <sched.h>
#include <stdarg.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

#define FBIO_WAITFORVSYNC 0x40044620u   /* _IOW('F', 0x20, __u32): MXCFB_WAIT_FOR_VSYNC */
#define PERIOD_NOMINAL_US 16810         /* CVT 800x480@60: 29.5 MHz, 992 x 500 */
#define PERIOD_MIN_US     15000
#define PERIOD_MAX_US     18500
#define WAKE_EARLY_US     3000          /* our thread enters the driver this early */
#define MODEL_MIN_SAMPLES 8             /* refreshes seen before gui_task uses the model */

static const char *const LEADS[] = { "OFF", "1.5 MS", "3.0 MS", "4.5 MS", "6.0 MS" };
static const int LEAD_US[] = { 0, 1500, 3000, 4500, 6000 };
#define NLEADS (int)(sizeof(LEAD_US) / sizeof(LEAD_US[0]))
static volatile int enabled;            /* the UTILITY setting: not OFF */
static volatile int dead;               /* the thread gave up: Pioneer's wait from now on */
static volatile int lead_us = 4500;
static volatile pthread_t sync_tid;    /* our thread (no TLS: the library has no TLS model) */
static volatile int sync_tid_set;

typedef int (*ioctl_fn)(int, unsigned long, ...);
static ioctl_fn real_ioctl;

/* raw syscalls: glibc 2.13 has clock_gettime / clock_nanosleep in librt */
static uint64_t now_us(void)
{
    struct timespec ts;
    syscall(SYS_clock_gettime, CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000u + (uint64_t)ts.tv_nsec / 1000u;
}

static void sleep_until_us(uint64_t t)
{
    struct timespec ts = { (time_t)(t / 1000000u), (long)(t % 1000000u) * 1000 };
    while (syscall(SYS_clock_nanosleep, CLOCK_MONOTONIC, TIMER_ABSTIME, &ts, NULL) != 0 &&
           errno == EINTR) {
    }
}

/* ---- the refresh model: refresh k at anchor + k * period (us), published by a seqlock ---- */
static struct {
    volatile uint32_t seq;
    uint64_t anchor;
    uint32_t period;                    /* 0: no model */
} model;

static void model_publish(uint64_t anchor, uint32_t period)
{
    __atomic_add_fetch(&model.seq, 1, __ATOMIC_ACQ_REL);   /* odd: writing */
    model.anchor = anchor;
    model.period = period;
    __atomic_add_fetch(&model.seq, 1, __ATOMIC_ACQ_REL);   /* even: done */
}

static int model_read(uint64_t *anchor, uint32_t *period)
{
    for (int tries = 0; tries < 4; tries++) {
        uint32_t s0 = __atomic_load_n(&model.seq, __ATOMIC_ACQUIRE);
        if (s0 & 1)
            continue;
        uint64_t a = model.anchor;
        uint32_t p = model.period;
        if (__atomic_load_n(&model.seq, __ATOMIC_ACQUIRE) == s0) {
            *anchor = a;
            *period = p;
            return p != 0;
        }
    }
    return 0;
}

/* The first refresh after t: anchor + k * period > t. */
static uint64_t next_refresh(uint64_t t, uint64_t anchor, uint32_t period)
{
    if (t < anchor)
        return anchor;
    return anchor + ((t - anchor) / period + 1) * (uint64_t)period;
}

/* ---- statistics (logged by the thread every 10 s) ---- */
static volatile uint32_t st_gui, st_gui_synced, st_driver, st_resets;

/* ---- gui_task's refresh wait ---- */
static int gui_wait(int fd, unsigned long req, void *arg)
{
    uint64_t anchor;
    uint32_t period;
    st_gui++;
    if (!enabled || dead || !model_read(&anchor, &period))
        return real_ioctl(fd, req, arg);              /* Pioneer's wait */
    /* the first refresh whose "refresh - lead" is still ahead; sleep until that moment */
    uint64_t t = now_us();
    uint64_t target = next_refresh(t + (uint64_t)lead_us, anchor, period) - (uint64_t)lead_us;
    int e = errno;
    sleep_until_us(target);
    errno = e;
    st_gui_synced++;
    return 0;
}

int ioctl(int fd, unsigned long req, ...)
{
    va_list ap;
    va_start(ap, req);
    void *arg = va_arg(ap, void *);
    va_end(ap);
    if (!real_ioctl)
        real_ioctl = (ioctl_fn)dlsym(RTLD_NEXT, "ioctl");
    if (req == FBIO_WAITFORVSYNC && enabled && !dead &&
        !(sync_tid_set && pthread_equal(pthread_self(), sync_tid)))
        return gui_wait(fd, req, arg);
    return real_ioctl(fd, req, arg);
}

/* ---- the thread: refresh waits, period and phase ---- */
static void *sync_thread(void *unused)
{
    (void)unused;
    sync_tid = pthread_self();
    __atomic_store_n(&sync_tid_set, 1, __ATOMIC_RELEASE);
    int fd = open("/dev/fb0", O_RDWR);
    if (fd < 0) {
        xm_log("sync: /dev/fb0 not opened (errno %d) - Pioneer's refresh wait", errno);
        dead = 1;
        return 0;
    }
    struct sched_param rt = { 30 };
    int rt_ok = pthread_setschedparam(pthread_self(), SCHED_FIFO, &rt) == 0;
    uint64_t last = 0, anchor = 0, log_at = now_us() + 10000000u;
    uint32_t period = PERIOD_NOMINAL_US, good = 0, fast = 0, errs = 0;
    uint32_t zero = 0;
    xm_log("sync: thread up (%s)", rt_ok ? "real-time" : "normal priority");
    for (;;) {
        uint64_t t = now_us();
        if (t >= log_at) {
            xm_log("sync: %s, lead %d us, period %u.%02u ms, gui waits %u (synced %u), driver waits %u, resets %u",
                   enabled ? "on" : "off", enabled ? lead_us : 0, period / 1000, period % 1000 / 10, st_gui, st_gui_synced,
                   st_driver, st_resets);
            st_gui = st_gui_synced = st_driver = st_resets = 0;
            log_at = t + 10000000u;
        }
        if (!enabled) {                                /* off: forget the model, idle */
            model_publish(0, 0);
            last = 0;
            good = 0;
            usleep(200000);
            continue;
        }
        if (good >= 2)                                 /* enter the driver just before it */
            sleep_until_us(next_refresh(t + WAKE_EARLY_US, anchor, period) - WAKE_EARLY_US);
        uint64_t t_in = now_us();
        if (real_ioctl(fd, FBIO_WAITFORVSYNC, &zero) != 0) {
            if (++errs >= 5) {
                xm_log("sync: refresh wait fails (errno %d) - Pioneer's refresh wait from now on", errno);
                break;
            }
            usleep(20000);
            continue;
        }
        errs = 0;
        t = now_us();
        st_driver++;
        if (t - t_in < 200) {                         /* the driver didn't wait */
            if (++fast >= 20) {
                xm_log("sync: refresh wait returns at once - Pioneer's refresh wait from now on");
                break;
            }
        } else {
            fast = 0;
        }
        if (last) {
            uint32_t dt = (uint32_t)(t - last);
            uint32_t k = (dt + period / 2) / period;   /* refreshes since the last one */
            if (k >= 1 && k <= 8) {
                uint32_t m = dt / k;
                if (m >= PERIOD_MIN_US && m <= PERIOD_MAX_US) {
                    period = period + (int32_t)(m - period) / 8;
                    good++;
                } else {
                    good = 0;
                    st_resets++;
                }
            } else {
                good = 0;
                st_resets++;
            }
        }
        last = t;
        anchor = t;
        if (good >= MODEL_MIN_SAMPLES)
            model_publish(anchor, period);
        else
            model_publish(0, 0);
    }
    model_publish(0, 0);
    dead = 1;
    close(fd);
    return 0;
}

static void set_sync(int choice)
{
    if (choice < 0 || choice >= NLEADS)
        choice = 0;
    if (choice)
        lead_us = LEAD_US[choice];
    enabled = choice != 0;
    xm_log("sync: SCREEN SYNC %s", LEADS[choice]);
}

XM_MODULE(sync)
{
    real_ioctl = (ioctl_fn)dlsym(RTLD_NEXT, "ioctl");
    if (!real_ioctl)
        return;
    pthread_t th;
    if (pthread_create(&th, 0, sync_thread, 0) != 0) {
        xm_log("sync: no thread - Pioneer's refresh wait");
        return;
    }
    pthread_detach(th);
    xm_menu_add("SCREEN SYNC", LEADS, NLEADS, "screen_sync", 0, set_sync);   /* default: OFF */
}
