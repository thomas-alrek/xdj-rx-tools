/*
 * Pioneer XDJ-RX emulation -- front-panel MCU ("sub-ucom") SPI slave: control frames in, LED frames out, MAIN-UCOM keys, USB fault lines
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

/*
 * XDJ-RX front-panel microcontroller ("Subucom") - SPI slave stub.
 *
 * rbp handshakes with the panel MCU over /dev/subucom_spi1.0 (an ECSPI slave,
 * bus 1 / CS 0, 1.5 MHz) and blocks until it has read a complete
 * ui::StaticInfomation from it ("Subucom StaticInfomation is not completed.
 * retry...").  The MCU also streams jog-wheel / fader / button state.  QEMU
 * attaches no SPI slave, so the driver reads zeros and the handshake never
 * completes.
 *
 * This is the traffic-capture stage: an SSI slave on each ECSPI bus that logs
 * every byte the driver clocks out (SUBUCOM_TRACE=1) so the panel_protocol
 * frame format can be reverse-engineered from real traffic, and returns a
 * placeholder 0xff on MISO.  The protocol response is filled in next.
 */
#define TYPE_XDJ_SUBUCOM "xdj-subucom"
OBJECT_DECLARE_SIMPLE_TYPE(XdjSubucom, XDJ_SUBUCOM)

struct XdjSubucom {
    SSIPeripheral parent_obj;
    uint32_t idx;         /* total MOSI byte counter (trace only) */
    uint32_t pos;         /* byte position within the current SPI burst */
    int      press;       /* >0: menu-press injection countdown */
    uint8_t  frame[128];  /* SubRxData frame presented on MISO this burst */
    uint8_t  mosi[128];   /* raw MOSI bytes rbp sent this burst (SubTxData) */
    uint8_t  tx_last[128];/* last published logical TX frame */
    uint32_t tx_len;      /* length of the last seen burst (0 = none yet) */
    uint8_t  tx_pub[20];  /* last published LED payload */
    bool     tx_pub_valid;
};

static XdjSubucom *g_xdj_subucom;
/*
 * The two front-USB active-low FAULT-flag GPIOs (GPIO4_IO30 = USB1, GPIO7_IO12
 * = USB2; gpiodrv ids 0x7e/0xcc), captured at realize so the harness can inject
 * a USB fault at runtime by driving one LOW (see xdj_subucom_frame_start).
 */
DeviceState *g_xdj_usb_gpio[2];
/*
 * All i.MX6 GPIO bank devices, captured at realize. Used to drive the MAIN-UCOM
 * "direct-input" key GPIOs (USB1/USB2/rekordbox/MIDI/DECK/TIME, read by rbp via
 * /dev/gpiodrv) -- see the /tmp/mainkey hook in xdj_subucom_frame_start.
 */
DeviceState *g_xdj_gpio[8];

/*
 * CRC-16/X-25 (poly 0x1021, reflected 0x8408, init 0xffff, refin/refout, xorout
 * 0xffff). rbp's SubRxData parser (FUN_00284d44) verifies the halfword at
 * rbp_buf[0x62] against this CRC computed over the FIRST 96 bytes of the frame,
 * with each 32-bit word byte-swapped first (FUN_0028259c reads words and REVs
 * them, then calls the table CRC at 0x3f7f4; the length method 0x2859b8 returns
 * start+96). Decoding happens ONLY when this matches -- so a valid CRC is the
 * real gate for buttons, not the frame header. (RE 2026-11-05.)
 */
static uint16_t xdj_crc16_tab[256];
static int xdj_crc16_ready;
static void xdj_crc16_init(void)
{
    int i, k;
    for (i = 0; i < 256; i++) {
        uint16_t c = i;
        for (k = 0; k < 8; k++) {
            c = (c & 1) ? (uint16_t)((c >> 1) ^ 0x8408) : (uint16_t)(c >> 1);
        }
        xdj_crc16_tab[i] = c;
    }
    xdj_crc16_ready = 1;
}
static uint16_t xdj_crc16(const uint8_t *d, int n)
{
    uint16_t crc = 0xffff;
    int i;
    if (!xdj_crc16_ready) {
        xdj_crc16_init();
    }
    for (i = 0; i < n; i++) {
        crc = (uint16_t)(xdj_crc16_tab[(crc ^ d[i]) & 0xff] ^ (crc >> 8));
    }
    return (uint16_t)~crc;
}

/*
 * Auto-init the mixer to an audible default so the device "just works" on boot
 * ("auto-level"). rbp only polls the panel MCU once its panel thread is up, so
 * the SPI burst that calls xdj_subucom_frame_start() is itself the "rbp is
 * running" signal -- no guest-console scraping, no filter chardev, no /tmp files.
 * A short settle after the FIRST poll lets the MixerEngine finish coming up, then
 * -- whenever the operator is NOT driving the panel (no /tmp/subucom_ctrl) -- we
 * assert a sensible persistent hold set: both channels routed to DECK plus
 * unity-ish gains (the same values the manual inj.py "base" uses), so a loaded
 * deck is audible the moment it plays, with no manual inject. The /tmp ctrl file
 * (debug/web-panel path) still overrides it frame-for-frame when present.
 *   XDJ_AUTOLEVEL=0            disables it entirely (idle routing stays LINE).
 *   XDJ_AUTOLEVEL_DELAY_MS=N   settle delay after the first poll (default 4000).
 * {off, val, bit}: val>=0 = LE 16-bit analog; else a button bitmask (bit).
 */
static const struct { int off; long val; int bit; } xdj_autolevel[] = {
    { 0x2c, 0x3ff, -1 },   /* channel fader (up)      */
    { 0x4a, 0x3ff, -1 },   /* MASTER level (up)       */
    { 0x4e, 0x200, -1 },   /* crossfader (centre)     */
    { 0x24, 0x200, -1 },   /* EQ HI  (centre/flat)    */
    { 0x2a, 0x200, -1 },   /* EQ MID (centre/flat)    */
    { 0x28, 0x200, -1 },   /* EQ LOW (centre/flat)    */
    { 0x26, 0x3ff, -1 },   /* TRIM / gain (up)        */
    { 0x2e, 0x200, -1 },   /* CH1 COLOR (centre = off) */
    { 0x3c, 0x3ff, -1 },   /* CH2 channel fader (up)  */
    { 0x34, 0x200, -1 },   /* CH2 EQ HI  (centre)     */
    { 0x38, 0x200, -1 },   /* CH2 EQ LOW (centre)     */
    { 0x3a, 0x200, -1 },   /* CH2 EQ MID (centre)     */
    { 0x36, 0x3ff, -1 },   /* CH2 TRIM (up)           */
    { 0x3e, 0x200, -1 },   /* CH2 COLOR (centre = off) */
    { 0x33,    -1, 0x80 }, /* CH0 source -> DECK      */
    { 0x43,    -1, 0x80 }, /* CH1 source -> DECK      */
};

/* True once rbp has been polling the panel for the settle delay (and enabled). */
static bool xdj_autolevel_armed(void)
{
    static int      disabled = -1;   /* -1 unknown; 0 enabled; 1 disabled */
    static int64_t  delay_ns;
    static int64_t  t0;              /* virtual time of the first panel poll */
    int64_t now;

    if (disabled < 0) {
        const char *e = getenv("XDJ_AUTOLEVEL");
        const char *d = getenv("XDJ_AUTOLEVEL_DELAY_MS");
        int ms = d ? atoi(d) : 4000;
        disabled = (e && e[0] == '0') ? 1 : 0;
        delay_ns = (int64_t)(ms < 0 ? 0 : ms) * 1000000;
    }
    if (disabled) {
        return false;
    }
    now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    if (t0 == 0) {                   /* first poll: start the settle timer */
        t0 = now;
        return false;
    }
    return (now - t0) >= delay_ns;
}

/*
 * Phase 1 control channel (XDJ-RX standalone-app roadmap). Read panel control
 * input from a QEMU chardev instead of polling a /tmp file every SPI frame. Two
 * independent channels, each a socket the harness (server.py now, the Tauri host
 * later) connects to and writes:
 *   id=xdjctrl -> the sub-ucom snapshot (same text spec as /tmp/subucom_ctrl:
 *                 "B 12 08", "A 54 <hex>", one-shots "b"/"a")
 *   id=xdjkey  -> the MAIN-UCOM key GPIO int (same as /tmp/mainkey)
 * Framing: each write is the ENTIRE desired state (one snapshot) terminated by a
 * NUL byte; an empty record (a lone NUL) releases. One NUL record == one atomic
 * ctrl-file write today, so the file model's "the whole snapshot replaces the held
 * set" semantics are preserved. On client disconnect the last snapshot is kept
 * (mirrors "the file stays until removed"), so holds/auto-level survive a harness
 * restart.
 *
 * If a channel's chardev is absent, the frame hook falls back to its file path
 * (XDJ_CTRL_PATH / XDJ_KEY_PATH, else the legacy /tmp names) -- the env override
 * also makes the file path per-instance.
 *
 * Thread-safety: the chardev frontend handlers and xdj_subucom_frame_start (called
 * from imx_spi_flush_txfifo) both run in the QEMU main loop under the BQL, so they
 * are serialized -- these buffers need no locking.
 */
#define XDJ_CTRL_MAX 4096
typedef struct XdjCtrlChan {
    const char *id;                 /* chardev id to bind ("xdjctrl"/"xdjkey")   */
    CharFrontend be;
    bool bound;                     /* bind attempted (whether or not it existed) */
    bool present;                   /* a chardev with this id exists = socket mode */
    bool overflow;                  /* current record overran the buffer; resync   */
    char acc[XDJ_CTRL_MAX];         /* bytes accumulated up to the next NUL         */
    int  acc_len;
    char cur[XDJ_CTRL_MAX];         /* latest complete record (NUL-terminated)      */
    bool fresh;                     /* a new record arrived since last consumed     */
} XdjCtrlChan;

static XdjCtrlChan xdj_ctrl_chan = { .id = "xdjctrl" };
static XdjCtrlChan xdj_key_chan  = { .id = "xdjkey"  };

static int xdj_ctrl_can_read(void *opaque)
{
    XdjCtrlChan *c = opaque;
    return XDJ_CTRL_MAX - 1 - c->acc_len;   /* room left before the terminator */
}

static void xdj_ctrl_receive(void *opaque, const uint8_t *buf, int size)
{
    XdjCtrlChan *c = opaque;
    int i;
    for (i = 0; i < size; i++) {
        uint8_t b = buf[i];
        if (b == 0) {                        /* record boundary: swap it in */
            if (!c->overflow) {
                c->acc[c->acc_len] = 0;
                memcpy(c->cur, c->acc, (size_t)c->acc_len + 1);
                c->fresh = true;
            }
            c->acc_len = 0;
            c->overflow = false;
        } else if (c->acc_len < XDJ_CTRL_MAX - 1) {
            c->acc[c->acc_len++] = (char)b;
        } else {
            c->overflow = true;              /* too long: drop until next NUL */
        }
    }
}

static void xdj_ctrl_event(void *opaque, QEMUChrEvent ev)
{
    XdjCtrlChan *c = opaque;
    if (ev == CHR_EVENT_OPENED) {            /* new client: drop any partial record */
        c->acc_len = 0;
        c->overflow = false;
        if (c == &xdj_ctrl_chan && g_xdj_subucom) {
            g_xdj_subucom->tx_pub_valid = false;   /* re-send LED state to it */
        }
    }
}

/* Bind the channel's chardev once; returns true if socket mode is active. */
static bool xdj_ctrl_bind(XdjCtrlChan *c)
{
    if (!c->bound) {
        Chardev *chr = qemu_chr_find(c->id);
        c->bound = true;
        if (chr) {
            qemu_chr_fe_init(&c->be, chr, &error_abort);
            qemu_chr_fe_set_handlers(&c->be, xdj_ctrl_can_read, xdj_ctrl_receive,
                                     xdj_ctrl_event, NULL, c, NULL, true);
            c->present = true;
        }
    }
    return c->present;
}

/* Non-blocking write back to the channel's client (LED back-channel). */
static void xdj_ctrl_chan_write(XdjCtrlChan *c, const uint8_t *buf, int len)
{
    if (c->present && qemu_chr_fe_backend_open(&c->be)) {
        qemu_chr_fe_write(&c->be, buf, len);   /* partial/failed write = dropped */
    }
}

/*
 * Parse a sub-ucom ctrl snapshot (newline-separated spec lines, same grammar as
 * the /tmp/subucom_ctrl file) into the hold arrays. Returns the number of holds.
 * If a one-shot ("b"/"a") line is present, fills the inj_off/inj_val/inj_bit
 * out-params from the first one and sets has_oneshot (the caller fires it, so
 * an in-flight one-shot is never clobbered). Parsing matches the old inline
 * fopen/fgets loop byte-for-byte -- only the input source changed.
 */
static int xdj_parse_ctrl(const char *text, int maxhold,
                          int *hold_off, long *hold_val, int *hold_bit,
                          int *inj_off, long *inj_val, int *inj_bit,
                          bool *has_oneshot)
{
    int newn = 0;
    bool did_oneshot = false;
    const char *p = text;
    char line[64];

    while (p && *p) {
        int li = 0;
        while (*p && *p != '\n') {           /* copy one line (drop the newline) */
            if (li < (int)sizeof(line) - 1) {
                line[li++] = *p;
            }
            p++;
        }
        line[li] = 0;
        if (*p == '\n') {
            p++;
        }
        {
            char kind = 0; unsigned off = 0, a = 0;
            if (sscanf(line, " %c %x %x", &kind, &off, &a) < 2) {
                continue;
            }
            if (kind == 'B' || kind == 'A') {
                if (newn < maxhold) {
                    hold_off[newn] = (int)off;
                    if (kind == 'A') { hold_val[newn] = (long)a; hold_bit[newn] = -1; }
                    else             { hold_bit[newn] = (int)a;  hold_val[newn] = -1; }
                    newn++;
                }
            } else if (!did_oneshot) {        /* first one-shot line only */
                *inj_off = (int)off;
                if (kind == 'a') { *inj_val = (long)a; *inj_bit = -1; }
                else             { *inj_bit = (int)a;  *inj_val = -1; }
                did_oneshot = true;
            }
        }
    }
    if (has_oneshot) {
        *has_oneshot = did_oneshot;
    }
    return newn;
}

/*
 * rbp -> panel MCU direction (SubTxData, the MOSI half of the burst): LED /
 * indicator / level-meter state (the "LED back-channel"). rbp's TX frame shares the RX
 * transport conventions: per-32-bit-word byte reversal on the wire, and a
 * CRC-16/X-25 (same table as RX) over the first XDJ_TX_PAYLOAD bytes stored LE at
 * [0x16] (logical coordinates). Only bytes 0x00..0x13 carry state; the rest of the
 * 100-byte burst is stale buffer memory. Layout: docs/subucom-led-map.md.
 *
 * At the end of each burst the payload is un-swapped, CRC-checked, and -- when it
 * differs from the last one -- published as one text line "T <40 hex>\n":
 *   - socket mode: written back up the xdjctrl chardev (the same connection the
 *     panel server pushes control snapshots on), non-blocking; dropped if no
 *     client / the socket is full (the next change re-sends full state anyway);
 *   - file mode:   atomically replaces XDJ_TX_PATH (default /tmp/subucom_tx).
 *   XDJ_TX_LOG=1  also logs "SUBUCOM-TX <len> <hex>" (the whole burst) on stderr.
 */
#define XDJ_TX_PAYLOAD 20
#define XDJ_TX_CRC_OFF 0x16

/* Source-key LEDs (rekordbox / MIDI / USB1 / USB2). Not in SubTxData: rbp drives
 * them as GPIO outputs 162..165 (GPIO6_IO02..05, active-high, one-hot, verified
 * live 2026-11-15). Mirrored here (bit0 = rekordbox .. bit3 = USB2) and appended
 * to every published LED line as a 21st byte. */
static uint8_t g_xdj_srcled;

static void xdj_subucom_tx_publish(const uint8_t *tx)
{
    char line[12 + 2 * XDJ_TX_PAYLOAD];
    int i, n;
    XdjCtrlChan *c = &xdj_ctrl_chan;

    n = snprintf(line, sizeof(line), "T ");
    for (i = 0; i < XDJ_TX_PAYLOAD; i++) {
        n += snprintf(line + n, sizeof(line) - n, "%02x", tx[i]);
    }
    n += snprintf(line + n, sizeof(line) - n, " %02x", g_xdj_srcled);
    line[n++] = '\n';
    if (xdj_ctrl_bind(c)) {
        xdj_ctrl_chan_write(c, (const uint8_t *)line, n);
    } else {
        static char path[512], tmp[520];
        FILE *f;
        if (!path[0]) {
            const char *e = getenv("XDJ_TX_PATH");
            snprintf(path, sizeof(path), "%s", (e && e[0]) ? e : "/tmp/subucom_tx");
            snprintf(tmp, sizeof(tmp), "%s.tmp", path);
        }
        f = fopen(tmp, "w");
        if (f) {
            fwrite(line, 1, n, f);
            fclose(f);
            rename(tmp, path);
        }
    }
}

static void xdj_subucom_tx_done(XdjSubucom *s)
{
    uint8_t tx[128], sw[XDJ_TX_PAYLOAD];
    uint32_t n = s->pos, i;
    uint16_t crc;
    static int log = -1;

    if (n < XDJ_TX_CRC_OFF + 2) {
        return;
    }
    if (n > sizeof(tx)) {
        n = sizeof(tx);
    }
    for (i = 0; i < n; i++) {
        uint32_t j = (i & ~3u) + (3 - (i & 3));
        tx[i] = (j < n) ? s->mosi[j] : 0;
    }
    if (log < 0) {
        const char *e = getenv("XDJ_TX_LOG");
        log = (e && e[0] && e[0] != '0') ? 1 : 0;
    }
    if (log && (n != s->tx_len || memcmp(tx, s->tx_last, n) != 0)) {
        fprintf(stderr, "SUBUCOM-TX %u ", n);
        for (i = 0; i < n; i++) {
            fprintf(stderr, "%02x", tx[i]);
        }
        fprintf(stderr, "\n");
    }
    if (n == s->tx_len && memcmp(tx, s->tx_last, n) == 0) {
        return;
    }
    memcpy(s->tx_last, tx, n);
    s->tx_len = n;
    /* CRC gate: same scheme as RX -- CRC over the word-reversed payload. */
    for (i = 0; i < XDJ_TX_PAYLOAD; i++) {
        sw[i] = tx[(i & ~3u) + (3 - (i & 3))];
    }
    crc = xdj_crc16(sw, XDJ_TX_PAYLOAD);
    if ((tx[XDJ_TX_CRC_OFF] | (tx[XDJ_TX_CRC_OFF + 1] << 8)) != crc) {
        return;                      /* not a valid SubTxData frame (boot noise) */
    }
    if (memcmp(tx, s->tx_pub, XDJ_TX_PAYLOAD) == 0 && s->tx_pub_valid) {
        return;
    }
    memcpy(s->tx_pub, tx, XDJ_TX_PAYLOAD);
    s->tx_pub_valid = true;
    xdj_subucom_tx_publish(tx);
}

/* GPIO6_IO02..05 output edge (rbp switched source): mirror and re-publish. */
void xdj_srcled_set(void *opaque, int n, int level)
{
    uint8_t v = level ? (g_xdj_srcled | (1u << n)) : (g_xdj_srcled & ~(1u << n));

    if (v == g_xdj_srcled) {
        return;
    }
    g_xdj_srcled = v;
    if (g_xdj_subucom && g_xdj_subucom->tx_pub_valid) {
        xdj_subucom_tx_publish(g_xdj_subucom->tx_pub);
    }
}

/*
 * Called by the ECSPI (imx_spi_flush_txfifo) at the start of every SPI burst so
 * the panel's SubRxData frame is presented byte-aligned on MISO. We build the
 * frame in rbp's buffer coordinates (rbp_buf[j]), compute the CRC rbp expects,
 * then shift it by +3 into the MISO buffer (the kernel drops 3 MISO bytes during
 * the command prefix, so rbp_buf[j] == slave frame[j+3]).
 * `touch /tmp/subucom_menu` injects one menu-button press.
 */
void xdj_subucom_frame_start(void)
{
    XdjSubucom *s = g_xdj_subucom;
    uint8_t rbp[100];
    uint8_t sw[96];
    uint16_t crc;
    int k;

    if (!s) {
        return;
    }
    /*
     * USB fault-flag injection. These are NOT insert-detect lines: rbp's
     * UsbStorageManager (listener 0x250020) reads them as active-low USB fault
     * flags; LOW on a mounted slot raises "USB Error. Remove the device." and
     * HIGH clears it (500 ms timer). Normal inserts must leave them HIGH -- the
     * attach itself wakes HOST1 (xdj-usbnc.c). Idle HIGH from reset (xdj-board.c).
     *   touch /tmp/usb_insert  -> USB1 fault asserted (GPIO4_IO30 driven LOW)
     *   rm    /tmp/usb_insert  -> USB1 fault cleared  (GPIO4_IO30 back HIGH)
     *   /tmp/usb2_insert does the same for USB2 (GPIO7_IO12).
     */
    {
        static int usb_present[2] = { 0, 0 };
        static const int det_pin[2] = { 30, 12 };
        static const char *det_file[2] = { "/tmp/usb_insert", "/tmp/usb2_insert" };
        int u;
        for (u = 0; u < 2; u++) {
            int want = (access(det_file[u], F_OK) == 0);
            if (want != usb_present[u] && g_xdj_usb_gpio[u]) {
                qemu_set_irq(qdev_get_gpio_in(g_xdj_usb_gpio[u], det_pin[u]),
                             want ? 0 : 1);
                usb_present[u] = want;
            }
        }
    }
    /*
     * MAIN-UCOM direct-key simulation. The service manual shows USB1/USB2 (and
     * rekordbox/MIDI/DECK1-2/TIME MODE) are NOT subucom keys -- they are i.MX6
     * GPIO switches read by rbp via /dev/gpiodrv, idle HIGH / pressed LOW. The 8
     * such keys are Linux GPIO numbers 119,120,146,149,158,159,160,161
     * (rbp FUN_00274a38). GPIO N lives on bank s->gpio[N/32], pin N%32.
     *   echo <gpio> > /tmp/mainkey   -> hold that key pressed (drive LOW)
     *   rm /tmp/mainkey  (or empty)  -> release (drive HIGH)
     * The keys idle HIGH from reset (psr-reset, set in realize), so no boot-time
     * edge is needed here -- only drive a pin LOW on press / back HIGH on release.
     */
    {
        static int pressed = -1;
        int want = -1;
        if (xdj_ctrl_bind(&xdj_key_chan)) {         /* socket mode: xdjkey chardev */
            if (xdj_key_chan.cur[0]) {
                if (sscanf(xdj_key_chan.cur, "%d", &want) != 1) {
                    want = -1;
                }
            }
        } else {                                    /* fallback: XDJ_KEY_PATH / file */
            const char *path = getenv("XDJ_KEY_PATH");
            FILE *kf;
            if (!path || !path[0]) {
                path = "/tmp/mainkey";
            }
            kf = fopen(path, "r");
            if (kf) {
                if (fscanf(kf, "%d", &want) != 1) {
                    want = -1;
                }
                fclose(kf);
            }
        }
        if (want != pressed) {
            if (pressed >= 0 && pressed < 256 && g_xdj_gpio[pressed >> 5]) {
                qemu_set_irq(qdev_get_gpio_in(g_xdj_gpio[pressed >> 5],
                                              pressed & 31), 1);  /* release */
            }
            if (want >= 0 && want < 256 && g_xdj_gpio[want >> 5]) {
                qemu_set_irq(qdev_get_gpio_in(g_xdj_gpio[want >> 5],
                                              want & 31), 0);      /* press */
            }
            pressed = want;
        }
    }
    if (getenv("SUBUCOM_BURST") && s->pos) {
        fprintf(stderr, "SUBUCOM msg_size=%u\n", s->pos);
    }
    xdj_subucom_tx_done(s);
    s->pos = 0;

    /*
     * Flexible control injection. Write a one-line spec to /tmp/subucom_ctrl:
     *   "b <off> <bitmask>"  -> one-shot button press+release (auto, ~30 frames)
     *   "a <off> <value>"    -> one-shot LE 16-bit analog, then release
     *   "B <off> <bitmask>"  -> HOLD a button down while the file exists (release on rm)
     *   "A <off> <value>"    -> HOLD an LE 16-bit analog value while the file exists
     * Offsets/values are hex. Uppercase B/A = persistent hold (lets a tester observe
     * held/analog/momentary states and fader positions; release by removing the file).
     * `touch /tmp/subucom_menu` is the legacy one-shot MENU shortcut.
     */
    /*
     * Up to MAXHOLD simultaneous persistent holds (A/B lines), parsed from ALL
     * lines of the ctrl file. Analog faders whose position spans two complementary
     * frame fields (the tempo fader = 0x04 negative half + 0x0a positive half) must
     * set both at once: with only one field forced rbp reads the pot as fully pegged.
     */
    enum { MAXHOLD = 64 };   /* full virtual-panel state + the merged auto-level set */
    static int  hold_off[MAXHOLD];
    static long hold_val[MAXHOLD];              /* >=0: 16-bit analog; <0: button bit */
    static int  hold_bit[MAXHOLD];
    static int  nhold = 0;
    static int inj_off = -1, inj_bit = 0;       /* one-shot */
    static long inj_val = -1;
    {
        int newn = 0;
        bool had_input = false;                 /* a ctrl snapshot/file was present */
        int  t_off = -1, t_bit = 0; long t_val = -1;
        bool has_os = false;                    /* the snapshot contains a one-shot */
        if (xdj_ctrl_bind(&xdj_ctrl_chan)) {    /* socket mode: xdjctrl chardev */
            bool fresh = xdj_ctrl_chan.fresh;
            had_input = true;                   /* the in-memory snapshot IS the state */
            newn = xdj_parse_ctrl(xdj_ctrl_chan.cur, MAXHOLD,
                                  hold_off, hold_val, hold_bit,
                                  &t_off, &t_val, &t_bit, &has_os);
            if (has_os && fresh && s->press == 0) {
                /* fire the one-shot once per fresh record (replaces remove(file)) */
                inj_off = t_off; inj_val = t_val; inj_bit = t_bit;
                s->press = 30;
                xdj_ctrl_chan.fresh = false;
            } else if (!has_os) {
                xdj_ctrl_chan.fresh = false;    /* nothing to fire; consume freshness */
            }                                   /* has_os & press>0: keep fresh, retry */
        } else {                                /* fallback: XDJ_CTRL_PATH / legacy file */
            const char *path = getenv("XDJ_CTRL_PATH");
            FILE *cf;
            if (!path || !path[0]) {
                path = "/tmp/subucom_ctrl";
            }
            cf = fopen(path, "r");
            if (cf) {
                char buf[XDJ_CTRL_MAX];
                size_t n = fread(buf, 1, sizeof(buf) - 1, cf);
                buf[n] = 0;
                fclose(cf);
                had_input = true;
                newn = xdj_parse_ctrl(buf, MAXHOLD, hold_off, hold_val, hold_bit,
                                      &t_off, &t_val, &t_bit, &has_os);
                if (has_os && s->press == 0) {
                    inj_off = t_off; inj_val = t_val; inj_bit = t_bit;
                    s->press = 30;
                    if (newn == 0) remove(path); /* one-shot consumed, no holds to keep */
                }
            }
        }
        /*
         * Auto-level baseline (MERGE, not replace). Once armed, assert the audible
         * default for every control the operator is NOT currently driving, so the
         * initial mixer state "just works" for testing while the web panel / inj
         * can still override ANY individual control at runtime (its hold for that
         * offset wins) -- and an unrelated leftover hold (e.g. a browse-encoder
         * value) can no longer silence the deck by suppressing the whole set.
         */
        if (xdj_autolevel_armed()) {
            int d;
            for (d = 0; d < (int)ARRAY_SIZE(xdj_autolevel) && newn < MAXHOLD; d++) {
                int j, overridden = 0;
                for (j = 0; j < newn; j++) {
                    if (hold_off[j] == xdj_autolevel[d].off) { overridden = 1; break; }
                }
                if (overridden) {
                    continue;                    /* operator is driving this control */
                }
                hold_off[newn] = xdj_autolevel[d].off;
                hold_val[newn] = xdj_autolevel[d].val;
                hold_bit[newn] = xdj_autolevel[d].bit;
                newn++;
            }
        }
        nhold = newn;
        /* legacy MENU one-shot shortcut (only when no ctrl input is present) */
        if (!had_input && s->press == 0 && access("/tmp/subucom_menu", F_OK) == 0) {
            inj_off = 0x1b; inj_bit = 0x01; inj_val = -1; /* MENU (code 0x20a) */
            s->press = 30; remove("/tmp/subucom_menu");
        }
    }

    /* Build the 100-byte frame rbp will see (rbp_buf coordinates). */
    memset(rbp, 0, sizeof(rbp));
    /* Bytes 2-3 = the panel MCU's firmware version word (LE). rbp keeps it from
     * the first frame (boot-key handler FUN_00282630 -> +0x94) and shows it as
     * "SUB Ver." in test mode; the real MCU reports 0.35 (images/subucom.rev).
     * XDJ_SUBUCOM_VER=<hex> overrides (default 0x0035). */
    {
        static int ver = -1;
        if (ver < 0) {
            const char *e = getenv("XDJ_SUBUCOM_VER");
            ver = e ? (int)(strtoul(e, NULL, 16) & 0xffff) : 0x0035;
        }
        rbp[2] = ver & 0xff;
        rbp[3] = (ver >> 8) & 0xff;
    }

    {
        int hi;
        for (hi = 0; hi < nhold; hi++) {         /* persistent holds: all applied */
            int off = hold_off[hi];
            if (off < 0 || off + 1 >= (int)sizeof(rbp)) continue;
            if (hold_val[hi] >= 0) {
                rbp[off]     = (uint8_t)(hold_val[hi] & 0xff);
                rbp[off + 1] = (uint8_t)((hold_val[hi] >> 8) & 0xff);
            } else {
                rbp[off] |= (uint8_t)hold_bit[hi];
            }
        }
    }
    if (s->press > 0) {                          /* one-shot press window (applies ON TOP of holds) */
        if (s->press > 8 && inj_off >= 0) {      /* was nhold==0 gated: a one-shot must fire while held */
            if (inj_val >= 0) {
                rbp[inj_off]     = (uint8_t)(inj_val & 0xff);
                rbp[inj_off + 1] = (uint8_t)((inj_val >> 8) & 0xff);
            } else {
                rbp[inj_off] |= (uint8_t)inj_bit;
            }
        }                                       /* else: released -> up event */
        s->press--;
    }

    /*
     * CRC gate: rbp CRCs the first 96 bytes with each 32-bit word byte-reversed
     * (sw[4w+j] = rbp[4w + (3-j)]), then stores the result at rbp_buf[0x62].
     */
    for (k = 0; k < 96; k++) {
        sw[k] = rbp[(k & ~3) + (3 - (k & 3))];
    }
    crc = xdj_crc16(sw, 96);
    if (getenv("SUBUCOM_BADCRC")) {
        crc ^= 0xffff;              /* corrupt -> rbp rejects every frame (no decode) */
    }
    rbp[0x62] = crc & 0xff;
    rbp[0x63] = (crc >> 8) & 0xff;

    /*
     * Present the frame on MISO.  The ECSPI shifts each 32-bit word MSB-first,
     * so the wire byte at position p lands in rbp's logical read buffer B at
     * index (p & ~3) + (3 - (p & 3)) -- a per-32-bit-word byte reversal, with
     * NO linear byte drop (gdb calibration against live rbp, 2026-11-06,
     * confirmed to the byte; the earlier +3 "kernel drops 3 MISO bytes" was a
     * misreading of this word swap).  This is exactly the swap rbp's CRC
     * routine (FUN_0028259c) reverses before hashing.  So to make rbp's buffer
     * equal our logical frame rbp[] -- CRC at rbp_buf[0x62], control sections
     * at 0x04/0x14/0x24/0x34/0x44 -- we transmit rbp[] with each word reversed:
     *   s->frame[i] = rbp[(i & ~3) + (3 - (i & 3))].
     * rbp then re-reverses, recovering rbp[] exactly, and its CRC over the
     * first 96 bytes matches the value we stored at rbp[0x62].  SUBUCOM_RAMP
     * fills a 0,1,2,... ramp (diagnostic: reveals the raw MISO->rbp_buf map).
     */
    {
        size_t i;
        memset(s->frame, 0, sizeof(s->frame));
        for (i = 0; i < sizeof(rbp); i++) {
            s->frame[i] = rbp[(i & ~(size_t)3) + (3 - (i & 3))];
        }
        if (getenv("SUBUCOM_RAMP")) {
            for (i = 0; i < sizeof(s->frame); i++) {
                s->frame[i] = (uint8_t)i;
            }
        }
    }
}

static uint32_t xdj_subucom_transfer(SSIPeripheral *dev, uint32_t val)
{
    XdjSubucom *s = XDJ_SUBUCOM(dev);
    uint8_t miso;

    if (getenv("SUBUCOM_TRACE")) {
        fprintf(stderr, "SUBUCOM [%5u] mosi=%02x\n",
                s->idx, (unsigned)(val & 0xff));
    }
    s->idx++;
    if (s->pos < sizeof(s->mosi)) {
        s->mosi[s->pos] = (uint8_t)val;
    }
    miso = (s->pos < sizeof(s->frame)) ? s->frame[s->pos] : 0x00;
    s->pos++;
    return miso;
}

static void xdj_subucom_realize(SSIPeripheral *dev, Error **errp)
{
    g_xdj_subucom = XDJ_SUBUCOM(dev);
}

static void xdj_subucom_class_init(ObjectClass *klass, const void *data)
{
    SSIPeripheralClass *k = SSI_PERIPHERAL_CLASS(klass);
    k->realize  = xdj_subucom_realize;
    k->transfer = xdj_subucom_transfer;
}

static const TypeInfo xdj_subucom_info = {
    .name          = TYPE_XDJ_SUBUCOM,
    .parent        = TYPE_SSI_PERIPHERAL,
    .instance_size = sizeof(XdjSubucom),
    .class_init    = xdj_subucom_class_init,
};

static void xdj_subucom_register_types(void)
{
    type_register_static(&xdj_subucom_info);
}
type_init(xdj_subucom_register_types)
