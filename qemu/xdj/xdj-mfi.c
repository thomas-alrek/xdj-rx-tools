/*
 * Pioneer XDJ-RX emulation -- Apple MFi authentication coprocessor (identity only) on the I2C4 controller
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
 * Auth coprocessor (E-7206 "AUTH CHIP ERROR"). The XDJ-RX carries an Apple MFi
 * authentication coprocessor on the i.MX6 I2C4 controller (0x021F8000, guest
 * /dev/i2c-3), slave 0x10. rbp's startup self-check (FUN_00349294) writes the
 * register pointer 0x00 and reads 1 byte that must be 0x05 (Device Version: CP
 * 2.0C), then register 0x01 that must be 0x01 (Firmware Version); any NACK or other
 * value -> -1 -> E-7206. QEMU's i.MX6 has no I2C4 at all, so the chip was missing.
 * This models the chip's identity registers only: a register pointer set by the
 * first written byte, auto-incrementing reads within each register block. It does
 * NOT implement challenge/response signing or carry a certificate (those registers
 * read as zero / length 0) -- only needed when an iPod/iPhone actually connects.
 */
#define TYPE_XDJ_MFI_CP "xdj-mfi-cp"
OBJECT_DECLARE_SIMPLE_TYPE(XdjMfiCp, XDJ_MFI_CP)
struct XdjMfiCp {
    I2CSlave parent_obj;
    uint8_t  reg;           /* register (block) pointer */
    uint8_t  pos;           /* byte offset within the block */
    bool     have_ptr;      /* first byte of a write = register pointer */
};

static uint8_t xdj_mfi_cp_byte(uint8_t reg, uint8_t pos)
{
    static const uint8_t dev_id[4] = { 0x00, 0x00, 0x02, 0x00 };
    switch (reg) {
    case 0x00: return pos == 0 ? 0x05 : 0;      /* Device Version (2.0C)      */
    case 0x01: return pos == 0 ? 0x01 : 0;      /* Firmware Version           */
    case 0x02: return pos == 0 ? 0x02 : 0;      /* Auth Protocol Major        */
    case 0x03: return 0x00;                     /* Auth Protocol Minor        */
    case 0x04: return pos < 4 ? dev_id[pos] : 0; /* Device ID 0x00000200      */
    case 0x40: return 0x00;                     /* Self-test status: no fault */
    default:   return 0x00;                     /* error code 0, lengths 0... */
    }
}

static int xdj_mfi_cp_event(I2CSlave *i2c, enum i2c_event ev)
{
    XdjMfiCp *s = XDJ_MFI_CP(i2c);
    if (ev == I2C_START_SEND) {
        s->have_ptr = false;
    }
    return 0;
}

static int xdj_mfi_cp_send(I2CSlave *i2c, uint8_t data)
{
    XdjMfiCp *s = XDJ_MFI_CP(i2c);
    if (!s->have_ptr) {
        s->reg = data;
        s->pos = 0;
        s->have_ptr = true;
    }                       /* register data writes (challenge etc.) ignored */
    return 0;
}

static uint8_t xdj_mfi_cp_recv(I2CSlave *i2c)
{
    XdjMfiCp *s = XDJ_MFI_CP(i2c);
    return xdj_mfi_cp_byte(s->reg, s->pos++);
}

static void xdj_mfi_cp_class_init(ObjectClass *klass, const void *data)
{
    I2CSlaveClass *k = I2C_SLAVE_CLASS(klass);
    k->event = xdj_mfi_cp_event;
    k->send  = xdj_mfi_cp_send;
    k->recv  = xdj_mfi_cp_recv;
}

static const TypeInfo xdj_mfi_cp_info = {
    .name          = TYPE_XDJ_MFI_CP,
    .parent        = TYPE_I2C_SLAVE,
    .instance_size = sizeof(XdjMfiCp),
    .class_init    = xdj_mfi_cp_class_init,
};

static void xdj_mfi_register_types(void)
{
    type_register_static(&xdj_mfi_cp_info);
}
type_init(xdj_mfi_register_types)

/* realize: I2C4 controller carrying the auth coprocessor */
void xdj_mfi_realize(DeviceState *gic)
{
    /* XDJ-RX: I2C4 (0x021F8000, GIC SPI 35; the guest's imx-i2c.3 = /dev/i2c-3)
     * carrying the MFi auth coprocessor at 0x10 -- see xdj_mfi_cp above. Mapped
     * with priority 1 over any catch-all unimplemented region. */
    {
        DeviceState *i2c4 = qdev_new(TYPE_IMX_I2C);
        I2CBus *bus;
        sysbus_realize_and_unref(SYS_BUS_DEVICE(i2c4), &error_fatal);
        sysbus_mmio_map_overlap(SYS_BUS_DEVICE(i2c4), 0, 0x021F8000, 1);
        sysbus_connect_irq(SYS_BUS_DEVICE(i2c4), 0, qdev_get_gpio_in(gic, 35));
        bus = IMX_I2C(i2c4)->bus;
        i2c_slave_create_simple(bus, TYPE_XDJ_MFI_CP, 0x10);
    }
}
