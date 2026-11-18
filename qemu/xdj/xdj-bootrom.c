/*
 * Pioneer XDJ-RX emulation -- the i.MX6 boot ROM (NAND boot) and the MMDC.
 *
 * With XDJ_BOOT_ROM=1 CPU0 starts at the reset vector in the ROM region, as on the
 * chip, and boots the unit's own u-boot from the emulated NAND (mtd0), the way the
 * i.MX6Q boot ROM does (reference manual, "System Boot" / "NAND Flash"):
 *
 *  1. FCB search: page 0 of blocks 0..3. A valid FCB has the "FCB " fingerprint and a
 *     checksum = ~(sum of FCB bytes 4..511). kobs-ng puts the FCB at raw byte 12 of the
 *     page. It gives the page geometry, the BCH layout (metadata bytes, ECC block
 *     sizes and type), the bad-block-marker swap and Firmware1/2_startingPage.
 *  2. Firmware: pages are de-interleaved with the FCB's layout ([meta][block0][ecc0]
 *     [block1][ecc1]...), data byte BadBlockMarkerByte restored from metadata[0]
 *     (DISBBM=0). Parity isn't checked: the NAND model stores zero parity (xdj-nand.c).
 *     Firmware1 is used, Firmware2 if Firmware1 has no valid IVT. PagesInFirmware is a
 *     hint: an image read runs on to where the other copy starts.
 *  3. Image: the IVT sits at image offset 0x400. The ROM loads boot_data.length bytes
 *     of the image to boot_data.start, applies the DCD if there is one, then either
 *     jumps to the entry or, for a plugin image (boot_data.plugin = 1), calls it as
 *     plugin(&start, &bytes, &ivt_offset), loads `bytes` of the image to `start` and
 *     boots the IVT at start + ivt_offset.
 *  4. ROM services the plugin uses: the ROM version word at 0x48 and the ROM API table
 *     at 0xC0 (version < 0x12) / 0xC4, whose +8 entry is pu_irom_hwcnfg_setup
 *     (boot-device setup, nothing to do here).
 *
 * The ROM program (rom_code[] below) is a few ARM instructions; it asks this file for
 * the work through the control registers of "xdj-bootrom-ctl" (reads with an effect):
 * CTL_LOAD loads the initial image and returns its entry, CTL_PLUGIN says whether that
 * is a plugin, CTL_CONTINUE loads the image the plugin described and returns its entry.
 *
 * The MMDC (DDR controller, 0x021B0000 / 0x021B4000) is a register file in which every
 * calibration and request completes at once without errors: MDSCR.CON_ACK follows
 * CON_REQ, and the self-clearing trigger bits (ZQ, write leveling, read DQS gating, read
 * and write delay calibration, the PHY measure-unit force) read back clear. The plugin
 * runs the DDR calibration and waits on exactly these bits.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#include "qemu/osdep.h"
#include "qapi/error.h"
#include "hw/core/loader.h"
#include "system/address-spaces.h"
#include "system/memory.h"
#include "hw/arm/fsl-imx6.h"
#include "xdj.h"

/* ---------------------------------------------------------------- MMDC -- */

#define MMDC0_BASE  0x021B0000
#define MMDC1_BASE  0x021B4000
#define MMDC_SIZE   0x4000

typedef struct {
    MemoryRegion mr;
    uint32_t reg[MMDC_SIZE / 4];
} Mmdc;

static Mmdc g_mmdc[2];

/* (offset, bits that read back clear: self-clearing triggers and error flags) */
static const struct { uint32_t off, clear; } mmdc_selfclear[] = {
    { 0x800, 1u << 16 },                             /* MPZQHWCTRL.ZQ_HW_FOR      */
    { 0x808, (1u << 0) | (0xfu << 8) },              /* MPWLGCR.HW_WL_EN, errors  */
    { 0x83c, (1u << 31) | (1u << 28) | (1u << 12) }, /* MPDGCTRL0: RST_RD_FIFO,
                                                        HW_DG_EN, HW_DG_ERR      */
    { 0x840, (1u << 31) | (1u << 28) | (1u << 12) }, /* MPDGCTRL1                */
    { 0x860, (1u << 4) | 0xfu },                     /* MPRDDLHWCTL: HW_RD_DL_EN,
                                                        errors                   */
    { 0x864, (1u << 4) | 0xfu },                     /* MPWRDLHWCTL              */
    { 0x894, 1u << 0 },                              /* MPPDCMPR2? calibration go */
    { 0x8b8, 1u << 11 },                             /* MPMUR0.FRC_MSR           */
};

static uint64_t mmdc_read(void *opaque, hwaddr off, unsigned size)
{
    Mmdc *m = opaque;
    uint32_t v = m->reg[(off & (MMDC_SIZE - 1)) >> 2];

    if (off == 0x1c) {                               /* MDSCR: CON_ACK = CON_REQ */
        v = (v & ~(1u << 14)) | ((v >> 1) & (1u << 14));
    }
    return v;
}

static void mmdc_write(void *opaque, hwaddr off, uint64_t val, unsigned size)
{
    Mmdc *m = opaque;
    uint32_t v = val;
    unsigned i;

    for (i = 0; i < ARRAY_SIZE(mmdc_selfclear); i++) {
        if (mmdc_selfclear[i].off == off) {
            v &= ~mmdc_selfclear[i].clear;
        }
    }
    m->reg[(off & (MMDC_SIZE - 1)) >> 2] = v;
}

static const MemoryRegionOps mmdc_ops = {
    .read = mmdc_read,
    .write = mmdc_write,
    .endianness = DEVICE_NATIVE_ENDIAN,
    .valid = { .min_access_size = 4, .max_access_size = 4 },
};

/* ------------------------------------------------------------ boot ROM -- */

#define ROM_VERSION      0x10          /* i.MX6Q TO1.0, as the emulated ANATOP reports */
#define ROM_CODE         0x100
#define ROM_HWCNFG       0x1a0         /* pu_irom_hwcnfg_setup: returns */
#define CTL_BASE         0x0001f000    /* reserved space after the 96 KiB ROM */
#define CTL_LOAD         0x00
#define CTL_PLUGIN       0x04
#define CTL_CONTINUE     0x08
#define OCRAM_STACK      0x00907000    /* the ROM's own data/stack area ends here */
#define PLUGIN_ARGS      0x00906f00    /* start, bytes, ivt_offset */

#define IVT_TAG          0xd1
#define DCD_TAG          0xd2
#define IVT_OFFSET_NAND  0x400

typedef struct {
    MemoryRegion ctl;
    /* FCB */
    uint32_t page_data, page_total, ppb;
    uint32_t meta, blk0, blkn, nblocks, ecc0, eccn;
    uint32_t fw_page[2], fw_pages[2];
    uint32_t bbm_byte, disbbm;
    int fw;                            /* firmware copy in use, -1 = none */
    bool plugin;
    char err[96];
} BootRom;

static BootRom g_rom;

static bool rom_enabled(void)
{
    const char *e = getenv("XDJ_BOOT_ROM");
    return e && *e && strcmp(e, "0") != 0;
}

/* BCH parity bytes for an i.MX6 ECC type (t = 2 * type, GF(2^13)) */
static uint32_t ecc_bytes(uint32_t type)
{
    return (2 * type * 13 + 7) / 8;
}

static bool fcb_parse(BootRom *r, const uint8_t *raw)
{
    const uint8_t *f = raw + 12;
    uint32_t sum = 0, i;

    if (memcmp(f + 4, "FCB ", 4) != 0) {
        return false;
    }
    for (i = 4; i < 512; i++) {
        sum += f[i];
    }
    if (ldl_le_p(f) != ~sum) {
        return false;
    }
    /* BootBlockStruct_t (i.MX6): the NAND timing block is 8 bytes */
    r->page_data  = ldl_le_p(f + 0x14);
    r->page_total = ldl_le_p(f + 0x18);
    r->ppb        = ldl_le_p(f + 0x1c);
    r->eccn       = ecc_bytes(ldl_le_p(f + 0x2c));   /* EccBlockNEccType   */
    r->blk0       = ldl_le_p(f + 0x30);              /* EccBlock0Size      */
    r->blkn       = ldl_le_p(f + 0x34);              /* EccBlockNSize      */
    r->ecc0       = ecc_bytes(ldl_le_p(f + 0x38));   /* EccBlock0EccType   */
    r->meta       = ldl_le_p(f + 0x3c);              /* MetadataBytes      */
    r->nblocks    = ldl_le_p(f + 0x40) + 1;          /* NumEccBlocksPerPage + block 0 */
    r->fw_page[0] = ldl_le_p(f + 0x68);              /* Firmware1_startingPage */
    r->fw_page[1] = ldl_le_p(f + 0x6c);              /* Firmware2_startingPage */
    r->fw_pages[0] = ldl_le_p(f + 0x70);             /* PagesInFirmware1   */
    r->fw_pages[1] = ldl_le_p(f + 0x74);             /* PagesInFirmware2   */
    r->bbm_byte   = ldl_le_p(f + 0x7c);              /* BadBlockMarkerByte */
    r->disbbm     = ldl_le_p(f + 0xac);              /* DISBBM             */
    if (r->page_data != 2048 || r->page_total != 2112 || !r->blk0 || !r->blkn ||
        r->meta + r->blk0 + r->ecc0 + (r->nblocks - 1) * (r->blkn + r->eccn) > r->page_total) {
        return false;
    }
    return true;
}

/* One firmware page, de-interleaved per the FCB. */
static bool fw_page(BootRom *r, uint32_t page, uint8_t *out)
{
    const uint8_t *raw = xdj_nand_raw_page(page);
    uint32_t pos = r->meta, n = 0, b;

    if (!raw) {
        return false;
    }
    for (b = 0; b < r->nblocks; b++) {
        uint32_t len = b ? r->blkn : r->blk0;
        memcpy(out + n, raw + pos, len);
        n += len;
        pos += len + (b ? r->eccn : r->ecc0);
    }
    if (!r->disbbm && r->bbm_byte < n) {
        out[r->bbm_byte] = raw[0];       /* the swapped data byte lives in metadata[0] */
    }
    return true;
}

/* Copy image bytes [off, off+len) of the firmware copy in use to guest address dst. */
static bool fw_copy(BootRom *r, uint32_t off, uint32_t len, hwaddr dst)
{
    uint8_t buf[2048];
    uint32_t ps = r->page_data;
    /* PagesInFirmware is a hint (the plugin can ask for more, e.g. the image measured
     * from 1 KiB before its IVT): read on, up to where the other copy starts. */
    uint32_t span = r->fw_page[1] > r->fw_page[0] ? r->fw_page[1] - r->fw_page[0]
                                                  : r->fw_pages[r->fw];

    while (len) {
        uint32_t page = off / ps, in = off % ps, n = MIN(len, ps - in);
        if (page >= span || !fw_page(r, r->fw_page[r->fw] + page, buf)) {
            fprintf(stderr, "xdj-bootrom: firmware%d page %u (NAND page %u) out of range\n",
                    r->fw + 1, page, r->fw_page[r->fw] + page);
            return false;
        }
        address_space_write(&address_space_memory, dst, MEMTXATTRS_UNSPECIFIED, buf + in, n);
        off += n;
        dst += n;
        len -= n;
    }
    return true;
}

static uint32_t rd32(hwaddr a)
{
    return address_space_ldl_le(&address_space_memory, a, MEMTXATTRS_UNSPECIFIED, NULL);
}

/* DCD: write-data commands (tag 0xcc) are applied; check-data (0xcf) can't fail here. */
static void dcd_apply(hwaddr dcd)
{
    uint32_t hdr = rd32(dcd), len, pos;

    if ((hdr & 0xff) != DCD_TAG) {
        return;
    }
    len = ((hdr >> 8) & 0xff) << 8 | ((hdr >> 16) & 0xff);   /* big-endian length */
    for (pos = 4; pos + 4 <= len;) {
        uint32_t ch = rd32(dcd + pos), clen, i;
        uint8_t tag = ch & 0xff, par = (ch >> 24) & 0xff;
        clen = ((ch >> 8) & 0xff) << 8 | ((ch >> 16) & 0xff);
        if (clen < 4) {
            break;
        }
        if (tag == 0xcc) {
            for (i = 4; i + 8 <= clen; i += 8) {
                uint32_t a = bswap32(rd32(dcd + pos + i)), v = bswap32(rd32(dcd + pos + i + 4));
                if ((par & 7) == 4) {
                    address_space_stl_le(&address_space_memory, a, v, MEMTXATTRS_UNSPECIFIED, NULL);
                }
            }
        }
        pos += clen;
    }
}

/* Boot the IVT at guest address ivt: returns its entry, 0 if invalid. */
static uint32_t ivt_boot(hwaddr ivt, bool *plugin)
{
    uint32_t hdr = rd32(ivt), entry = rd32(ivt + 4), dcd = rd32(ivt + 12);
    uint32_t bd = rd32(ivt + 16), self = rd32(ivt + 20);

    if ((hdr & 0xff) != IVT_TAG || self != ivt) {
        return 0;
    }
    if (dcd) {
        dcd_apply(dcd);
    }
    *plugin = bd && (rd32(bd + 8) & 1);
    return entry;
}

/* CTL_LOAD: FCB, then the initial image of Firmware1 (Firmware2 as fallback). */
static uint32_t rom_load(BootRom *r)
{
    uint8_t buf[2048];
    int blk, c;

    r->fw = -1;
    for (blk = 0; blk < 4; blk++) {
        const uint8_t *raw = xdj_nand_raw_page(blk * 64);
        if (raw && fcb_parse(r, raw)) {
            break;
        }
    }
    if (blk == 4) {
        snprintf(r->err, sizeof(r->err), "no valid FCB in blocks 0-3");
        return 0;
    }
    for (c = 0; c < 2; c++) {
        uint32_t start, length, entry;
        r->fw = c;
        if (!fw_page(r, r->fw_page[c], buf) || buf[IVT_OFFSET_NAND] != IVT_TAG) {
            continue;
        }
        /* boot_data, read from the image itself (IVT fields are absolute addresses) */
        uint32_t self = ldl_le_p(buf + IVT_OFFSET_NAND + 20), bd = ldl_le_p(buf + IVT_OFFSET_NAND + 16);
        uint32_t bdo = bd - (self - IVT_OFFSET_NAND);
        if (bdo + 12 > sizeof(buf)) {
            continue;
        }
        start = ldl_le_p(buf + bdo);
        length = ldl_le_p(buf + bdo + 4);
        if (start + IVT_OFFSET_NAND != self || !fw_copy(r, 0, length, start)) {
            continue;
        }
        entry = ivt_boot(self, &r->plugin);
        if (entry) {
            fprintf(stderr, "xdj-bootrom: FCB ok, firmware%d at page %u: %u bytes -> %#x, %s %#x\n",
                    c + 1, r->fw_page[c], length, start, r->plugin ? "plugin" : "entry", entry);
            return entry;
        }
    }
    r->fw = -1;
    snprintf(r->err, sizeof(r->err), "no bootable firmware (pages %u, %u)", r->fw_page[0], r->fw_page[1]);
    return 0;
}

/* CTL_CONTINUE: after the plugin, load what it described and boot that IVT. */
static uint32_t rom_continue(BootRom *r)
{
    uint32_t start = rd32(PLUGIN_ARGS), bytes = rd32(PLUGIN_ARGS + 4), off = rd32(PLUGIN_ARGS + 8);
    uint32_t entry;
    bool plugin;

    if (r->fw < 0 || !fw_copy(r, 0, bytes, start)) {
        snprintf(r->err, sizeof(r->err), "plugin image %#x+%#x unreadable", start, bytes);
        return 0;
    }
    entry = ivt_boot(start + off, &plugin);
    if (!entry) {
        snprintf(r->err, sizeof(r->err), "no IVT at %#x after the plugin", start + off);
        return 0;
    }
    fprintf(stderr, "xdj-bootrom: plugin done: %u bytes -> %#x, IVT %#x, entry %#x\n",
            bytes, start, start + off, entry);
    return entry;
}

static uint64_t ctl_read(void *opaque, hwaddr off, unsigned size)
{
    BootRom *r = opaque;
    uint32_t v = 0;

    r->err[0] = 0;
    switch (off) {
    case CTL_LOAD:
        v = rom_load(r);
        break;
    case CTL_PLUGIN:
        return r->plugin;
    case CTL_CONTINUE:
        v = rom_continue(r);
        break;
    }
    if (!v && r->err[0]) {
        fprintf(stderr, "xdj-bootrom: %s - the ROM stops (no USB serial download here)\n", r->err);
    }
    return v;
}

static void ctl_write(void *opaque, hwaddr off, uint64_t val, unsigned size)
{
}

static const MemoryRegionOps ctl_ops = {
    .read = ctl_read,
    .write = ctl_write,
    .endianness = DEVICE_NATIVE_ENDIAN,
    .valid = { .min_access_size = 4, .max_access_size = 4 },
};

/* ARM encodings for the ROM program */
#define A_LDR(rd, rn, off) (0xe5900000u | ((rn) << 16) | ((rd) << 12) | (off))
#define A_CMP0(rn)         (0xe3500000u | ((rn) << 16))
#define A_MOV(rd, rm)      (0xe1a00000u | ((rd) << 12) | (rm))
#define A_BX(rm)           (0xe12fff10u | (rm))
#define A_BXEQ(rm)         (0x012fff10u | (rm))
#define A_BLX(rm)          (0xe12fff30u | (rm))
#define A_HALT             0xeafffffeu                           /* b . */
#define A_B(from, to)      (0xea000000u | ((((to) - (from) - 8) >> 2) & 0xffffff))

static void rom_install(void)
{
    static uint32_t rom[0x200 / 4];
    static const uint32_t lits[] = {
        OCRAM_STACK, CTL_BASE, PLUGIN_ARGS, PLUGIN_ARGS + 4, PLUGIN_ARGS + 8,
    };
    enum { L_SP, L_CTL, L_ARG0, L_ARG1, L_ARG2 };
    uint32_t code[32];
    int n = 0, i, lit0;

    /* ldr rd, [pc, #off] loading lits[l]: holds l until the offset is patched below */
#define LDR_LIT(rd, l)  (code[n++] = 0xe59f0000u | ((rd) << 12) | (l))
    LDR_LIT(13, L_SP);                    /* sp = top of the ROM's OCRAM area      */
    LDR_LIT(4, L_CTL);                    /* r4 = control registers                */
    code[n++] = A_LDR(0, 4, CTL_LOAD);    /* r0 = entry of the initial image       */
    code[n++] = A_CMP0(0);
    code[n++] = 0x0afffffe;               /* beq . : nothing bootable              */
    code[n++] = A_LDR(5, 4, CTL_PLUGIN);
    code[n++] = A_CMP0(5);
    code[n++] = A_BXEQ(0);                /* not a plugin: jump to the entry       */
    code[n++] = A_MOV(6, 0);
    LDR_LIT(0, L_ARG0);                   /* r0 = &start                           */
    LDR_LIT(1, L_ARG1);                   /* r1 = &bytes                           */
    LDR_LIT(2, L_ARG2);                   /* r2 = &ivt_offset                      */
    code[n++] = A_BLX(6);                 /* plugin(&start, &bytes, &ivt_offset)   */
    code[n++] = A_LDR(0, 4, CTL_CONTINUE);/* r0 = entry of the image it described  */
    code[n++] = A_CMP0(0);
    code[n++] = 0x0afffffe;               /* beq .                                 */
    code[n++] = A_BX(0);
#undef LDR_LIT
    lit0 = n;
    for (i = 0; i < n; i++) {             /* resolve the literal loads             */
        if ((code[i] & 0xffff0000u) == 0xe59f0000u) {
            uint32_t l = code[i] & 0xff;
            uint32_t off = (lit0 + l) * 4 - (i * 4 + 8);
            code[i] = (code[i] & 0xfffff000u) | off;
        }
    }
    for (i = 0; i < 8; i++) {             /* vectors: reset -> code, others halt   */
        rom[i] = i ? A_HALT : A_B(0, ROM_CODE);
    }
    rom[0x48 / 4] = ROM_VERSION;
    /* ROM API tables at 0xC0 (version < 0x12) and 0xC4: +8 = pu_irom_hwcnfg_setup */
    rom[(0xc0 + 8) / 4] = ROM_HWCNFG;
    rom[(0xc4 + 8) / 4] = ROM_HWCNFG;
    rom[ROM_HWCNFG / 4] = A_BX(14);       /* pu_irom_hwcnfg_setup: bx lr          */
    assert(ROM_CODE + (n + ARRAY_SIZE(lits)) * 4 <= ROM_HWCNFG);
    memcpy(&rom[ROM_CODE / 4], code, n * 4);
    memcpy(&rom[ROM_CODE / 4 + n], lits, sizeof(lits));
    rom_add_blob_fixed("xdj-bootrom", rom, sizeof(rom), FSL_IMX6_ROM_ADDR);
}

void xdj_bootrom_realize(FslIMX6State *s)
{
    int i;

    for (i = 0; i < 2; i++) {
        memory_region_init_io(&g_mmdc[i].mr, NULL, &mmdc_ops, &g_mmdc[i],
                              i ? "xdj.mmdc1" : "xdj.mmdc0", MMDC_SIZE);
        memory_region_add_subregion(get_system_memory(), i ? MMDC1_BASE : MMDC0_BASE,
                                    &g_mmdc[i].mr);
    }
    if (!rom_enabled()) {
        return;
    }
    memory_region_init_io(&g_rom.ctl, NULL, &ctl_ops, &g_rom, "xdj.bootrom-ctl", 0x100);
    memory_region_add_subregion(get_system_memory(), CTL_BASE, &g_rom.ctl);
    rom_install();
    fprintf(stderr, "xdj-bootrom: NAND boot through the boot ROM (XDJ_BOOT_ROM)\n");
}
