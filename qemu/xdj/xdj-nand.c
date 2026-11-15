/*
 * Pioneer XDJ-RX emulation -- i.MX6 raw NAND: APBH-DMA + GPMI + BCH + a NAND chip.
 *
 * The firmware keeps its system on a 256 MiB SLC NAND (mtdparts from the u-boot env:
 * boot, bb1, env, kernel_a/b, rootfs, data, pioneer, bb2, core, gui, quickboot, ...),
 * driven by the kernel's gpmi-nand + mxs-dma (APBH) drivers. The firmware updater
 * writes it with flash_erase / nandwrite / kobs-ng / ubiformat. Stock QEMU has none
 * of these blocks, so the drivers' probes fail and /dev/mtd* never appear.
 *
 * Modelled to the extent the 3.0.35 drivers use the hardware (GPL source,
 * drivers/dma/mxs-dma.c, drivers/mtd/nand/gpmi-nand/):
 *  - APBH-DMA (0x00110000, SPI 13): 16 channels of CCW chains -- PIO words into the GPMI
 *    registers, CMD_READ (memory -> device) / CMD_WRITE (device -> memory) data phases,
 *    CHAIN / DEC_SEM / IRQ, SEMA kick, CTRL1 completion IRQs. Runs synchronously in the
 *    SEMA write (the transfer is instant).
 *  - GPMI (0x00112000): CTRL0 starts an operation -- CLE (+ADDRESS_INCREMENT -> ALE)
 *    command/address bytes, raw data in/out, wait-for-ready; with ECCCTRL.ENABLE_ECC
 *    the page goes through the BCH engine instead.
 *  - BCH (0x00114000, SPI 15): FLASH0LAYOUT page layout [meta][data0][ecc0][data1]..
 *    (bit-packed, LSB first, like the hardware -- gpmi-nand's block-mark swap relies on
 *    it), PAYLOAD/AUXILIARY buffers, per-chunk status 0x00 good / 0xff erased,
 *    COMPLETE_IRQ. Parity bits are written as 0: the kernel never sees them (we report
 *    no bit errors); only the boot ROM would check them.
 *  - The chip: Samsung K9F2G08U0C-style, ID EC DA 10 95 44 (non-ONFI): 2048+64-byte
 *    pages, 64 pages/block, 2048 blocks = 256 MiB; reset, read ID, read (00/30),
 *    random data out (05/E0), program (80/85/10), erase (60/D0), status (70).
 *    Program ANDs into the array, erase sets 0xff.
 *  Backing store: $XDJ_NAND_IMG, raw pages with OOB (2112 bytes each, page-major),
 *  created erased (0xff) if missing. Without it no chip answers (like an empty socket).
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#include "qemu/osdep.h"
#include "qapi/error.h"
#include "hw/core/irq.h"
#include "system/address-spaces.h"
#include "hw/arm/fsl-imx6.h"
#include "xdj.h"
#include <sys/mman.h>

#define APBH_BASE   0x00110000
#define GPMI_BASE   0x00112000
#define BCH_BASE    0x00114000
#define APBH_IRQ    13
#define BCH_IRQ     15
#define GPMI_IRQ    16

#define SFTRST      (1u << 31)
#define CLKGATE     (1u << 30)

/* NAND geometry */
#define NAND_PAGE   2048
#define NAND_OOB    64
#define NAND_RAW    (NAND_PAGE + NAND_OOB)
#define NAND_PPB    64
#define NAND_BLOCKS 2048
#define NAND_PAGES  (NAND_PPB * NAND_BLOCKS)
static const uint8_t nand_id[5] = { 0xec, 0xda, 0x10, 0x95, 0x44 };

/* CCW bits (mxs-dma.c) */
#define CCW_CMD(b)      ((b) & 3)
#define CCW_CHAIN       (1 << 2)
#define CCW_IRQ         (1 << 3)
#define CCW_DEC_SEM     (1 << 6)
#define CCW_PIO_NUM(b)  (((b) >> 12) & 0xf)
#define DMA_NO_XFER     0
#define DMA_WRITE       1      /* device -> memory */
#define DMA_READ        2      /* memory -> device */

enum { N_IDLE, N_READ, N_RNDOUT, N_PROG, N_RNDIN, N_ERASE, N_READID };
enum { O_NONE, O_PAGE, O_ID, O_STATUS };

typedef struct {
    /* APBH */
    uint32_t apbh_ctrl[4];               /* CTRL0, CTRL1, CTRL2, CHANNEL_CTRL */
    uint32_t apbh_misc[0x100 / 4];       /* 0x040..0x0ff */
    struct {
        uint32_t cur, nxt, cmd, bar, sema, dbg1, dbg2;
    } ch[16];
    /* GPMI / BCH register files */
    uint32_t gpmi[0x140 / 4];
    uint32_t bch[0x200 / 4];
    /* current GPMI raw operation (fed by DMA data phases) */
    int op_mode, op_addr, op_incr, op_first;
    uint32_t op_left;
    /* chip */
    uint8_t *array;                      /* mmapped image, NULL = no chip */
    uint8_t reg[NAND_RAW];               /* page register */
    int pend, out;
    uint8_t ab[5];
    int nab;
    uint32_t col, row;
    int idx;
    uint8_t status;
    qemu_irq apbh_irq, bch_irq, gpmi_irq;
    MemoryRegion mr_apbh, mr_gpmi, mr_bch;
    bool log;
} XdjNand;

static XdjNand g_nand;

/* ---------------------------------------------------------------- the chip ---- */

static void nand_cle(XdjNand *s, uint8_t c)
{
    s->nab = 0;
    switch (c) {
    case 0xff:                                   /* reset */
        s->pend = N_IDLE;
        s->out = O_NONE;
        s->status = 0xe0;
        break;
    case 0x90:                                   /* read ID (+1 address byte) */
        s->pend = N_READID;
        s->out = O_ID;
        s->idx = 0;
        break;
    case 0x00:
        s->pend = N_READ;
        break;
    case 0x30:                                   /* read confirm: page -> register */
        if (s->pend == N_READ && s->array && s->row < NAND_PAGES) {
            memcpy(s->reg, s->array + (size_t)s->row * NAND_RAW, NAND_RAW);
            s->out = O_PAGE;
        }
        s->pend = N_IDLE;
        break;
    case 0x05:
        s->pend = N_RNDOUT;
        break;
    case 0xe0:
        if (s->pend == N_RNDOUT) {
            s->out = O_PAGE;
        }
        s->pend = N_IDLE;
        break;
    case 0x80:                                   /* serial data input */
        memset(s->reg, 0xff, sizeof(s->reg));
        s->pend = N_PROG;
        s->out = O_NONE;
        break;
    case 0x85:
        s->pend = N_RNDIN;
        break;
    case 0x10:
    case 0x15:                                   /* program (cache program = same) */
        if ((s->pend == N_PROG || s->pend == N_RNDIN) && s->array &&
            s->row < NAND_PAGES) {
            uint8_t *p = s->array + (size_t)s->row * NAND_RAW;
            int i;
            for (i = 0; i < NAND_RAW; i++) {
                p[i] &= s->reg[i];
            }
            s->status = 0xe0;
        } else {
            s->status = 0xe1;
        }
        s->pend = N_IDLE;
        break;
    case 0x60:
        s->pend = N_ERASE;
        break;
    case 0xd0:
        if (s->pend == N_ERASE && s->array && s->row < NAND_PAGES) {
            uint32_t first = s->row & ~(NAND_PPB - 1);
            memset(s->array + (size_t)first * NAND_RAW, 0xff,
                   (size_t)NAND_PPB * NAND_RAW);
            s->status = 0xe0;
        } else {
            s->status = 0xe1;
        }
        s->pend = N_IDLE;
        break;
    case 0x70:
        s->out = O_STATUS;
        break;
    default:
        if (s->log) {
            fprintf(stderr, "xdj-nand: unhandled command %02x\n", c);
        }
        break;
    }
}

static void nand_ale(XdjNand *s, uint8_t a)
{
    if (s->nab < 5) {
        s->ab[s->nab] = a;
    }
    s->nab++;
    switch (s->pend) {
    case N_READID:
        s->idx = 0;
        break;
    case N_READ:
    case N_PROG:
        if (s->nab == 2) {
            s->col = s->ab[0] | (s->ab[1] << 8);
        } else if (s->nab == 5) {
            s->row = s->ab[2] | (s->ab[3] << 8) | ((uint32_t)s->ab[4] << 16);
        }
        break;
    case N_RNDOUT:
    case N_RNDIN:
        if (s->nab == 2) {
            s->col = s->ab[0] | (s->ab[1] << 8);
        }
        break;
    case N_ERASE:
        if (s->nab == 3) {
            s->row = s->ab[0] | (s->ab[1] << 8) | ((uint32_t)s->ab[2] << 16);
        }
        break;
    }
}

static uint8_t nand_rd(XdjNand *s)
{
    if (!s->array) {
        return 0xff;                             /* nothing on the bus */
    }
    switch (s->out) {
    case O_PAGE:
        return s->col < NAND_RAW ? s->reg[s->col++] : 0xff;
    case O_ID:
        return nand_id[s->idx++ % 5];
    case O_STATUS:
        return s->status;
    }
    return 0xff;
}

static void nand_wr(XdjNand *s, uint8_t v)
{
    if ((s->pend == N_PROG || s->pend == N_RNDIN) && s->col < NAND_RAW) {
        s->reg[s->col++] = v;
    }
}

/* ---------------------------------------------------------------- GPMI ops ---- */

static void gpmi_data_out(XdjNand *s, const uint8_t *buf, uint32_t n)
{
    uint32_t i;
    for (i = 0; i < n && s->op_left; i++, s->op_left--) {
        if (s->op_addr == 1 && (s->op_first || !s->op_incr)) {   /* CLE */
            nand_cle(s, buf[i]);
        } else if (s->op_addr == 1 || s->op_addr == 2) {          /* ALE */
            nand_ale(s, buf[i]);
        } else {
            nand_wr(s, buf[i]);
        }
        s->op_first = 0;
    }
}

static void gpmi_data_in(XdjNand *s, uint8_t *buf, uint32_t n)
{
    uint32_t i;
    for (i = 0; i < n; i++) {
        buf[i] = s->op_left ? nand_rd(s) : 0xff;
        if (s->op_left) {
            s->op_left--;
        }
    }
}

/* bit-packed stream helpers (LSB first, like the BCH layout) */
static void bits_copy(uint8_t *dst, uint32_t doff, const uint8_t *src, uint32_t soff,
                      uint32_t nbits)
{
    uint32_t i;
    for (i = 0; i < nbits; i++, doff++, soff++) {
        int b = (src[soff >> 3] >> (soff & 7)) & 1;
        dst[doff >> 3] = (dst[doff >> 3] & ~(1 << (doff & 7))) | (b << (doff & 7));
    }
}

static bool bits_all_ones(const uint8_t *src, uint32_t off, uint32_t nbits)
{
    uint32_t i;
    for (i = 0; i < nbits; i++, off++) {
        if (!((src[off >> 3] >> (off & 7)) & 1)) {
            return false;
        }
    }
    return true;
}

static void bch_update_irq(XdjNand *s)
{
    uint32_t c = s->bch[0];
    qemu_set_irq(s->bch_irq, (c & 1) && (c & (1 << 8)));
}

/* One BCH page operation (decode = read from chip, encode = write to chip). */
static void bch_page(XdjNand *s, bool encode)
{
    uint32_t l0 = s->bch[0x80 / 4], l1 = s->bch[0x90 / 4];
    uint32_t nblocks = (l0 >> 24) + 1;
    uint32_t meta = (l0 >> 16) & 0xff;
    uint32_t gf0 = (l0 & (1 << 10)) ? 14 : 13, gfn = (l1 & (1 << 10)) ? 14 : 13;
    uint32_t ecc0 = ((l0 >> 11) & 0x1f) * 2 * gf0;   /* bits */
    uint32_t eccn = ((l1 >> 11) & 0x1f) * 2 * gfn;
    uint32_t d0 = (l0 & 0x3ff) * 4, dn = (l1 & 0x3ff) * 4;
    uint32_t psize = l1 >> 16;
    uint32_t mask = s->gpmi[0x20 / 4] & 0x1ff;
    dma_addr_t pay = s->gpmi[0x40 / 4], aux = s->gpmi[0x50 / 4];
    uint32_t paylen = d0 + (nblocks - 1) * dn;
    uint32_t stat_off = (meta + 3) & ~3u;
    g_autofree uint8_t *raw = g_malloc(psize + 4);
    g_autofree uint8_t *pl = g_malloc0(paylen + 4);
    g_autofree uint8_t *ax = g_malloc0(stat_off + nblocks + 4);
    uint32_t pos = 0, b, poff = 0;

    if (!psize || psize > 0x4000 || (uint64_t)meta * 8 + paylen * 8 +
        ecc0 + (nblocks - 1) * eccn > (uint64_t)psize * 8) {
        if (s->log) {
            fprintf(stderr, "xdj-nand: bad BCH layout %08x %08x\n", l0, l1);
        }
        goto done;
    }
    if (!encode) {
        s->op_left = psize;
        gpmi_data_in(s, raw, psize);
        for (b = 0; b < nblocks; b++) {
            uint32_t dsz = b ? dn : d0, esz = b ? eccn : ecc0;
            uint32_t start = pos;
            if (b == 0) {
                bits_copy(ax, 0, raw, pos, meta * 8);
                pos += meta * 8;
            }
            bits_copy(pl, poff * 8, raw, pos, dsz * 8);
            pos += dsz * 8 + esz;
            poff += dsz;
            ax[stat_off + b] = bits_all_ones(raw, start, pos - start) ? 0xff : 0x00;
        }
        if (mask & 0xff) {
            address_space_write(&address_space_memory, pay, MEMTXATTRS_UNSPECIFIED,
                                pl, paylen);
        }
        if (mask & 0x100) {
            address_space_write(&address_space_memory, aux, MEMTXATTRS_UNSPECIFIED,
                                ax, stat_off + nblocks);
        }
    } else {
        memset(raw, 0xff, psize);
        if (mask & 0xff) {
            address_space_read(&address_space_memory, pay, MEMTXATTRS_UNSPECIFIED,
                               pl, paylen);
        } else {
            memset(pl, 0xff, paylen);
        }
        if (mask & 0x100) {
            address_space_read(&address_space_memory, aux, MEMTXATTRS_UNSPECIFIED,
                               ax, meta);
        } else {
            memset(ax, 0xff, meta);
        }
        for (b = 0; b < nblocks; b++) {
            uint32_t dsz = b ? dn : d0, esz = b ? eccn : ecc0;
            static const uint8_t zero[64];
            uint32_t k;
            if (b == 0) {
                bits_copy(raw, pos, ax, 0, meta * 8);
                pos += meta * 8;
            }
            bits_copy(raw, pos, pl, poff * 8, dsz * 8);
            pos += dsz * 8;
            poff += dsz;
            for (k = 0; k < esz; k += 8 * sizeof(zero)) {   /* parity: zeros */
                bits_copy(raw, pos + k, zero, 0, MIN(esz - k, 8 * sizeof(zero)));
            }
            pos += esz;
        }
        s->op_left = psize;
        s->op_addr = 0;
        gpmi_data_out(s, raw, psize);
    }
done:
    s->op_left = 0;
    s->bch[0] |= 1;                               /* COMPLETE_IRQ */
    bch_update_irq(s);
}

/* PIO write of GPMI CTRL0 (from a DMA CCW) starts an operation. */
static void gpmi_start(XdjNand *s)
{
    uint32_t c0 = s->gpmi[0], ecc = s->gpmi[0x20 / 4];
    int mode = (c0 >> 24) & 3;
    bool cs0 = ((c0 >> 20) & 7) == 0;

    s->op_mode = mode;
    s->op_addr = (c0 >> 17) & 3;
    s->op_incr = (c0 >> 16) & 1;
    s->op_first = 1;
    s->op_left = cs0 ? (c0 & 0xffff) : 0;
    if (mode == 3) {                              /* wait for ready: always ready */
        s->op_left = 0;
        return;
    }
    if ((ecc & (1 << 12)) && cs0 && (mode == 0 || mode == 1)) {
        bool encode = ((ecc >> 13) & 3) == 1;
        bch_page(s, encode);
    }
}

/* ---------------------------------------------------------------- APBH ------ */

static void apbh_update_irq(XdjNand *s)
{
    uint32_t c1 = s->apbh_ctrl[1], c2 = s->apbh_ctrl[2];
    qemu_set_irq(s->apbh_irq, ((c1 & (c1 >> 16)) & 0xffff) || (c2 & 0xffff));
}

static void apbh_run(XdjNand *s, int n)
{
    int guard = 4096;

    if (s->apbh_ctrl[0] & (SFTRST | CLKGATE)) {
        return;
    }
    while (s->ch[n].sema && guard--) {
        uint32_t a = s->ch[n].nxt;
        uint32_t next = address_space_ldl_le(&address_space_memory, a,
                                             MEMTXATTRS_UNSPECIFIED, NULL);
        uint32_t bits = address_space_ldl_le(&address_space_memory, a + 4,
                                             MEMTXATTRS_UNSPECIFIED, NULL);
        uint32_t bar = address_space_ldl_le(&address_space_memory, a + 8,
                                            MEMTXATTRS_UNSPECIFIED, NULL);
        uint32_t npio = CCW_PIO_NUM(bits), xfer = bits >> 16, i;

        s->ch[n].cur = a;
        s->ch[n].cmd = bits;
        s->ch[n].bar = bar;
        if (n < 4) {                              /* channels 0-3 = GPMI chip selects */
            for (i = 0; i < npio && i < 0x140 / 0x10; i++) {
                s->gpmi[i * 4] = address_space_ldl_le(&address_space_memory,
                                                      a + 12 + i * 4,
                                                      MEMTXATTRS_UNSPECIFIED, NULL);
            }
            if (npio) {
                gpmi_start(s);
            }
            if (xfer && CCW_CMD(bits) == DMA_READ) {
                g_autofree uint8_t *buf = g_malloc(xfer);
                address_space_read(&address_space_memory, bar,
                                   MEMTXATTRS_UNSPECIFIED, buf, xfer);
                gpmi_data_out(s, buf, xfer);
            } else if (xfer && CCW_CMD(bits) == DMA_WRITE) {
                g_autofree uint8_t *buf = g_malloc(xfer);
                gpmi_data_in(s, buf, xfer);
                address_space_write(&address_space_memory, bar,
                                    MEMTXATTRS_UNSPECIFIED, buf, xfer);
            }
        }
        if (bits & CCW_DEC_SEM) {
            s->ch[n].sema--;
        }
        if (bits & CCW_IRQ) {
            s->apbh_ctrl[1] |= 1u << n;
        }
        if (!(bits & CCW_CHAIN)) {
            break;
        }
        s->ch[n].nxt = next;
    }
    apbh_update_irq(s);
}

static void apbh_reset(XdjNand *s)
{
    memset(s->ch, 0, sizeof(s->ch));
    s->apbh_ctrl[1] = s->apbh_ctrl[2] = s->apbh_ctrl[3] = 0;
    apbh_update_irq(s);
}

/* SET/CLR/TOG aliases at +4/+8/+c of a "SCT" register */
static uint32_t sct(uint32_t old, uint32_t v, int alias)
{
    switch (alias) {
    case 4:  return old | v;
    case 8:  return old & ~v;
    case 12: return old ^ v;
    }
    return v;
}

/* SFTRST/CLKGATE behaviour common to the MXS blocks: SFTRST gates the clock. */
static uint32_t mxs_ctrl0(uint32_t v)
{
    return (v & SFTRST) ? (v | CLKGATE) : v;
}

static uint64_t apbh_read(void *opaque, hwaddr off, unsigned size)
{
    XdjNand *s = opaque;
    if (off < 0x40) {
        return s->apbh_ctrl[off >> 4];
    }
    if (off >= 0x100 && off < 0x100 + 16 * 0x70) {
        int n = (off - 0x100) / 0x70, r = ((off - 0x100) % 0x70) >> 4;
        switch (r) {
        case 0: return s->ch[n].cur;
        case 1: return s->ch[n].nxt;
        case 2: return s->ch[n].cmd;
        case 3: return s->ch[n].bar;
        case 4: return (s->ch[n].sema & 0xff) << 16;
        case 5: return s->ch[n].dbg1;
        case 6: return s->ch[n].dbg2;
        }
    }
    if (off == 0x800) {
        return 0x03010000;                        /* APBH version 3.1 (i.MX6) */
    }
    if (off < 0x100) {
        return s->apbh_misc[off >> 2];
    }
    return 0;
}

static void apbh_write(void *opaque, hwaddr off, uint64_t val, unsigned size)
{
    XdjNand *s = opaque;
    uint32_t v = val;
    if (off < 0x40) {
        int r = off >> 4, a = off & 0xc, k;
        uint32_t nv = sct(s->apbh_ctrl[r], v, a);
        if (r == 0) {
            nv = mxs_ctrl0(nv);
            if (nv & SFTRST) {
                apbh_reset(s);
            }
        } else if (r == 3) {                      /* CHANNEL_CTRL: reset bits self-clear */
            for (k = 0; k < 16; k++) {
                if (nv & (1u << (16 + k))) {
                    s->ch[k].sema = 0;
                }
            }
            nv &= 0xffff;
        }
        s->apbh_ctrl[r] = nv;
        apbh_update_irq(s);
        return;
    }
    if (off >= 0x100 && off < 0x100 + 16 * 0x70) {
        int n = (off - 0x100) / 0x70, r = ((off - 0x100) % 0x70) >> 4;
        if (r == 1) {
            s->ch[n].nxt = v;
        } else if (r == 4) {
            s->ch[n].sema = (s->ch[n].sema + (v & 0xff)) & 0xff;
            if (!(s->apbh_ctrl[3] & (1u << n))) {  /* not frozen */
                apbh_run(s, n);
            }
        }
        return;
    }
    if (off < 0x100) {
        s->apbh_misc[off >> 2] = v;
    }
}

/* ---------------------------------------------------------------- GPMI/BCH -- */

static uint64_t gpmi_read(void *opaque, hwaddr off, unsigned size)
{
    XdjNand *s = opaque;
    if (off == 0xb0) {                            /* STAT: all chips ready, present */
        return 0xff000001;
    }
    if (off == 0xd0) {
        return 0x05010000;                        /* GPMI version (i.MX6) */
    }
    if (off < sizeof(s->gpmi)) {
        return s->gpmi[(off & ~0xf) >> 2];
    }
    return 0;
}

static void gpmi_write(void *opaque, hwaddr off, uint64_t val, unsigned size)
{
    XdjNand *s = opaque;
    int r = (off & ~0xf) >> 2, a = off & 0xc;
    if (off >= sizeof(s->gpmi)) {
        return;
    }
    if (r == 0) {
        s->gpmi[0] = mxs_ctrl0(sct(s->gpmi[0], val, a));
    } else if (r == 0x20 / 4 || r == 0x60 / 4) {  /* ECCCTRL, CTRL1 have aliases */
        s->gpmi[r] = sct(s->gpmi[r], val, a);
    } else if (!a) {
        s->gpmi[r] = val;
    }
}

static uint64_t bch_read(void *opaque, hwaddr off, unsigned size)
{
    XdjNand *s = opaque;
    if (off < sizeof(s->bch)) {
        return s->bch[(off & ~0xf) >> 2];
    }
    return 0;
}

static void bch_write(void *opaque, hwaddr off, uint64_t val, unsigned size)
{
    XdjNand *s = opaque;
    int r = (off & ~0xf) >> 2, a = off & 0xc;
    if (off >= sizeof(s->bch)) {
        return;
    }
    if (r == 0) {
        uint32_t nv = mxs_ctrl0(sct(s->bch[0], val, a));
        if (nv & SFTRST) {
            nv &= ~1u;                            /* reset clears the IRQ */
        }
        s->bch[0] = nv;
        bch_update_irq(s);
    } else {
        s->bch[r] = sct(s->bch[r], val, a);
    }
}

static const MemoryRegionOps apbh_ops = {
    .read = apbh_read, .write = apbh_write,
    .endianness = DEVICE_NATIVE_ENDIAN,
    .valid = { .min_access_size = 4, .max_access_size = 4 },
};
static const MemoryRegionOps gpmi_ops = {
    .read = gpmi_read, .write = gpmi_write,
    .endianness = DEVICE_NATIVE_ENDIAN,
    .valid = { .min_access_size = 4, .max_access_size = 4 },
};
static const MemoryRegionOps bch_ops = {
    .read = bch_read, .write = bch_write,
    .endianness = DEVICE_NATIVE_ENDIAN,
    .valid = { .min_access_size = 4, .max_access_size = 4 },
};

static void nand_open_image(XdjNand *s)
{
    const char *path = getenv("XDJ_NAND_IMG");
    const size_t size = (size_t)NAND_PAGES * NAND_RAW;
    struct stat st;
    void *m;
    int fd;

    if (!path || !*path) {
        return;
    }
    fd = open(path, O_RDWR | O_CREAT, 0644);
    if (fd < 0 || fstat(fd, &st) < 0) {
        fprintf(stderr, "xdj-nand: cannot open %s: %s\n", path, strerror(errno));
        if (fd >= 0) {
            close(fd);
        }
        return;
    }
    if ((size_t)st.st_size < size && ftruncate(fd, size) < 0) {
        fprintf(stderr, "xdj-nand: cannot size %s\n", path);
        close(fd);
        return;
    }
    m = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    close(fd);
    if (m == MAP_FAILED) {
        fprintf(stderr, "xdj-nand: mmap %s failed\n", path);
        return;
    }
    if ((size_t)st.st_size < size) {              /* new / short image: erase the rest */
        memset((uint8_t *)m + st.st_size, 0xff, size - st.st_size);
    }
    s->array = m;
    fprintf(stderr, "xdj-nand: %s (256 MiB, 2048+64 x 64 x 2048)\n", path);
}

void xdj_nand_realize(DeviceState *dev, DeviceState *gic)
{
    XdjNand *s = &g_nand;

    s->log = getenv("XDJ_NAND_LOG") != NULL;
    s->status = 0xe0;
    s->apbh_ctrl[0] = SFTRST | CLKGATE;           /* MXS blocks come up held in reset */
    s->gpmi[0] = SFTRST | CLKGATE;
    s->bch[0] = SFTRST | CLKGATE;
    nand_open_image(s);
    s->apbh_irq = qdev_get_gpio_in(gic, APBH_IRQ);
    s->bch_irq = qdev_get_gpio_in(gic, BCH_IRQ);
    s->gpmi_irq = qdev_get_gpio_in(gic, GPMI_IRQ);
    memory_region_init_io(&s->mr_apbh, OBJECT(dev), &apbh_ops, s, "xdj.apbh-dma", 0x2000);
    memory_region_init_io(&s->mr_gpmi, OBJECT(dev), &gpmi_ops, s, "xdj.gpmi", 0x2000);
    memory_region_init_io(&s->mr_bch, OBJECT(dev), &bch_ops, s, "xdj.bch", 0x2000);
    memory_region_add_subregion_overlap(get_system_memory(), APBH_BASE, &s->mr_apbh, 1);
    memory_region_add_subregion_overlap(get_system_memory(), GPMI_BASE, &s->mr_gpmi, 1);
    memory_region_add_subregion_overlap(get_system_memory(), BCH_BASE, &s->mr_bch, 1);
}
