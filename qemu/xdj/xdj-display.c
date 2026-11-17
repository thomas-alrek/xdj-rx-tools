/*
 * Pioneer XDJ-RX emulation -- display path: Vivante GPU / IPU / thermal stubs and the framebuffer scanout
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
#include "qapi/visitor.h"
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

/*
 * Minimal Vivante GPU (GC2000 3D / GC320 2D) register stub.
 *
 * QEMU does not model the i.MX6 GPU.  The vendor "galcore" driver, after
 * resetting the GPU via SRC, reads these registers to identify the core and
 * polls the idle / interrupt-acknowledge registers.  With no device mapped the
 * reads return 0 and the driver blocks forever.  We return values that report
 * "idle, all events acknowledged, a GC2000 rev 0x5108" so the driver's init
 * completes and the kernel boots on to userspace.  No real GPU work happens.
 */
/*
 * Each Vivante core (3D @0x130000, 2D @0x134000) has its own identity and IRQ.
 * galcore reads chipModel/chipRevision to derive the core's gceHARDWARE_TYPE;
 * the 2D register slot MUST identify as a 2D part (GC320) or gckGALDEVICE_Construct
 * rejects it with -EINVAL (the "probe of galcore.0 failed with error -22").
 */
typedef struct XdjGpuCore {
    qemu_irq irq;       /* this core's GIC line (for faking command completion) */
    uint32_t model;     /* chipModel    (0x020): GC2000=0x2000, GC320=0x0320     */
    uint32_t revision;  /* chipRevision (0x024)                                  */
    uint32_t features;  /* chipFeatures (0x01c)                                  */
    uint32_t minor_features; /* chipMinorFeatures0..3 (0x034/0x074/0x084/0x088): */
                        /* galcore decodes these (no chip DB in 4.6.9) into the   */
                        /* feature set gcoHAL_IsFeatureAvailable reports to        */
                        /* userspace.  The DirectFB GAL gfxdriver + libGAL gco2D   */
                        /* blit path is feature-gated, so a 2D core advertising no */
                        /* features makes rbp's dynamic blit take a degraded path  */
                        /* and call through a garbage gco2D pointer (SIGSEGV).     */
    uint32_t events;    /* AQIntrAcknowledge (0x010) bitmap to report once a      */
                        /* command is kicked (the completion event galcore waits  */
                        /* on); 0 otherwise so its pre-command status poll is clean*/
    bool cmd_pending;   /* a command was kicked: report c->events on next 0x010 rd */
    uint32_t clkctrl;   /* last AQHiClockControl written                         */
    uint32_t fe_addr;   /* last AQFEStartAddress (0x654): the command buffer the  */
                        /* FE will fetch (physical; galcore runs the FE MMU off,  */
                        /* so it is directly readable guest memory).  Used to     */
                        /* parse the command's EVENT id (see xdj_gpu_cmd_event).  */
    QEMUTimer *fe_timer;/* if set, raise the completion IRQ a short delay after  */
                        /* the command kick (so galcore sees it once it is in    */
                        /* its GIC poll loop with the IRQ enabled), instead of    */
                        /* raising synchronously on the register write.           */
    qemu_irq sibling_irq;/* the other GPU core's GIC line; when set, completion  */
                        /* is delivered (and acked) on BOTH lines, so galcore's   */
                        /* GICC_HPPIR poll is satisfied whether it enabled the    */
                        /* per-core 2D line or a unified GPU completion IRQ.       */
} XdjGpuCore;

/*
 * Models "the GPU finished the submitted command and raised its completion
 * interrupt".  galcore's probe submits a sync command then, with CPU IRQs
 * masked, busy-polls the GIC CPU interface (GICC_HPPIR) for the GPU IRQ to
 * become pending.  A synchronous raise on the kick write can land before
 * galcore has enabled/EOI'd the line (lost edge); firing from a timer delivers
 * the completion once galcore is actually waiting for it.
 */
static void imx6_gpu_fe_fire(void *opaque)
{
    XdjGpuCore *c = opaque;
    if (c->irq) {
        qemu_set_irq(c->irq, 1);
    }
    if (c->sibling_irq) {
        qemu_set_irq(c->sibling_irq, 1);
    }
}

/*
 * XDJ-RX bus-frequency DVFS settling-wait unblock (GPT tick).
 *
 * When the full-stock galcore path (the default) enables the GPU, the kernel's
 * bus-frequency DVFS does an ARM/DDR clock change with CPU interrupts masked and
 * then waits for the next timer tick as a settling delay (single-core busy-polls
 * GICC_HPPIR, SMP does WFI).  The GPT one-shot clockevent is not reprogrammed
 * inside that masked window, so under emulation no tick arrives and the DVFS
 * hangs.  The CCM model calls xdj_dvfs_gpt_kick() on the DVFS clock-step writes
 * to raise the GPT's completion interrupt (GIC ID 87), which ends the poll.
 * Armed (pointer != NULL) on the full-galcore path (the default); a no-op when
 * that path is disabled (XDJ_GPU2D_OFF=1).
 */
static IMXGPTState *xdj_dvfs_gpt;

void xdj_dvfs_gpt_kick(void)
{
    if (xdj_dvfs_gpt) {
        imx_gpt_kick(xdj_dvfs_gpt);
    }
}

static uint64_t imx6_gpu_stub_read(void *opaque, hwaddr off, unsigned size)
{
    XdjGpuCore *c = opaque;

    if (getenv("IMX6_GPU_TRACE")) {
        fprintf(stderr, "GPUSTUB[%04x] rd off=0x%03x\n",
                (unsigned)c->model, (unsigned)off);
    }
    switch (off) {
    case 0x000:                     /* AQHiClockControl: echo back the last    */
        /* value written, with the two "clock running" status bits (16,17) set */
        /* so _ResetGPU()'s completion check passes.                           */
        return c->clkctrl | 0x00030000u;
    case 0x004: return 0xffffffffu; /* AQHiIdle          - every module idle  */
    case 0x010: {                   /* AQIntrAcknowledge                       */
        /* Report the submitted command's completion event only after a kick,
         * and only that event -- signalling events galcore never queued makes
         * its event handler dereference unallocated event structs (NULL oops in
         * _EventHandler_Block), and setting the error bits (31=AXI, 30=MMU) on a
         * pre-command status poll makes it log "AXI BUS ERROR"/"MMU Exception!".
         * Reading acknowledges: drop the line and clear the pending flag. */
        uint32_t v = c->cmd_pending ? c->events : 0u;
        c->cmd_pending = false;
        if (c->irq) {
            qemu_set_irq(c->irq, 0);
        }
        if (c->sibling_irq) {
            qemu_set_irq(c->sibling_irq, 0);
        }
        return v;
    }
    case 0x018: return 0x00000000u; /* chipIdentity      - take the else path  */
    case 0x01c: return c->features; /* chipFeatures                            */
    case 0x020: return c->model;    /* chipModel                               */
    case 0x024: return c->revision; /* chipRevision                            */
    case 0x034:                     /* chipMinorFeatures0                      */
    case 0x074:                     /* chipMinorFeatures1                      */
    case 0x084:                     /* chipMinorFeatures2                      */
    case 0x088:                     /* chipMinorFeatures3  (bitfields, not     */
        return c->minor_features;   /* numeric specs -- safe to advertise)     */
    default:    return 0x00000000u;
    }
}

/*
 * Parse the GPU command buffer the FE is about to fetch to find which completion
 * event this command signals, so we can raise exactly that event (not a fixed
 * mask).  galcore ends every submitted command with an EVENT command: a
 * LOAD_STATE of one word to the AQEventRegister (0x0E01) whose value is
 * (eventId | source).  Runtime 2D blits allocate a *rotating* event id per
 * commit and then wait for that specific id; a fixed mask leaves the commit's
 * stall waiting forever (the deck-UI stall).  Init commands use a fixed id and
 * worked with the mask -- parsing covers both.
 *
 * The FE command word format here is opcode[31:27]=6 (LOAD_STATE), addr[15:0];
 * so the EVENT command word is (6<<27)|...|0x0E01, i.e. (w>>27)==6 &&
 * (w&0xffff)==0x0E01, and the next word is the value.  AQFEControl (0x658) low
 * 16 bits are the prefetch count in 64-bit units.  Returns the event bitmask
 * (1<<id), or 0 if no EVENT command is found (caller keeps its default mask).
 */
static uint32_t xdj_gpu_cmd_event(uint32_t fe_addr, uint32_t ctrl)
{
    uint32_t words = (ctrl & 0xffffu) * 2u;   /* 64-bit units -> 32-bit words */

    if (fe_addr == 0) {
        return 0;
    }
    if (words == 0 || words > 1024) {
        words = 1024;                          /* cap the scan */
    }
    for (uint32_t i = 0; i + 1 < words; i++) {
        uint32_t w = address_space_ldl_le(&address_space_memory, fe_addr + i * 4u,
                                          MEMTXATTRS_UNSPECIFIED, NULL);
        if ((w >> 27) == 6u && (w & 0xffffu) == 0x0e01u) {
            uint32_t v = address_space_ldl_le(&address_space_memory,
                                              fe_addr + (i + 1) * 4u,
                                              MEMTXATTRS_UNSPECIFIED, NULL);
            return 1u << (v & 0x1fu);
        }
    }
    return 0;
}

static void imx6_gpu_stub_write(void *opaque, hwaddr off, uint64_t val,
                                unsigned size)
{
    XdjGpuCore *c = opaque;

    if (getenv("IMX6_GPU_TRACE")) {
        fprintf(stderr, "GPUSTUB[%04x] wr off=0x%03x val=0x%08x\n",
                (unsigned)c->model, (unsigned)off, (unsigned)val);
    }
    if (off == 0x000) {
        c->clkctrl = (uint32_t)val;
    }
    if (off == 0x654) {
        c->fe_addr = (uint32_t)val;            /* command buffer the FE will fetch */
    }
    /*
     * galcore submits a command and sleeps until the GPU raises its completion
     * IRQ.  No GPU runs here, so raise it ourselves on the command kick -- the
     * 3D/2D front-end control (AQFEControl, 0x658) and the VG DMA control
     * (0x504).  (NOT on the interrupt-enable write 0x014: that would fire before
     * any command/event exists and the handler would process a phantom event.)
     * The ISR then reads 0x010 and sees the completion event (c->events).
     */
    if (c->irq && (off == 0x658 || off == 0x504)) {
        if (off == 0x658) {
            /* Report the exact event this command queued (rotating per commit at
             * runtime) instead of the fixed mask -- otherwise the blit's
             * completion stall never releases.  Keep the default mask if the
             * command carries no EVENT command. */
            uint32_t ev = xdj_gpu_cmd_event(c->fe_addr, (uint32_t)val);
            if (ev) {
                c->events = ev;
            }
        }
        c->cmd_pending = true;
        if (c->fe_timer) {
            /* deliver the completion ~200us later, once galcore is waiting */
            timer_mod(c->fe_timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) +
                                   NANOSECONDS_PER_SECOND / 5000);
        } else {
            qemu_set_irq(c->irq, 1); /* default path: synchronous (unchanged) */
        }
    }
}

static const MemoryRegionOps imx6_gpu_stub_ops = {
    .read = imx6_gpu_stub_read,
    .write = imx6_gpu_stub_write,
    .endianness = DEVICE_NATIVE_ENDIAN,
    .valid = { .min_access_size = 4, .max_access_size = 4 },
};

/*
 * i.MX6 anatop temperature-sensor (TEMPSENSE0) stub.
 *
 * The anatop thermal driver measures die temperature by triggering a
 * conversion and polling HW_ANADIG_TEMPSENSE0 (ANALOG base + 0x180) for the
 * FINISHED bit (bit 2).  QEMU models no temperature sensor, so that bit never
 * sets: anatop_thermal_get_temp() spins msleep(10) up to ~500 jiffies (~5 s),
 * prints "Thermal sensor timeout, retry!", and the thermal zone repeats it
 * forever -- pegging a core and (under TCG) stealing ~half the emulation
 * throughput from the app on the other core.  Report the conversion as always
 * finished (bit 2 set) with a plausible count, so every read returns at once
 * and the thermal poll stops burning CPU.  Offsets 0x4/0x8/0xc are the i.MX6
 * SET/CLR/TOG aliases; writes (MEASURE_TEMP, clear-FINISHED) are no-ops here.
 */
static uint64_t imx6_tempsense_stub_read(void *opaque, hwaddr off, unsigned size)
{
    if (off == 0x0) {
        /* bit 2 = FINISHED; bits 8..19 = TEMP_VALUE (a mid-range raw count) */
        return (1u << 2) | (0x580u << 8);
    }
    return 0;
}

static void imx6_tempsense_stub_write(void *opaque, hwaddr off, uint64_t val,
                                      unsigned size)
{
}

static const MemoryRegionOps imx6_tempsense_stub_ops = {
    .read = imx6_tempsense_stub_read,
    .write = imx6_tempsense_stub_write,
    .endianness = DEVICE_NATIVE_ENDIAN,
    .valid = { .min_access_size = 4, .max_access_size = 4 },
};

/*
 * i.MX6 IPU1 interrupt stub.
 *
 * QEMU models no IPU, so the mxc_sdc_fb display path never receives the
 * vsync / end-of-frame interrupts it waits on.  The driver then falls back to
 * a timeout on every DirectFB page-flip (DSFLIP_WAITFORSYNC) and on display
 * enable, which makes the UI crawl and stalls boot (~80s).  We model just the
 * IPU Control Module interrupt registers (INT_CTRL/INT_STAT) plus a refresh timer
 * that asserts whatever interrupts the driver has enabled and pulses the IPU1
 * sync IRQ.  The kernel's chained ipu_sync_irq_handler demuxes and completes
 * the fb waits, exactly as the panel's real ~60 Hz end of frame would.
 *
 * Register layout recovered from the firmware kernel's ipu_clear_irq /
 * ipu_enable_irq: CM block at IPU1 base + 0x200000; INT_CTRL_n at 0x3C+4*(n-1),
 * INT_STAT_n at 0x200+4*(n-1), n=1..15; INT_STAT is write-1-to-clear.  The
 * "sync" group (serviced by ipu_sync_irq_handler -> IPU1_SYNC_IRQ) is registers
 * 1-4 and 11-15; the error group (5-10) uses a separate line we leave idle.
 */
#define IPU1_CM_BASE      0x02600000u
#define IPU_CM_SIZE       0x1000
#define IPU_INT_CTRL_BASE 0x003c
#define IPU_INT_STAT_BASE 0x0200
#define IPU_NUM_INT_REG   15

static uint32_t ipu_int_ctrl[IPU_NUM_INT_REG];
static uint32_t ipu_int_stat[IPU_NUM_INT_REG];
/*
 * Bits the driver has itself cleared (write-1-to-clear) at least once.  We only
 * ever synthesize an interrupt that the driver has actively managed: a bit it
 * has cleared has had its completion/wait-queue initialized, so completing it
 * is safe.  This gates out the probe-time race where an interrupt is enabled
 * before its handler's data structures exist (which panics in complete()).
 */
static uint32_t ipu_int_seen[IPU_NUM_INT_REG];
static qemu_irq ipu_sync_irq_line;
static QEMUTimer *ipu_irq_timer;

/*
 * The panel's refresh.  The kernel sets the LCD up from the command line
 * "video=mxcfb0:dev=ldb,800x480M@60" - a CVT mode (fbcvt): pixel clock
 * 29.5 MHz, 992 x 500 total (480 visible lines + 3 front porch + 17 sync and
 * back porch), so a line is 33.63 us and a refresh 16.81 ms (59.48 Hz; a real
 * unit measures 16.80 ms).  The refresh free-runs from panel_epoch: the IDMAC
 * end-of-frame (EOF) of refresh k comes at panel_epoch + k * PANEL_FRAME_NS,
 * when the last visible line has been read; the next readout starts 20 lines
 * later.  The EOF interrupt and the raster readout (xdj_raster_*) both follow
 * this clock, as the hardware's do.
 */
#define PANEL_PIXCLK_HZ  29500000ull
#define PANEL_HTOTAL     992
#define PANEL_VTOTAL     500
#define PANEL_VACTIVE    480
#define PANEL_LINE_NS    ((int64_t)(PANEL_HTOTAL * 1000000000ull / PANEL_PIXCLK_HZ))
#define PANEL_FRAME_NS   (PANEL_LINE_NS * PANEL_VTOTAL)
static int64_t panel_epoch;

/* The first EOF strictly after `now` (QEMU_CLOCK_VIRTUAL ns). */
static int64_t panel_next_eof(int64_t now)
{
    int64_t k = (now - panel_epoch) / PANEL_FRAME_NS + 1;
    return panel_epoch + k * PANEL_FRAME_NS;
}

/*
 * We synthesize interrupts for only the primary BG display channel (IDMAC
 * ch23): EOF in INT_STAT_1 and NFACK in INT_STAT_3, both bit 23 (recovered
 * from the driver's own INT_CTRL writes - see the IPU_TRACE output).  That is
 * all the mxc_sdc_fb display-enable and DSFLIP_WAITFORSYNC paths wait on.  We
 * deliberately do NOT drive the FG/alpha channel (bit 27) or any other channel:
 * their handlers (e.g. mxcfb_alpha_irq_handler) call complete() on completions
 * that aren't initialized yet during probe, which panics the kernel.
 */
#define IPU_BG_CH_BIT    0x00800000u          /* IDMAC channel 23 */
static const int ipu_assert_reg[] = { 0, 2 }; /* INT_STAT_1 (EOF), _3 (NFACK) */

static void ipu_sync_update_irq(void)
{
    uint32_t pend = 0;
    unsigned i;
    for (i = 0; i < ARRAY_SIZE(ipu_assert_reg); i++) {
        int r = ipu_assert_reg[i];
        pend |= ipu_int_stat[r] & ipu_int_ctrl[r] & IPU_BG_CH_BIT;
    }
    qemu_set_irq(ipu_sync_irq_line, pend ? 1 : 0);
}

/*
 * One interrupt delivery, ~1 frame after the driver armed it.  Real hardware
 * raises the sync/EOF IRQ once per frame after the channel is armed; we model
 * exactly that (edge-triggered), rather than a free-running stream which
 * livelocks the driver's setup.  Armed from register writes (see ipu_cm_write).
 */
/* schedule the interrupt delivery at the panel's next end of frame */
static void ipu_irq_arm(void)
{
    timer_mod(ipu_irq_timer, panel_next_eof(qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL)));
}

static void ipu_irq_fire(void *opaque)
{
    unsigned i;
    bool enabled = false;
    for (i = 0; i < ARRAY_SIZE(ipu_assert_reg); i++) {
        int r = ipu_assert_reg[i];
        /*
         * Real hardware raises the BG display channel's sync/EOF interrupt once
         * per frame for as long as that interrupt is ENABLED, independent of
         * whether anything is currently waiting on it.  Model exactly that:
         * assert the BG status bit whenever the driver has the BG interrupt
         * enabled in INT_CTRL.  (|= is idempotent while a previous one is still
         * pending, like the real level/edge status latch.)
         */
        if (ipu_int_ctrl[r] & IPU_BG_CH_BIT) {
            ipu_int_stat[r] |= IPU_BG_CH_BIT;
            enabled = true;
        }
    }
    ipu_sync_update_irq();
    /*
     * Free-run at the panel's refresh while the BG channel interrupt stays enabled, so vsync
     * delivery is self-driving.  Without this a new IRQ was only scheduled when
     * the driver wrote an IPU register (ipu_cm_write), so the very first
     * display-enable wait and any DirectFB DSFLIP_WAITFORSYNC page-flip that
     * waits on an already-enabled interrupt could miss their edge and stall --
     * which froze the UI and left display bring-up dependent on the host
     * display's refresh cadence (worked under a live window, hung/crashed
     * headless).  Delivery stops as soon as the driver disables the BG
     * interrupt (INT_CTRL BG bit cleared), so this cannot run forever.
     */
    if (enabled) {
        ipu_irq_arm();
    }
}

static uint64_t ipu_cm_read(void *opaque, hwaddr off, unsigned size)
{
    if (off >= IPU_INT_CTRL_BASE &&
        off < IPU_INT_CTRL_BASE + 4 * IPU_NUM_INT_REG) {
        return ipu_int_ctrl[(off - IPU_INT_CTRL_BASE) >> 2];
    }
    if (off >= IPU_INT_STAT_BASE &&
        off < IPU_INT_STAT_BASE + 4 * IPU_NUM_INT_REG) {
        return ipu_int_stat[(off - IPU_INT_STAT_BASE) >> 2];
    }
    return 0;
}

static void ipu_cm_write(void *opaque, hwaddr off, uint64_t val, unsigned size)
{
    if (off >= IPU_INT_CTRL_BASE &&
        off < IPU_INT_CTRL_BASE + 4 * IPU_NUM_INT_REG) {
        if (getenv("IPU_TRACE")) {
            fprintf(stderr, "IPU INT_CTRL_%u = 0x%08x\n",
                    (unsigned)((off - IPU_INT_CTRL_BASE) >> 2) + 1, (unsigned)val);
        }
        ipu_int_ctrl[(off - IPU_INT_CTRL_BASE) >> 2] = (uint32_t)val;
        ipu_sync_update_irq();
        ipu_irq_arm();    /* driver (re)armed an interrupt: deliver one frame */
    } else if (off >= IPU_INT_STAT_BASE &&
               off < IPU_INT_STAT_BASE + 4 * IPU_NUM_INT_REG) {
        unsigned idx = (off - IPU_INT_STAT_BASE) >> 2;
        ipu_int_stat[idx] &= ~(uint32_t)val;    /* W1C */
        ipu_int_seen[idx] |= (uint32_t)val;     /* driver is actively cycling it */
        ipu_sync_update_irq();
        ipu_irq_arm();    /* driver cleared before a new wait: deliver one frame */
    }
}

static const MemoryRegionOps ipu_cm_ops = {
    .read = ipu_cm_read,
    .write = ipu_cm_write,
    .endianness = DEVICE_NATIVE_ENDIAN,
    .valid = { .min_access_size = 4, .max_access_size = 4 },
};

/*
 * XDJ-RX framebuffer scanout.
 *
 * QEMU does not model the i.MX6 IPU display path, but the vendor mxc_sdc_fb
 * driver still allocates and configures the framebuffer (480x800 ARGB8888) in
 * DRAM at a fixed address, and userspace draws into it. We register a graphic
 * console that scans that memory out to a window / screendump. This is a
 * read-only mirror of the framebuffer; no IPU registers are driven.
 */
#define XDJ_FB_BASE    0x18000000
#define XDJ_FB_WIDTH   800
#define XDJ_FB_HEIGHT  480

/*
 * IPU1 CPMEM (channel parameter memory) capture.
 *
 * The framebuffer pixel depth changes at runtime: the boot splash is drawn at
 * 32bpp, then DirectFB (the app) reprograms the display to 16bpp RGB565 for the
 * UI.  The mxc_sdc_fb driver records the active geometry in the IPU's CPMEM for
 * the display IDMAC channel (23, "MEM_BG_SYNC").  QEMU does not model the IPU,
 * so we back CPMEM with RAM purely to capture those writes and read back the
 * current line stride -- 3200 bytes => 32bpp, 1600 => 16bpp -- so the scanout
 * decodes whichever mode is live.  CPMEM per-channel is two 32-byte words; the
 * SLY (stride-1) field is word1, bit 102, 14 bits (-> data[3] bits 6..19); the
 * EBA0 buffer address is word1, bits 0..28 (phys/8).
 */
#define IPU1_CPMEM_BASE  0x02700000u
#define IPU1_CPMEM_SIZE  0x20000u
#define IPU_DISP_CH      23
#define IPU_CH_PARAM(ch) ((ch) * 0x40u)        /* sizeof(ipu_ch_param) */
#define IPU_CH_W1_D0(ch) (IPU_CH_PARAM(ch) + 0x20u + 0)   /* word1.data[0] */
#define IPU_CH_W1_D3(ch) (IPU_CH_PARAM(ch) + 0x20u + 12)  /* word1.data[3] */

static QemuConsole *xdj_fb_con;
static uint8_t *ipu_cpmem_ram;

/* Return the live framebuffer bit depth (16 or 32) read from CPMEM, or 0 if the
 * driver has not programmed a recognisable stride yet. */
static int xdj_fb_detect_bpp(void)
{
    uint32_t w1d3, w1d0, stride, eba;

    if (!ipu_cpmem_ram) {
        return 0;
    }
    w1d3 = ldl_le_p(ipu_cpmem_ram + IPU_CH_W1_D3(IPU_DISP_CH));
    w1d0 = ldl_le_p(ipu_cpmem_ram + IPU_CH_W1_D0(IPU_DISP_CH));
    stride = ((w1d3 >> 6) & 0x3FFFu) + 1;      /* SLY: bytes per line */
    eba = (w1d0 & 0x1FFFFFFFu) << 3;           /* EBA0: buffer phys addr */
    if (getenv("XDJ_FB_TRACE")) {
        static uint32_t last;
        if (stride != last) {
            fprintf(stderr, "XDJ_FB: ch%d stride=%u eba=0x%08x -> %dbpp\n",
                    IPU_DISP_CH, stride, eba, stride >= 2400 ? 32 : 16);
            last = stride;
        }
    }
    if (stride >= 1400 && stride <= 1800) {
        return 16;
    }
    if (stride >= 3000 && stride <= 3400) {
        return 32;
    }
    return 0;
}

/*
 * Read framebuffer rows [y0, y0 + n) from guest memory and convert them to
 * x8r8g8b8 at dst (dst_stride bytes per row).  With `hash`, also fold the raw
 * bytes into *hash (FNV-1a over 64-bit words).
 */
static void xdj_fb_read_rows(int y0, int n, int bpp, uint8_t *dst, int dst_stride,
                             uint64_t *hash)
{
    int x, y;

    for (y = y0; y < y0 + n; y++) {
        uint32_t *d = (uint32_t *)(dst + (size_t)(y - y0) * dst_stride);
        uint64_t raw[XDJ_FB_WIDTH * 4 / 8];

        if (bpp == 16) {
            /* RGB565: read a row and expand R5G6B5 -> x8r8g8b8 */
            const uint16_t *row = (const uint16_t *)raw;
            address_space_read(&address_space_memory,
                               XDJ_FB_BASE + (hwaddr)y * XDJ_FB_WIDTH * 2,
                               MEMTXATTRS_UNSPECIFIED, raw, XDJ_FB_WIDTH * 2);
            for (x = 0; x < XDJ_FB_WIDTH; x++) {
                uint16_t p = row[x];
                uint32_t r = (p >> 11) & 0x1f, g = (p >> 5) & 0x3f, b = p & 0x1f;
                r = (r << 3) | (r >> 2);
                g = (g << 2) | (g >> 4);
                b = (b << 3) | (b >> 2);
                d[x] = 0xff000000u | (r << 16) | (g << 8) | b;
            }
        } else {
            /* 32bpp x8r8g8b8 (boot splash), forcing alpha */
            const uint32_t *row = (const uint32_t *)raw;
            address_space_read(&address_space_memory,
                               XDJ_FB_BASE + (hwaddr)y * XDJ_FB_WIDTH * 4,
                               MEMTXATTRS_UNSPECIFIED, raw, XDJ_FB_WIDTH * 4);
            for (x = 0; x < XDJ_FB_WIDTH; x++) {
                d[x] = 0xff000000u | (row[x] & 0x00ffffffu);
            }
        }
        if (hash) {
            int i, words = XDJ_FB_WIDTH * (bpp == 16 ? 2 : 4) / 8;
            for (i = 0; i < words; i++) {
                *hash = (*hash ^ raw[i]) * 0x100000001b3ull;
            }
        }
    }
}

/* The live pixel depth, keeping the last known good one while CPMEM says nothing. */
static int xdj_fb_bpp(void)
{
    static int last_bpp = 32;        /* splash is 32bpp until the app switches */
    int bpp = xdj_fb_detect_bpp();

    if (bpp) {
        last_bpp = bpp;
    }
    return last_bpp;
}

/*
 * Raster readout: what the panel shows.
 *
 * The real panel doesn't see the framebuffer at one instant: the IPU reads it
 * line by line over each refresh (PANEL_LINE_NS per line, top to bottom, the
 * readout of refresh k ending at its EOF).  rbp's DirectFB draws into a
 * single, front-only framebuffer, copying each finished frame in right after
 * its wait for the EOF; a copy that overlaps the readout shows the old frame
 * above the point where it overtakes the readout and the new one below - a
 * tear.  To show that, panel_img is filled band by band (XDJ_RASTER_BAND rows,
 * default 16) at the times the panel reads those rows, and the console shows
 * panel_img.  The guest's copy runs at TCG speed, not the hardware's, so where
 * a tear lands differs from a real unit; whether frames tear, and whether a
 * fix keeps the copy out of the readout, carries over.  XDJ_RASTER=0: the old
 * whole-frame copy at each console refresh (no tearing possible).
 *
 * Tear meter (on demand, like xdj-fps): while "xdj-tears" is read, each
 * readout's bands are hashed when read and again at its EOF.  A band that
 * changed after it was read (stale), followed by a later band that shows new
 * content (it differs from the previous refresh), is a tear at that band's
 * first row.  qom-get /machine xdj-tears = torn refreshes per second over the
 * last second, xdj-tear-row = their mean tear row (-1: none).
 * XDJ_RASTER_TRACE=1 (with the meter running) logs each readout that had new
 * content to stderr: per band 'N' new (differs from the previous refresh),
 * 'S' stale (changed after it was read), '*' both, '.' neither.
 */
#define XDJ_RASTER_MAX_BANDS PANEL_VACTIVE
#define XDJ_TEAR_IDLE_NS     (3 * NANOSECONDS_PER_SECOND)
static bool xdj_raster_on;
static int xdj_raster_band = 16, xdj_raster_nbands;
static QEMUTimer *xdj_raster_timer;
static uint32_t *panel_img;                  /* XDJ_FB_WIDTH x XDJ_FB_HEIGHT */
static int xdj_raster_next;                  /* next band to read */
static int64_t xdj_raster_start;             /* when row 0 of this readout is read */
static int xdj_raster_bpp;                   /* depth for the whole readout */
static uint64_t band_hash[XDJ_RASTER_MAX_BANDS], prev_hash[XDJ_RASTER_MAX_BANDS];

static bool xdj_tear_running;
static int64_t xdj_tear_last_read, xdj_tear_win_start;
static unsigned xdj_tear_count, xdj_tear_row_sum;
static double xdj_tear_value, xdj_tear_row_value = -1;

static void xdj_raster_schedule(void)
{
    int rows_done = MIN((xdj_raster_next + 1) * xdj_raster_band, XDJ_FB_HEIGHT);
    timer_mod(xdj_raster_timer, xdj_raster_start + rows_done * PANEL_LINE_NS);
}

/* End of a readout (its EOF): tear accounting, then the next refresh's readout. */
static void xdj_raster_frame_end(void)
{
    int b;

    if (xdj_tear_running) {
        int64_t now = qemu_clock_get_ns(QEMU_CLOCK_REALTIME);
        bool stale = false, trace = getenv("XDJ_RASTER_TRACE") != NULL, any = false;
        int row = -1;
        char map[XDJ_RASTER_MAX_BANDS + 1];

        for (b = 0; b < xdj_raster_nbands; b++) {
            int y0 = b * xdj_raster_band, n = MIN(xdj_raster_band, XDJ_FB_HEIGHT - y0);
            uint64_t h = 0xcbf29ce484222325ull;
            static uint32_t scratch[XDJ_FB_WIDTH * XDJ_RASTER_MAX_BANDS];
            bool is_new = band_hash[b] != prev_hash[b], is_stale;

            if (stale && is_new && row < 0) {
                row = y0;                    /* new content below stale content */
                if (!trace) {
                    break;
                }
            }
            xdj_fb_read_rows(y0, n, xdj_raster_bpp, (uint8_t *)scratch,
                             XDJ_FB_WIDTH * 4, &h);
            is_stale = h != band_hash[b];    /* changed after it was read */
            stale |= is_stale;
            map[b] = is_new ? (is_stale ? '*' : 'N') : (is_stale ? 'S' : '.');
            any |= is_new || is_stale;
        }
        if (trace && any) {
            map[xdj_raster_nbands] = 0;
            fprintf(stderr, "XDJ_RASTER %s%s\n", map, row >= 0 ? "  TEAR" : "");
        }
        if (row >= 0) {
            xdj_tear_count++;
            xdj_tear_row_sum += row;
        }
        if (now - xdj_tear_last_read > XDJ_TEAR_IDLE_NS) {
            xdj_tear_running = false;        /* nobody is watching: stop */
        } else if (now - xdj_tear_win_start >= NANOSECONDS_PER_SECOND) {
            xdj_tear_value = xdj_tear_count * (double)NANOSECONDS_PER_SECOND /
                             (now - xdj_tear_win_start);
            xdj_tear_row_value = xdj_tear_count ?
                                 (double)xdj_tear_row_sum / xdj_tear_count : -1;
            xdj_tear_count = xdj_tear_row_sum = 0;
            xdj_tear_win_start = now;
        }
    }
    memcpy(prev_hash, band_hash, sizeof(prev_hash));
    xdj_raster_next = 0;
    xdj_raster_start += PANEL_FRAME_NS;
    /* after a pause (or a late main loop): resume at the coming readout */
    {
        int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
        if (xdj_raster_start + PANEL_VACTIVE * PANEL_LINE_NS < now) {
            xdj_raster_start = panel_next_eof(now) - PANEL_VACTIVE * PANEL_LINE_NS;
        }
    }
    xdj_raster_bpp = xdj_fb_bpp();
}

static void xdj_raster_tick(void *opaque)
{
    int y0 = xdj_raster_next * xdj_raster_band;
    int n = MIN(xdj_raster_band, XDJ_FB_HEIGHT - y0);
    uint64_t h = 0xcbf29ce484222325ull;

    xdj_fb_read_rows(y0, n, xdj_raster_bpp,
                     (uint8_t *)(panel_img + (size_t)y0 * XDJ_FB_WIDTH),
                     XDJ_FB_WIDTH * 4, &h);
    band_hash[xdj_raster_next] = h;
    if (++xdj_raster_next >= xdj_raster_nbands) {
        xdj_raster_frame_end();
    }
    xdj_raster_schedule();
}

static void xdj_tears_get(Object *obj, Visitor *v, const char *name,
                          void *opaque, Error **errp)
{
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_REALTIME);
    double val;

    xdj_tear_last_read = now;
    if (!xdj_tear_running) {                 /* (re)start a fresh measurement */
        xdj_tear_running = xdj_raster_on;
        xdj_tear_value = 0;
        xdj_tear_row_value = -1;
        xdj_tear_count = xdj_tear_row_sum = 0;
        xdj_tear_win_start = now;
    }
    val = strcmp(name, "xdj-tear-row") == 0 ? xdj_tear_row_value : xdj_tear_value;
    visit_type_number(v, name, &val, errp);
}

static bool xdj_fb_gfx_update(void *opaque)
{
    DisplaySurface *ds = qemu_console_surface(xdj_fb_con);
    uint8_t *dst;
    int stride, y;

    if (!ds) {
        return false;
    }
    dst = surface_data(ds);
    stride = surface_stride(ds);
    if (xdj_raster_on) {
        for (y = 0; y < XDJ_FB_HEIGHT; y++) {
            memcpy(dst + (size_t)y * stride, panel_img + (size_t)y * XDJ_FB_WIDTH,
                   XDJ_FB_WIDTH * 4);
        }
    } else {
        xdj_fb_read_rows(0, XDJ_FB_HEIGHT, xdj_fb_bpp(), dst, stride, NULL);
    }
    qemu_console_update(xdj_fb_con, 0, 0, XDJ_FB_WIDTH, XDJ_FB_HEIGHT);
    return true;
}

static const GraphicHwOps xdj_fb_ops = {
    .gfx_update = xdj_fb_gfx_update,
};

/*
 * Frame-rate meter: the guest's own presentation rate, independent of any
 * viewer (VNC refreshes the console at <= ~33 Hz and only while a client is
 * connected, so its update rate says nothing about the guest).
 *
 * A host-clock timer samples the scanned-out framebuffer at XDJ_FPS_HZ and
 * counts the samples whose content (or IPU buffer address, i.e. a page flip)
 * changed since the previous one; once a second that count becomes the rate.
 * So it reads "new frames shown per wall-clock second": 0 on a still screen,
 * at most XDJ_FPS_HZ.
 *
 * On demand only: reading the machine property "xdj-fps" (qom-get /machine
 * xdj-fps; the panel's GET /fps) starts the sampler and returns the latest rate;
 * the sampler stops itself XDJ_FPS_IDLE_NS after the last read, so it costs
 * nothing while nobody is watching.
 */
#define XDJ_FPS_HZ       120
#define XDJ_FPS_IDLE_NS  (3 * NANOSECONDS_PER_SECOND)

static QEMUTimer *xdj_fps_timer;
static bool xdj_fps_running;
static int64_t xdj_fps_last_read, xdj_fps_win_start;
static uint64_t xdj_fps_last_hash;
static unsigned xdj_fps_changes;
static double xdj_fps_value;

/* FNV-1a over the visible framebuffer (64-bit words) + the IPU buffer address. */
static uint64_t xdj_fb_hash(void)
{
    int bpp = xdj_fb_detect_bpp();
    hwaddr want = (hwaddr)XDJ_FB_WIDTH * XDJ_FB_HEIGHT * ((bpp ? bpp : 32) / 8);
    hwaddr len = want;
    uint64_t h = 0xcbf29ce484222325ull;
    const uint64_t *p;
    size_t i;

    if (ipu_cpmem_ram) {
        h = (h ^ ldl_le_p(ipu_cpmem_ram + IPU_CH_W1_D0(IPU_DISP_CH))) * 0x100000001b3ull;
    }
    p = address_space_map(&address_space_memory, XDJ_FB_BASE, &len, false,
                          MEMTXATTRS_UNSPECIFIED);
    if (!p) {
        return h;
    }
    for (i = 0; i < len / 8; i++) {
        h = (h ^ p[i]) * 0x100000001b3ull;
    }
    address_space_unmap(&address_space_memory, (void *)p, len, false, 0);
    return h;
}

static void xdj_fps_tick(void *opaque)
{
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_REALTIME);
    uint64_t h;

    if (now - xdj_fps_last_read > XDJ_FPS_IDLE_NS) {
        xdj_fps_running = false;              /* nobody is watching: stop */
        return;
    }
    h = xdj_fb_hash();
    if (h != xdj_fps_last_hash) {
        xdj_fps_changes++;
        xdj_fps_last_hash = h;
    }
    if (now - xdj_fps_win_start >= NANOSECONDS_PER_SECOND) {
        xdj_fps_value = xdj_fps_changes * (double)NANOSECONDS_PER_SECOND /
                        (now - xdj_fps_win_start);
        xdj_fps_changes = 0;
        xdj_fps_win_start = now;
    }
    timer_mod(xdj_fps_timer, now + NANOSECONDS_PER_SECOND / XDJ_FPS_HZ);
}

static void xdj_fps_get(Object *obj, Visitor *v, const char *name,
                        void *opaque, Error **errp)
{
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_REALTIME);
    double fps = xdj_fps_value;

    xdj_fps_last_read = now;
    if (!xdj_fps_running) {                   /* (re)start a fresh measurement */
        xdj_fps_running = true;
        xdj_fps_value = 0;
        fps = 0;
        xdj_fps_changes = 0;
        xdj_fps_win_start = now;
        xdj_fps_last_hash = xdj_fb_hash();
        timer_mod(xdj_fps_timer, now + NANOSECONDS_PER_SECOND / XDJ_FPS_HZ);
    }
    visit_type_number(v, name, &fps, errp);
}

/* realize: GPU (3D/2D/VG) + tempsense + IPU interrupt stubs, CPMEM, scanout console */
void xdj_display_realize(FslIMX6State *s, DeviceState *dev, DeviceState *gic)
{
    static MemoryRegion gpu3d_stub, gpu2d_stub, tempsense_stub;

    /* Vivante GPU register stubs (3D + 2D) so galcore init completes.
     * Each core reports its own identity + IRQ.  The 3D core (GC2000) always
     * reports a 3D part.  The 2D slot reports a real 2D part (GC320) + wires its
     * R2D IRQ so full stock galcore init proceeds (clears the probe -22 and
     * satisfies the 2D command-completion wait); together with the mapped GC355
     * VG core and the DVFS GPT kick this is the full-galcore boot path.
     *
     * This is the default path — it emulates the real dual-core controller's
     * GPU-2D, so vanilla rbp drives galcore instead of falling back to software
     * render, on one or two cores (two = the real controller; the app and a NAND
     * unit use two).  Set XDJ_GPU2D_OFF=1 for the software-render path (2D slot
     * reports GC2000, galcore fails -22 and fast-aborts to software render),
     * for regression comparison. */
    {
        static XdjGpuCore gpu3d_core, gpu2d_core;
        bool gpu2d_full = getenv("XDJ_GPU2D_OFF") == NULL;

        gpu3d_core = (XdjGpuCore){
            .irq      = qdev_get_gpio_in(gic, FSL_IMX6_GPU3D_IRQ),
            .model    = 0x00002000u,  /* GC2000 */
            .revision = 0x00005108u,  /* i.MX6Q GC2000 */
            .events   = 0x3fffffffu,  /* completion events (no error bits 30/31) */
        };
        memory_region_init_io(&gpu3d_stub, OBJECT(dev), &imx6_gpu_stub_ops,
                              &gpu3d_core, "imx6.gpu3d-stub",
                              FSL_IMX6_GPU_3D_SIZE);
        memory_region_add_subregion(get_system_memory(), FSL_IMX6_GPU_3D_ADDR,
                                    &gpu3d_stub);

        gpu2d_core = (XdjGpuCore){
            .irq      = gpu2d_full ?
                        qdev_get_gpio_in(gic, FSL_IMX6_R2D_IRQ) : NULL,
            .model    = gpu2d_full ? 0x00000320u : 0x00002000u, /* GC320/GC2000 */
            .revision = gpu2d_full ? 0x00005007u : 0x00005108u,
            .events   = 0x3fffffffu,
            /* Advertise the 2D core's feature set so galcore's feature DB is
             * populated and the DirectFB GAL gfxdriver / libGAL gco2D take the
             * real (not degraded) blit path -- otherwise rbp's first dynamic
             * control-triggered blit calls a garbage gco2D pointer and SIGSEGVs.
             * Only on the full-galcore path; the GC2000 fallback keeps 0 (it
             * -22 fast-fails before any of this matters). */
            .features        = (gpu2d_full && !getenv("XDJ_GPU2D_NOFEAT")) ? 0xffffffffu : 0u,
            .minor_features  = (gpu2d_full && !getenv("XDJ_GPU2D_NOFEAT")) ? 0xffffffffu : 0u,
        };
        if (gpu2d_full) {
            /* Full stock galcore boot: deliver GPU command-completion IRQs from
             * a timer so galcore's GICC_HPPIR busy-poll sees them, on BOTH the
             * 3D and 2D GIC lines (galcore may use a unified GPU completion IRQ
             * rather than the per-core 2D line). */
            gpu3d_core.fe_timer =
                timer_new_ns(QEMU_CLOCK_VIRTUAL, imx6_gpu_fe_fire, &gpu3d_core);
            gpu2d_core.fe_timer =
                timer_new_ns(QEMU_CLOCK_VIRTUAL, imx6_gpu_fe_fire, &gpu2d_core);
            gpu3d_core.sibling_irq = gpu2d_core.irq;
            gpu2d_core.sibling_irq = gpu3d_core.irq;

            /* Deliver the bus-freq DVFS settling tick from the CCM clock-step
             * register writes (see xdj_dvfs_gpt_kick / imx6_ccm.c). */
            xdj_dvfs_gpt = &s->gpt;
        }
        memory_region_init_io(&gpu2d_stub, OBJECT(dev), &imx6_gpu_stub_ops,
                              &gpu2d_core, "imx6.gpu2d-stub",
                              FSL_IMX6_GPU_2D_SIZE);
        memory_region_add_subregion(get_system_memory(), FSL_IMX6_GPU_2D_ADDR,
                                    &gpu2d_stub);

        /*
         * i.MX6 also has a third Vivante core: the GC355 OpenVG engine
         * ("gcvCORE_VG" = core index 2) at 0x02204000, which QEMU does not map.
         * galcore enumerates and initialises all three cores; its VG clock/idle
         * poll (gckOS_ReadRegisterEx core 2) spins forever reading an unmapped
         * region (returns 0, the "idle"/"clock running" bits never set).  Give
         * the VG core the same idle/clock-reporting stub + a GC355 identity so
         * that init completes too.  On the full-galcore path (the default).
         */
        if (gpu2d_full) {
            static XdjGpuCore gpuvg_core;
            static MemoryRegion gpuvg_stub;
            gpuvg_core = (XdjGpuCore){
                .model    = 0x00000355u,  /* GC355 (OpenVG) */
                .revision = 0x00001216u,
                /* galcore's VG command-queue setup (gckVGCOMMAND) rejects the
                 * core with gcvSTATUS_NOT_SUPPORTED (-13 -> probe -22) unless
                 * chipFeatures bit 28 is set: it derives VG_hw[9] =
                 * (chipFeatures & 0x1fffffff) >> 28 and requires it non-zero
                 * (0x806919a0).  Advertise it (plus the other feature bits a
                 * GC355 reports) so the VG construct completes. */
                .features = 0xffffffffu,
                /* The VG init command signals completion event 30 (bit 30):
                 * its command buffer loads the event register (0x0E01) with
                 * 0x5e = (event 30 | PE source).  Report exactly that event. */
                .events   = 0x40000000u,
                /* The VG core has its own GIC line (V2D/OpenVG = ID 43); galcore
                 * request_irq()s it and blocks in gckOS_WaitSignal for the VG
                 * command to complete.  Wire it + a completion timer so the
                 * command kick raises it (like the 3D/2D cores). */
                .irq      = qdev_get_gpio_in(gic, FSL_IMX6_V2D_IRQ),
            };
            gpuvg_core.fe_timer =
                timer_new_ns(QEMU_CLOCK_VIRTUAL, imx6_gpu_fe_fire, &gpuvg_core);
            memory_region_init_io(&gpuvg_stub, OBJECT(dev), &imx6_gpu_stub_ops,
                                  &gpuvg_core, "imx6.gpuvg-stub",
                                  FSL_IMX6_OPENVG_SIZE);
            memory_region_add_subregion(get_system_memory(),
                                        FSL_IMX6_OPENVG_ADDR, &gpuvg_stub);
        }
    }

    /*
     * anatop TEMPSENSE0 stub (ANALOG base + 0x180): report the temperature
     * conversion as always finished so anatop_thermal_get_temp() returns at
     * once instead of spinning ~5s per read and retrying forever ("Thermal
     * sensor timeout, retry!"), which otherwise pegs a core and starves the
     * app under TCG.  Overlaps the ANALOG region at higher priority so it wins
     * for these four words (TEMPSENSE0 and its SET/CLR/TOG aliases).
     */
    memory_region_init_io(&tempsense_stub, OBJECT(dev), &imx6_tempsense_stub_ops,
                          NULL, "imx6.tempsense-stub", 0x10);
    memory_region_add_subregion_overlap(get_system_memory(),
                                        FSL_IMX6_ANALOG_ADDR + 0x180,
                                        &tempsense_stub, 1);

    /* i.MX6 IPU1 interrupt stub: synthesize the panel's EOF interrupts so the
     * mxc_sdc_fb display path's waits complete (fast boot, paced page-flips). */
    {
        static MemoryRegion ipu1_cm;
        ipu_sync_irq_line = qdev_get_gpio_in(gic, FSL_IMX6_IPU1_SYNC_IRQ);
        memory_region_init_io(&ipu1_cm, OBJECT(dev), &ipu_cm_ops, NULL,
                              "imx6.ipu1-cm", IPU_CM_SIZE);
        memory_region_add_subregion(get_system_memory(), IPU1_CM_BASE, &ipu1_cm);
        ipu_irq_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, ipu_irq_fire, NULL);
        panel_epoch = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
        /* armed on demand when the driver writes the IPU interrupt registers */
    }


    /* IPU1 CPMEM capture: back the channel-parameter memory with RAM so the
     * mxc_sdc_fb driver's geometry writes are retained, letting the scanout
     * read the live line stride and pick 16bpp (UI) vs 32bpp (splash). */
    {
        static MemoryRegion ipu1_cpmem;
        memory_region_init_ram(&ipu1_cpmem, OBJECT(dev), "imx6.ipu1-cpmem",
                               IPU1_CPMEM_SIZE, &error_abort);
        memory_region_add_subregion_overlap(get_system_memory(),
                                             IPU1_CPMEM_BASE, &ipu1_cpmem, 1);
        ipu_cpmem_ram = memory_region_get_ram_ptr(&ipu1_cpmem);
    }



    /* Framebuffer scanout console (mirror of the mxc_sdc_fb memory) */
    xdj_fb_con = qemu_graphic_console_create(dev, 0, &xdj_fb_ops, NULL);
    qemu_console_resize(xdj_fb_con, XDJ_FB_WIDTH, XDJ_FB_HEIGHT);

    /* Raster readout + tear meter (see xdj_raster_tick / xdj_tears_get) */
    {
        const char *r = getenv("XDJ_RASTER"), *band = getenv("XDJ_RASTER_BAND");
        xdj_raster_on = !(r && strcmp(r, "0") == 0);
        if (band && atoi(band) >= 1 && atoi(band) <= XDJ_FB_HEIGHT) {
            xdj_raster_band = atoi(band);
        }
        xdj_raster_nbands = (XDJ_FB_HEIGHT + xdj_raster_band - 1) / xdj_raster_band;
        if (xdj_raster_on) {
            panel_img = g_new0(uint32_t, XDJ_FB_WIDTH * XDJ_FB_HEIGHT);
            xdj_raster_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, xdj_raster_tick, NULL);
            xdj_raster_start = panel_next_eof(panel_epoch) - PANEL_VACTIVE * PANEL_LINE_NS;
            xdj_raster_bpp = 32;
            xdj_raster_schedule();
        }
        object_property_add(qdev_get_machine(), "xdj-tears", "number",
                            xdj_tears_get, NULL, NULL, NULL);
        object_property_add(qdev_get_machine(), "xdj-tear-row", "number",
                            xdj_tears_get, NULL, NULL, NULL);
    }

    /* Frame-rate meter (see xdj_fps_get): qom-get /machine xdj-fps */
    xdj_fps_timer = timer_new_ns(QEMU_CLOCK_REALTIME, xdj_fps_tick, NULL);
    object_property_add(qdev_get_machine(), "xdj-fps", "number",
                        xdj_fps_get, NULL, NULL, NULL);
}
