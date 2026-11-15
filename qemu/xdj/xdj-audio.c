/*
 * Pioneer XDJ-RX emulation -- audio: SDMA engine, SSI/AUDMUX, paced period clock to the host audio backend
 *
 * Part of the XDJ-RX device models for the i.MX6 SoC (hw/arm/fsl-imx6.c calls
 * into these from its realize; see xdj.h). Maintained in the xdj-rx-tools repo
 * under qemu/xdj/ and copied into hw/arm/xdj/ by qemu/build-qemu.sh -- edit it
 * there, not in the QEMU tree.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#include "qemu/osdep.h"
#include "qapi/error.h"
#include "qemu/timer.h"
#include "qemu/audio.h"
#include "hw/ssi/ssi.h"
#include "hw/i2c/i2c.h"
#include "hw/arm/fsl-imx6.h"
#include "xdj.h"
#include "hw/misc/unimp.h"
#include "hw/usb/imx-usb-phy.h"
#include "hw/usb/chipidea.h"
#include "hw/usb/ehci-regs.h"
#include "hw/core/boards.h"
#include "hw/core/qdev-properties.h"
#include "system/system.h"
#include "chardev/char.h"
#include "chardev/char-fe.h"
#include "qemu/error-report.h"
#include "qemu/module.h"
#include "target/arm/cpu-qom.h"
#include "ui/console.h"
#include "system/address-spaces.h"
#include "system/runstate.h"
#include "qemu/main-loop.h"
#include "qemu/thread.h"
#include "qemu/rcu.h"
#ifdef __APPLE__
#include <pthread/qos.h>
#endif

/* ============================================================================
 * Audio time base: minimal i.MX6 SDMA + SSI + AUDMUX model (Option A).
 *
 * The CDJ/XDJ deck playhead is slaved to the audio sample clock: rbp's REMAIN
 * only counts down as PCM frames are consumed by the hardware.  Stock QEMU
 * models no audio peripheral, so ALSA's "imx-pcm-audio hw params failed"
 * (the SDMA DMA layer) and the playhead stays frozen even while PLAY is latched.
 *
 * This is the "real-time null sink": we model *just enough* of the SDMA engine
 * (plus RAM-backed SSI/AUDMUX register files) that the dmaengine imx-sdma
 * driver probes, imx-pcm can configure/start a cyclic DMA channel, and a timer
 * then raises the per-channel "period elapsed" interrupt at the audio period
 * rate.  Each such interrupt makes ALSA advance the DMA pointer by one period,
 * which advances rbp's playhead -> REMAIN counts down.  No PCM data is moved
 * (silent); Option B would route it to a -audiodev sink.
 *
 * The whole handshake reduces to ONE mechanism, shared by probe and playback:
 *   "a channel is started (H_START bit n) -> raise its interrupt (H_INTR bit n
 *    + GIC line)".
 * Channel 0 is the SDMA command channel (context/script loads during probe and
 * channel config): it is one-shot, so we fire its completion interrupt once.
 * Channels >= 1 carry the audio cyclic DMA: we fire their interrupt repeatedly
 * at the period rate until the driver stops them (write to H_STATSTOP).
 *
 * Register offsets are the mainline drivers/dma/imx-sdma.c map (device
 * "imx-sdma", firmware "imx/sdma/sdma-imx6q-to1.bin"), which is what this
 * kernel's SDMA driver uses.
 * ========================================================================== */

#define SDMA_H_C0PTR      0x000  /* channel 0 control block pointer            */
#define SDMA_H_INTR       0x004  /* interrupt status, write-1-to-clear         */
#define SDMA_H_STATSTOP   0x008  /* R: running channels; W: stop channel(bit)  */
#define SDMA_H_START      0x00c  /* W: start channel (bit n)                   */
#define SDMA_H_EVTOVR     0x010
#define SDMA_H_DSPOVR     0x014
#define SDMA_H_HOSTOVR    0x018
#define SDMA_H_EVTPEND    0x01c
#define SDMA_H_RESET      0x024  /* W1 then self-clears (reads 0 = done)       */
#define SDMA_H_EVTERR     0x028
#define SDMA_H_INTRMASK   0x02c
#define SDMA_H_CONFIG     0x038
#define SDMA_NUM_CH       32

/*
 * SDMA channel control block (CCB) and buffer descriptor (BD) layout, as the
 * imx-sdma driver builds them in guest DRAM.  H_C0PTR holds the physical base
 * of the CCB array (one 16-byte CCB per channel); CCB.base_bd_ptr (+4) points
 * at that channel's BD array (12 bytes each).  The BD's first word packs
 * count[15:0] | status[23:16] | command[31:24]; the status byte carries the
 * ownership/flags we care about (shifted << 16 in the word).
 */
#define SDMA_CCB_STRIDE   16
#define SDMA_CCB_BASE_BD  4
#define SDMA_BD_STRIDE    12
#define SDMA_BD_DONE      (0x01u << 16)   /* 1 = owned by SDMA (armed) */
#define SDMA_BD_WRAP      (0x02u << 16)   /* last BD of a cyclic ring  */
#define SDMA_BD_MAX       64              /* ring-walk safety cap      */

/* S24_LE (in a 4-byte slot) stereo == 8 bytes/frame, same as the S32 stereo
 * output voice, so guest BD bytes and voice bytes share a 1:1 frame currency. */
#define SDMA_FRAME_BYTES  8
#define SDMA_FIFO_FRAMES  16384           /* master PCM FIFO capacity (~370 ms) */

typedef struct Imx6Sdma Imx6Sdma;
typedef struct {
    Imx6Sdma  *s;
    int        ch;
    uint32_t   bd_idx;      /* ring position for the cyclic BD walk */
    int64_t    period_ns;   /* this channel's real-time period interval (timer) */
    uint32_t   period_frames; /* BD.count / frame bytes (audio-clock mode)     */
    uint64_t   frames_done; /* frame_clock value at this channel's last period */
    QEMUTimer *timer;
} SdmaChan;

struct Imx6Sdma {
    MemoryRegion iomem;
    qemu_irq     irq;
    uint32_t     regs[FSL_IMX6_SDMA_SIZE / 4]; /* generic backing store       */
    uint32_t     intr;          /* H_INTR pending latch                        */
    uint32_t     running;       /* channels >=1 currently cyclic (H_STATSTOP)  */
    int64_t      period_ns;     /* fallback period interval (no BD info)       */
    uint32_t     byte_rate;     /* fallback drain rate, bytes/sec (timer mode) */
    /* Option B: callback-pull audio. When a voice opens, the host audio
     * backend's consumption drives period completion (the real audio clock),
     * so no QEMU timer and no byte_rate calibration are used. */
    AudioBackend *be;
    SWVoiceOut   *voice;        /* NULL => fall back to the timer clock        */
    bool          voice_on;     /* voice currently active                      */
    int           out_ch;       /* channel sonified to the voice (-1 = auto)   */
    uint64_t      frame_clock;  /* frames the output voice has consumed        */
    /* XDJ_AUDIO_STATS: estimated host-side cushion (frames written ahead of a
     * real-time 44.1k play-out clock) and underrun/starvation counters. */
    int64_t       st_t0, st_last_cb, st_win;
    double        st_play;      /* play-out position (frames), real-time paced */
    double        st_min_buf, st_gap;
    uint32_t      st_cbs, st_starve, st_under;
    int64_t       st_max_iv;
    uint32_t      st_late, st_min_fifo, st_ticks, st_resync;
    uint32_t      st_dbg;
    double        st_k;
    /* Paced period clock (voice mode): one period per tick, like the real SDMA
     * draining the SSI, rate-locked to the host audio clock via the FIFO level. */
    QemuThread    pace_thread;
    uint64_t      pace_clock;   /* frames of period time the pacer has issued  */
    /* Master PCM FIFO between the pacer (producer) and the host voice (consumer). */
    uint8_t       fifo[SDMA_FIFO_FRAMES * SDMA_FRAME_BYTES];
    uint32_t      f_rd, f_fill;  /* read index (frames), frames queued          */
    uint32_t      target;        /* FIFO fill the pacer steers to (frames)      */
    bool          primed;        /* FIFO reached target since the voice started */
    uint32_t      last_peak;
    SdmaChan     chan[SDMA_NUM_CH];
};

static Imx6Sdma g_sdma;

static void sdma_update_irq(Imx6Sdma *s)
{
    qemu_set_irq(s->irq, s->intr ? 1 : 0);
}

/*
 * Emulate the engine finishing one cyclic period: hand the current buffer
 * descriptor back to the CPU by clearing its BD_DONE bit, then advance the ring
 * tail (wrapping on BD_WRAP).  The driver's sdma_update_channel_loop() collects
 * exactly the BDs whose DONE bit is clear and re-arms them, so clearing one per
 * interrupt advances ALSA's pointer by exactly one period -- in lockstep with
 * the driver's own buf_tail.
 */
/* Physical base of channel ch's buffer-descriptor array (0 if not set up). */
static uint32_t sdma_bd_base(Imx6Sdma *s, int ch)
{
    uint32_t ccb = s->regs[SDMA_H_C0PTR >> 2];   /* CCB array base (phys) */

    if (!ccb) {
        return 0;
    }
    return address_space_ldl_le(&address_space_memory,
                                ccb + ch * SDMA_CCB_STRIDE + SDMA_CCB_BASE_BD,
                                MEMTXATTRS_UNSPECIFIED, NULL);
}

/*
 * Compute a cyclic channel's real-time period interval from the BD it was
 * programmed with: BD.count is the period size in bytes, and the device drains
 * byte_rate bytes/sec, so one period lasts count/byte_rate seconds.  Deriving
 * it from the BD (rather than a fixed rate) keeps the playhead real-time
 * regardless of the app's chosen period/buffer size.  Falls back to the global
 * default if the BD isn't readable yet.
 */
static void sdma_set_period(Imx6Sdma *s, int ch)
{
    uint32_t bd_base = sdma_bd_base(s, ch);
    uint32_t count;

    s->chan[ch].period_ns = s->period_ns;        /* default */
    if (!bd_base || !s->byte_rate) {
        return;
    }
    count = address_space_ldl_le(&address_space_memory, bd_base,
                                 MEMTXATTRS_UNSPECIFIED, NULL) & 0xffff;
    if (count) {
        s->chan[ch].period_ns =
            (int64_t)count * NANOSECONDS_PER_SECOND / s->byte_rate;
    }
    if (getenv("XDJ_AUDIO_DEBUG")) {
        fprintf(stderr, "[sdma] ch%d period: count=%u bytes byte_rate=%u "
                "period_ns=%lld\n", ch, count, s->byte_rate,
                (long long)s->chan[ch].period_ns);
    }
}

static void sdma_consume_period(Imx6Sdma *s, SdmaChan *c)
{
    uint32_t bd_base = sdma_bd_base(s, c->ch);
    uint32_t bd_addr, mode;

    if (!bd_base) {
        return;
    }
    bd_addr = bd_base + c->bd_idx * SDMA_BD_STRIDE;
    mode = address_space_ldl_le(&address_space_memory, bd_addr,
                                MEMTXATTRS_UNSPECIFIED, NULL);
    address_space_stl_le(&address_space_memory, bd_addr, mode & ~SDMA_BD_DONE,
                         MEMTXATTRS_UNSPECIFIED, NULL);

    if (getenv("XDJ_AUDIO_DEBUG")) {                 /* production-rate meter */
        static uint64_t fr[SDMA_NUM_CH];
        static int64_t t0;
        int64_t now = qemu_clock_get_ns(QEMU_CLOCK_REALTIME);
        if (c->ch < SDMA_NUM_CH) {
            fr[c->ch] += (mode & 0xffff) / SDMA_FRAME_BYTES;
        }
        if (!t0) t0 = now;
        if (now - t0 >= NANOSECONDS_PER_SECOND) {
            int q;
            for (q = 1; q < SDMA_NUM_CH; q++) {
                if (fr[q]) {
                    fprintf(stderr, "[sdma-rate] ch%d produced=%llu frames/s "
                            "(44100=realtime)\n", q, (unsigned long long)fr[q]);
                }
                fr[q] = 0;
            }
            t0 = now;
        }
    }

    if ((mode & SDMA_BD_WRAP) || ++c->bd_idx >= SDMA_BD_MAX) {
        c->bd_idx = 0;
    }
}

/* ---- Option B: real audio, paced like the hardware ---------------------------
 * A single S32/stereo/44.1k output voice is opened on the host audio backend.
 *
 * On the device the SDMA drains the SSI FIFO at the sample rate, so each cyclic
 * channel completes ONE period every period_frames/44100 s, evenly spaced. rbp
 * runs a tiny ALSA ring (2 x 64 frames = 2.9 ms), so the spacing matters: if two
 * periods complete back to back, ALSA's hw_ptr jumps a whole buffer, avail hits
 * the stop threshold and the guest logs "ALSA: Resolve xrun" (audible dropout).
 * The host audio callback cannot pace this itself -- it runs only every few ms
 * (measured ~345/s, i.e. 2 periods per call), which made every call an xrun.
 *
 * So a pacer completes at most one period per channel per tick,
 * and copies the master channel's period into a FIFO; the host callback just
 * drains the FIFO. The pacer (its own thread, see sdma_pace_thread) is trimmed (+/-8%) by the FIFO level, so
 * the guest's period clock stays locked to the host audio clock (the playhead
 * stays real-time) while the FIFO absorbs main-loop and backend jitter.
 *   XDJ_AUDIO_LATENCY_MS  FIFO target (default 20 ms)
 * If no voice can be opened, the SDMA model falls back to per-channel timers.
 */

/* The channel whose PCM is sent to the output voice (out_ch if running, else the
 * lowest running channel). */
static int sdma_master_ch(Imx6Sdma *s)
{
    int ch;

    if (s->out_ch > 0 && (s->running & (1u << s->out_ch))) {
        return s->out_ch;
    }
    for (ch = 1; ch < SDMA_NUM_CH; ch++) {
        if (s->running & (1u << ch)) {
            return ch;
        }
    }
    return -1;
}

/* XDJ_AUDIO_STATS=1: every 5 s print how close the host output came to running
 * dry. buf = frames written minus a real-time 44.1k play-out clock (what the host
 * still has queued); buf < 0 means the host played silence (underrun, gap ms). */
static void sdma_audio_stats(Imx6Sdma *s, bool starved)
{
    static int en = -1;
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_REALTIME);
    double buf;

    if (en < 0) {
        en = getenv("XDJ_AUDIO_STATS") != NULL;
    }
    if (!en) {
        return;
    }
    if (!s->st_t0) {
        s->st_t0 = s->st_last_cb = s->st_win = now;
        s->st_play = (double)s->frame_clock;
        s->st_min_buf = 1e9;
        s->st_min_fifo = UINT32_MAX;
    }
    if (now - s->st_last_cb > s->st_max_iv) {
        s->st_max_iv = now - s->st_last_cb;
    }
    s->st_play += (double)(now - s->st_last_cb) * 44100.0 / 1e9;
    s->st_last_cb = now;
    buf = (double)s->frame_clock - s->st_play;
    if (buf < 0) {
        s->st_under++;
        s->st_gap += -buf;
        s->st_play = (double)s->frame_clock;
        buf = 0;
    }
    if (buf < s->st_min_buf) {
        s->st_min_buf = buf;
    }
    if (s->f_fill < s->st_min_fifo) {
        s->st_min_fifo = s->f_fill;
    }
    s->st_cbs++;
    s->st_starve += starved;
    if (now - s->st_win >= 5 * NANOSECONDS_PER_SECOND) {
        fprintf(stderr, "[audio-stats] 5s: cbs=%u starved=%u underruns=%u gap=%.1fms "
                "minbuf=%.2fms buf=%.2fms max_cb_iv=%.2fms fifo=%.1fms "
                "minfifo=%.1fms guest_late=%u pace=%u/s resync=%u k=%.3f\n",
                s->st_cbs, s->st_starve, s->st_under, s->st_gap * 1000.0 / 44100.0,
                s->st_min_buf * 1000.0 / 44100.0, buf * 1000.0 / 44100.0,
                s->st_max_iv / 1e6, s->f_fill * 1000.0 / 44100.0,
                s->st_min_fifo * 1000.0 / 44100.0, s->st_late,
                s->st_ticks / 5, s->st_resync, s->st_k);
        s->st_win = now;
        s->st_cbs = s->st_starve = s->st_under = 0;
        s->st_gap = 0;
        s->st_min_buf = 1e9;
        s->st_max_iv = 0;
        s->st_late = 0;
        s->st_ticks = s->st_resync = 0;
        s->st_min_fifo = UINT32_MAX;
    }
}

/* Copy one master period (S24_LE in a 4-byte slot) into the FIFO as S32. */
static void sdma_fifo_push_period(Imx6Sdma *s, uint32_t bd_addr, uint32_t cnt)
{
    static uint8_t pcm[8192];
    uint32_t buf, frames, i, wr, peak = 0;

    if (cnt > sizeof(pcm)) {
        cnt = sizeof(pcm);
    }
    frames = cnt / SDMA_FRAME_BYTES;
    if (!frames) {
        return;
    }
    buf = address_space_ldl_le(&address_space_memory, bd_addr + 4,
                               MEMTXATTRS_UNSPECIFIED, NULL);
    address_space_read(&address_space_memory, buf, MEMTXATTRS_UNSPECIFIED,
                       pcm, frames * SDMA_FRAME_BYTES);
    for (i = 0; i + 4 <= frames * SDMA_FRAME_BYTES; i += 4) {  /* S24 -> S32 */
        uint32_t v = pcm[i] | (pcm[i + 1] << 8) | (pcm[i + 2] << 16) |
                     ((uint32_t)pcm[i + 3] << 24);
        int32_t o = (int32_t)(v << 8);
        uint32_t a = o < 0 ? (uint32_t)(-(int64_t)o) : (uint32_t)o;
        if (a > peak) {
            peak = a;
        }
        pcm[i]     = o & 0xff;
        pcm[i + 1] = (o >> 8) & 0xff;
        pcm[i + 2] = (o >> 16) & 0xff;
        pcm[i + 3] = (o >> 24) & 0xff;
    }
    s->last_peak = peak;
    if (s->f_fill + frames > SDMA_FIFO_FRAMES) {      /* host stalled: drop oldest */
        uint32_t drop = s->f_fill + frames - SDMA_FIFO_FRAMES;
        s->f_rd = (s->f_rd + drop) % SDMA_FIFO_FRAMES;
        s->f_fill -= drop;
    }
    wr = (s->f_rd + s->f_fill) % SDMA_FIFO_FRAMES;
    for (i = 0; i < frames; i++) {
        memcpy(&s->fifo[((wr + i) % SDMA_FIFO_FRAMES) * SDMA_FRAME_BYTES],
               &pcm[i * SDMA_FRAME_BYTES], SDMA_FRAME_BYTES);
    }
    s->f_fill += frames;
}

/* Nominal pacer interval (one master period), in ns. */
static int64_t sdma_pace_period_ns(Imx6Sdma *s)
{
    int master = sdma_master_ch(s);
    uint32_t p = (master > 0 && s->chan[master].period_frames) ?
                 s->chan[master].period_frames : 64;
    return (int64_t)p * NANOSECONDS_PER_SECOND / 44100;
}

/* One pacer step (BQL held): complete at most one period per running channel.
 * Returns the interval to the next step in ns, or 0 when audio is idle. */
static int64_t sdma_pace_step(Imx6Sdma *s)
{
    int master = sdma_master_ch(s);
    int64_t nominal;
    double k;
    bool any = false;
    int ch;

    if (!s->running || !s->voice_on) {
        return 0;
    }
    if (getenv("XDJ_AUDIO_DEBUG") && (s->st_dbg++ & 0x3ff) == 0) {
        /* per-channel peak over up to 4 frames of the BD about to complete */
        for (ch = 1; ch < SDMA_NUM_CH; ch++) {
            uint32_t pb, paddr, pmode, pcnt, pbuf, pk = 0, kk;
            uint8_t sb[4];
            if (!(s->running & (1u << ch))) {
                continue;
            }
            pb = sdma_bd_base(s, ch);
            if (!pb) {
                continue;
            }
            paddr = pb + s->chan[ch].bd_idx * SDMA_BD_STRIDE;
            pmode = address_space_ldl_le(&address_space_memory, paddr,
                                         MEMTXATTRS_UNSPECIFIED, NULL);
            pcnt = pmode & 0xffff;
            pbuf = address_space_ldl_le(&address_space_memory, paddr + 4,
                                        MEMTXATTRS_UNSPECIFIED, NULL);
            for (kk = 0; kk + 4 <= 32 && kk + 4 <= pcnt; kk += 4) {
                uint32_t v;
                int32_t sv;
                address_space_read(&address_space_memory, pbuf + kk,
                                   MEMTXATTRS_UNSPECIFIED, sb, 4);
                v = sb[0] | (sb[1] << 8) | (sb[2] << 16);
                if (v & 0x800000) {
                    v |= 0xff000000;
                }
                sv = (int32_t)v;
                if ((uint32_t)(sv < 0 ? -sv : sv) > pk) {
                    pk = (uint32_t)(sv < 0 ? -sv : sv);
                }
            }
            fprintf(stderr, "[sdma-ch] ch%d running cnt=%u done=%d bufpeak=%u\n",
                    ch, pcnt, (int)!!(pmode & SDMA_BD_DONE), pk);
        }
    }
    nominal = sdma_pace_period_ns(s);
    s->pace_clock += (master > 0 && s->chan[master].period_frames) ?
                     s->chan[master].period_frames : 64;

    for (ch = 1; ch < SDMA_NUM_CH; ch++) {
        SdmaChan *c = &s->chan[ch];
        uint32_t cbase, cmode, bd_addr;

        if (!(s->running & (1u << ch)) || !c->period_frames) {
            continue;
        }
        cbase = sdma_bd_base(s, ch);
        if (!cbase) {
            continue;
        }
        /* Never catch up in a burst: time the guest lost (late refill, timer
         * stall) is dropped, exactly as a real SSI would keep its own pace. */
        if (s->pace_clock - c->frames_done > 2 * (uint64_t)c->period_frames) {
            c->frames_done = s->pace_clock - c->period_frames;
        }
        while (s->pace_clock - c->frames_done >= c->period_frames) {
            bd_addr = cbase + c->bd_idx * SDMA_BD_STRIDE;
            cmode = address_space_ldl_le(&address_space_memory, bd_addr,
                                         MEMTXATTRS_UNSPECIFIED, NULL);
            if (!(cmode & SDMA_BD_DONE)) {
                if (ch == master) {
                    s->st_late++;        /* guest hasn't re-armed it yet */
                }
                break;
            }
            if (ch == master) {
                sdma_fifo_push_period(s, bd_addr, cmode & 0xffff);
            }
            sdma_consume_period(s, c);
            c->frames_done += c->period_frames;
            s->intr |= (1u << ch);
            any = true;
        }
    }
    if (any) {
        sdma_update_irq(s);
    }

    /* Rate lock: FIFO above target -> tick slower, below -> faster (<= 8%). */
    k = 1.0 + 0.25 * ((double)s->f_fill - s->target) / (s->target ? s->target : 1);
    if (k < 0.92) {
        k = 0.92;
    } else if (k > 1.08) {
        k = 1.08;
    }
    s->st_ticks++;
    s->st_k = k;
    return (int64_t)(nominal * k);
}

/* The pacer runs on its own thread. QEMU's main-loop timers can't fire every
 * 1.45 ms on macOS (no ppoll: the loop sleeps in whole-millisecond poll()
 * timeouts, measured ~2.5 ms per 1.45 ms tick = a 60% rate), so it sleeps to
 * absolute deadlines itself and takes the BQL only for the step. A wake-up more
 * than one period late resyncs instead of catching up -- catching up would
 * complete periods back to back, which is the xrun this exists to avoid. */
static void *sdma_pace_thread(void *opaque)
{
    Imx6Sdma *s = opaque;
    int64_t next = 0;

    rcu_register_thread();
#ifdef __APPLE__
    pthread_set_qos_class_self_np(QOS_CLASS_USER_INTERACTIVE, 0);
#endif
    for (;;) {
        int64_t now = qemu_clock_get_ns(QEMU_CLOCK_REALTIME), iv;

        if (next && now < next) {
            int64_t d = next - now;
            struct timespec ts = { d / NANOSECONDS_PER_SECOND,
                                   d % NANOSECONDS_PER_SECOND };
            nanosleep(&ts, NULL);
            continue;
        }
        bql_lock();
        iv = runstate_is_running() ? sdma_pace_step(s) : 0;
        bql_unlock();
        if (!iv) {
            next = 0;
            g_usleep(2000);              /* idle / paused */
            continue;
        }
        if (next && now - next >= iv) {
            s->st_resync++;              /* stats only (racy read is fine) */
        }
        next = (next && now - next < iv) ? next + iv : now + iv;
    }
    return NULL;
}

/* Host backend pull: drain the FIFO (after an initial pre-fill to target). */
static void sdma_audio_cb(void *opaque, int avail)
{
    Imx6Sdma *s = opaque;
    static int dbg;

    if (!s->primed) {
        if (s->f_fill < s->target) {
            sdma_audio_stats(s, false);
            return;
        }
        s->primed = true;
    }
    while (avail >= SDMA_FRAME_BYTES && s->f_fill) {
        uint32_t n = s->f_fill;
        size_t wrote;

        if (n > SDMA_FIFO_FRAMES - s->f_rd) {
            n = SDMA_FIFO_FRAMES - s->f_rd;      /* contiguous run */
        }
        if (n > (uint32_t)avail / SDMA_FRAME_BYTES) {
            n = (uint32_t)avail / SDMA_FRAME_BYTES;
        }
        wrote = audio_be_write(s->be, s->voice,
                               &s->fifo[s->f_rd * SDMA_FRAME_BYTES],
                               n * SDMA_FRAME_BYTES);
        wrote /= SDMA_FRAME_BYTES;
        if (!wrote) {
            break;                       /* backend buffer full */
        }
        s->f_rd = (s->f_rd + wrote) % SDMA_FIFO_FRAMES;
        s->f_fill -= wrote;
        avail -= (int)(wrote * SDMA_FRAME_BYTES);
        s->frame_clock += wrote;
        if (wrote < n) {
            break;
        }
    }
    if (getenv("XDJ_AUDIO_DEBUG") && (dbg++ & 0x1ff) == 0) {
        fprintf(stderr, "[sdma-au] master=%d avail=%d fifo=%u fc=%llu peak=%u/%u\n",
                sdma_master_ch(s), avail, s->f_fill,
                (unsigned long long)s->frame_clock, s->last_peak,
                (unsigned)0x7fffffff);
    }
    sdma_audio_stats(s, s->f_fill == 0 && avail >= SDMA_FRAME_BYTES);
}

/* Lazily open the output voice; returns false to use the timer fallback. */
static bool sdma_audio_ensure_voice(Imx6Sdma *s)
{
    struct audsettings as;

    if (s->voice) {
        return true;
    }
    if (getenv("XDJ_AUDIO_NOSOUND")) {
        return false;                    /* force the timer clock */
    }
    if (!s->be) {
        Error *err = NULL;
        s->be = audio_get_default_audio_be(&err);
        if (!s->be) {
            error_free(err);
            return false;
        }
    }
    as.freq = 44100;
    as.nchannels = 2;
    as.fmt = AUDIO_FORMAT_S32;
    as.big_endian = 0;
    s->voice = audio_be_open_out(s->be, NULL, "xdj.audio", s, sdma_audio_cb, &as);
    return s->voice != NULL;
}

static void sdma_audio_set_active(Imx6Sdma *s, bool on)
{
    if (!s->voice || s->voice_on == on) {
        return;
    }
    if (on) {
        s->f_rd = s->f_fill = 0;         /* fresh FIFO; pre-fill to target */
        s->primed = false;
    }
    audio_be_set_active_out(s->be, s->voice, on);
    s->voice_on = on;                    /* the pacer thread picks this up */
}

/* One period-elapsed / completion interrupt for one channel. */
static void sdma_ch_tick(void *opaque)
{
    SdmaChan *c = opaque;
    Imx6Sdma *s = c->s;

    /* Cyclic audio channel: finish a period in guest memory, keep the train
     * running.  Channel 0 (one-shot command channel) just signals completion. */
    if (s->running & (1u << c->ch)) {
        sdma_consume_period(s, c);
    }

    s->intr |= (1u << c->ch);
    sdma_update_irq(s);

    if (s->running & (1u << c->ch)) {
        timer_mod(c->timer,
                  qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + c->period_ns);
    }
}

static uint64_t sdma_read(void *opaque, hwaddr off, unsigned size)
{
    Imx6Sdma *s = opaque;

    switch (off) {
    case SDMA_H_INTR:
        return s->intr;
    case SDMA_H_STATSTOP:
        /* Channel 0 completes instantly (never "running"); audio channels run
         * until stopped.  The driver polls this for channel 0 readiness. */
        return s->running;
    case SDMA_H_RESET:
        return 0;               /* reset always already complete */
    case SDMA_H_EVTERR:
        return 0;
    default:
        return s->regs[(off >> 2) & (FSL_IMX6_SDMA_SIZE / 4 - 1)];
    }
}

static void sdma_write(void *opaque, hwaddr off, uint64_t val, unsigned size)
{
    Imx6Sdma *s = opaque;
    uint32_t v = (uint32_t)val;
    int ch;

    switch (off) {
    case SDMA_H_INTR:                           /* write-1-to-clear */
        s->intr &= ~v;
        sdma_update_irq(s);
        return;

    case SDMA_H_START:
        for (ch = 0; ch < SDMA_NUM_CH; ch++) {
            if (!(v & (1u << ch))) {
                continue;
            }
            if (ch == 0) {
                /* Command channel (context/script load): one-shot completion a
                 * hair later, so the driver's wait_for_completion is armed. */
                timer_mod(s->chan[0].timer,
                          qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + 1000);
            } else {
                /* Audio cyclic DMA. Preferred clock: the host audio backend
                 * pulls PCM at the real rate (Option B) and completes periods;
                 * fallback: a BD-paced QEMU timer (Option A). */
                uint32_t bd_base;
                s->running |= (1u << ch);
                s->chan[ch].bd_idx = 0;     /* start at the ring head */
                sdma_set_period(s, ch);     /* period_ns for the timer fallback */
                bd_base = sdma_bd_base(s, ch);
                s->chan[ch].period_frames = bd_base ?
                    (address_space_ldl_le(&address_space_memory, bd_base,
                        MEMTXATTRS_UNSPECIFIED, NULL) & 0xffff) / SDMA_FRAME_BYTES
                    : 0;
                s->chan[ch].frames_done = s->pace_clock;
                if (s->out_ch < 0) {
                    s->out_ch = ch;         /* sonify the first stream started */
                }
                if (sdma_audio_ensure_voice(s)) {
                    sdma_audio_set_active(s, true);
                } else {
                    timer_mod(s->chan[ch].timer,
                              qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL)
                                  + s->chan[ch].period_ns);
                }
            }
        }
        return;

    case SDMA_H_STATSTOP:                        /* write stops channel(s) */
        for (ch = 1; ch < SDMA_NUM_CH; ch++) {
            if (v & (1u << ch)) {
                s->running &= ~(1u << ch);
                timer_del(s->chan[ch].timer);
            }
        }
        if (!s->running) {
            sdma_audio_set_active(s, false);    /* idle the output voice */
        }
        return;

    case SDMA_H_RESET:                           /* self-clearing */
        return;

    default:
        s->regs[(off >> 2) & (FSL_IMX6_SDMA_SIZE / 4 - 1)] = v;
        return;
    }
}

static const MemoryRegionOps sdma_ops = {
    .read = sdma_read,
    .write = sdma_write,
    .endianness = DEVICE_NATIVE_ENDIAN,
    .valid = { .min_access_size = 4, .max_access_size = 4 },
};

/*
 * SSI (imx-ssi) register file: RAM-backed so every config write the ASoC
 * cpu-dai makes is retained and reads back, with SISR reporting the TX FIFO
 * empty so nothing that polls it spins.  No audio behaviour is needed here --
 * the time base comes entirely from the SDMA period interrupts above.
 */
#define SSI_SISR  0x14   /* status: TFE0|TFE1 = TX FIFO(s) empty */
#define SSI_REGS  (0x100 / 4)

typedef struct {
    MemoryRegion mr;
    uint32_t     regs[SSI_REGS];
} Imx6Ssi;

static Imx6Ssi g_ssi[3];

static uint64_t ssi_read(void *opaque, hwaddr off, unsigned size)
{
    Imx6Ssi *s = opaque;

    if (off == SSI_SISR) {
        return 0x00000003;      /* TFE0 | TFE1: TX FIFOs always ready */
    }
    if ((off >> 2) < SSI_REGS) {
        return s->regs[off >> 2];
    }
    return 0;
}

static void ssi_write(void *opaque, hwaddr off, uint64_t val, unsigned size)
{
    Imx6Ssi *s = opaque;

    if ((off >> 2) < SSI_REGS) {
        s->regs[off >> 2] = (uint32_t)val;
    }
}

static const MemoryRegionOps ssi_ops = {
    .read = ssi_read,
    .write = ssi_write,
    .endianness = DEVICE_NATIVE_ENDIAN,
    .valid = { .min_access_size = 4, .max_access_size = 4 },
};

/* realize: SDMA engine (+ pacer thread), SSI1-3 and AUDMUX register files */
void xdj_audio_realize(DeviceState *dev, DeviceState *gic)
{
    /*
     * Audio time base (Option A): SDMA engine model + RAM-backed SSI/AUDMUX.
     * Makes the ASoC/ALSA path configure and start a cyclic DMA channel; the
     * SDMA period interrupt then advances ALSA's pointer -> rbp's REMAIN counts
     * down.  See the Imx6Sdma block above for the mechanism.
     */
    {
        int ch;
        const char *e = getenv("XDJ_AUDIO_PERIOD_HZ");
        const char *br = getenv("XDJ_AUDIO_BYTE_RATE");
        int hz = e ? atoi(e) : 86;      /* fallback pacing if no BD info */

        if (hz < 1) {
            hz = 86;
        }
        g_sdma.period_ns = NANOSECONDS_PER_SECOND / hz;
        /* Device drain rate used to pace period interrupts: period_ns =
         * BD.count / byte_rate.  The ideal 44.1 kHz S24_LE stereo rate is
         * 352800 B/s, but the guest's clocksource runs ~8.6% slow relative to
         * QEMU_CLOCK_VIRTUAL (a constant TCG calibration gap), so the guest
         * would see the playhead ~10% fast at that value.  Measured on the real
         * hw:ak4387audio device: 318000 B/s yields ~44100 frames/s (the drain
         * rate scales linearly with byte_rate); env-tunable for fine-tuning. */
        g_sdma.byte_rate = br ? (uint32_t)strtoul(br, NULL, 0) : 318000;
        if (!g_sdma.byte_rate) {
            g_sdma.byte_rate = 318000;
        }
        g_sdma.irq = qdev_get_gpio_in(gic, FSL_IMX6_SDMA_IRQ);
        {   /* Option B: which SDMA channel is sonified to the output voice.
             * -1 = auto (first stream started); XDJ_AUDIO_OUT_CH pins it. */
            const char *oc = getenv("XDJ_AUDIO_OUT_CH");
            g_sdma.out_ch = oc ? atoi(oc) : 1;  /* default ch1 = MasterOut */
        }
        {   /* paced period clock + master FIFO (Option B) */
            const char *lm = getenv("XDJ_AUDIO_LATENCY_MS");
            int ms = lm ? atoi(lm) : 20;
            if (ms < 3) {
                ms = 3;
            } else if (ms > 300) {
                ms = 300;
            }
            g_sdma.target = (uint32_t)(44100 * ms / 1000);
            qemu_thread_create(&g_sdma.pace_thread, "xdj-sdma-pace",
                               sdma_pace_thread, &g_sdma, QEMU_THREAD_DETACHED);
        }
        for (ch = 0; ch < SDMA_NUM_CH; ch++) {
            g_sdma.chan[ch].s = &g_sdma;
            g_sdma.chan[ch].ch = ch;
            g_sdma.chan[ch].timer =
                timer_new_ns(QEMU_CLOCK_VIRTUAL, sdma_ch_tick, &g_sdma.chan[ch]);
        }
        memory_region_init_io(&g_sdma.iomem, OBJECT(dev), &sdma_ops, &g_sdma,
                              "imx6.sdma", FSL_IMX6_SDMA_SIZE);
        memory_region_add_subregion(get_system_memory(), FSL_IMX6_SDMA_ADDR,
                                    &g_sdma.iomem);

        /* SSI1/2/3 register files (cpu-dai config scratch; no behaviour) */
        memory_region_init_io(&g_ssi[0].mr, OBJECT(dev), &ssi_ops, &g_ssi[0],
                              "imx6.ssi1", FSL_IMX6_SSI1_SIZE);
        memory_region_add_subregion(get_system_memory(), FSL_IMX6_SSI1_ADDR,
                                    &g_ssi[0].mr);
        memory_region_init_io(&g_ssi[1].mr, OBJECT(dev), &ssi_ops, &g_ssi[1],
                              "imx6.ssi2", FSL_IMX6_SSI2_SIZE);
        memory_region_add_subregion(get_system_memory(), FSL_IMX6_SSI2_ADDR,
                                    &g_ssi[1].mr);
        memory_region_init_io(&g_ssi[2].mr, OBJECT(dev), &ssi_ops, &g_ssi[2],
                              "imx6.ssi3", FSL_IMX6_SSI3_SIZE);
        memory_region_add_subregion(get_system_memory(), FSL_IMX6_SSI3_ADDR,
                                    &g_ssi[2].mr);

        /* AUDMUX: dumb RAM-backed register file (routing, no behaviour) */
        {
            static MemoryRegion audmux;
            memory_region_init_ram(&audmux, OBJECT(dev), "imx6.audmux",
                                   FSL_IMX6_AUDMUX_SIZE, &error_abort);
            memory_region_add_subregion(get_system_memory(),
                                        FSL_IMX6_AUDMUX_ADDR, &audmux);
        }
    }


}
