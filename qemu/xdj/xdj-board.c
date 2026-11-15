/*
 * Pioneer XDJ-RX emulation -- SoC glue: GPIO reset levels and captures, realize dispatcher
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

/* GPIO bank i: reset levels (key/fault pull-ups, update strap) before realize */
void xdj_imx6_gpio_pre_realize(FslIMX6State *s, int bank)
{
    {
        /*
         * XDJ-RX: the two USB-storage "device present" detect lines idle HIGH
         * (external pull-up; pulled LOW only when a USB drive is inserted), so
         * reset them high. Without this they read 0 ("USB present"); rbp's
         * UsbStorageManager then tries to push an initial USB state to the
         * front-panel MCU during UI init -- before the panel poll thread has
         * been started -- and blocks there forever, so the panel (buttons/jogs)
         * never comes up. deck1 = GPIO4_IO30 (gpiodrv id 126/0x7e), deck2 =
         * GPIO7_IO12 (id 204/0xcc).
         */
        /*
         * Idle HIGH out of reset: the USB-detect lines (above) AND the 8
         * MAIN-UCOM "direct-input" key GPIOs (USB1/USB2/rekordbox/MIDI/DECK/TIME/
         * jack switches -- all external switches pulled to +5, pressed = LOW).
         * Setting them high at RESET (rather than driving them high at runtime)
         * avoids a boot-time LOW->HIGH edge that rbp's key layer would see as a
         * spurious release during USB init. Key GPIOs: GPIO4_IO23/24,
         * GPIO5_IO18/21/30/31, GPIO6_IO00/01 (Linux ids 119,120,146,149,158,159,
         * 160,161).
         */
        if (bank == 2) {        /* GPIO3_IO30 (gpio 94): EUP update request */
            /* The sub-ucom (EUP) MCU raises this line when USB STOP + RELOOP/EXIT
             * (deck 1) are held at power-on; the initramfs /init reads it
             * (regio T,D,020A4000,40000000) and enters USB firmware-update mode.
             * XDJ_BOOT_UPDATE=1 boots with it HIGH (opt-in; default LOW = normal). */
            const char *u = getenv("XDJ_BOOT_UPDATE");
            if (u && u[0] && u[0] != '0') {
                object_property_set_uint(OBJECT(&s->gpio[bank]), "psr-reset",
                                         1u << 30, &error_abort);
            }
        } else if (bank == 3) { /* GPIO4: USB1 detect (30) + keys 23,24 */
            object_property_set_uint(OBJECT(&s->gpio[bank]), "psr-reset",
                                     (1u << 30) | (1u << 23) | (1u << 24),
                                     &error_abort);
        } else if (bank == 4) { /* GPIO5: keys 18,21,30,31 */
            object_property_set_uint(OBJECT(&s->gpio[bank]), "psr-reset",
                                     (1u << 18) | (1u << 21) | (1u << 30) |
                                     (1u << 31), &error_abort);
        } else if (bank == 5) { /* GPIO6: keys 00,01 */
            object_property_set_uint(OBJECT(&s->gpio[bank]), "psr-reset",
                                     (1u << 0) | (1u << 1), &error_abort);
        } else if (bank == 6) { /* GPIO7: USB2 detect (GPIO7_IO12) */
            object_property_set_uint(OBJECT(&s->gpio[bank]), "psr-reset",
                                     1u << 12, &error_abort);
        }
    }
}

/* GPIO bank i: capture banks for the panel harness, wire rbp's LED outputs */
void xdj_imx6_gpio_post_realize(FslIMX6State *s, int bank)
{
    {
        if (bank == 3) {        /* GPIO4: USB1 detect (GPIO4_IO30) */
            g_xdj_usb_gpio[0] = DEVICE(&s->gpio[bank]);
        } else if (bank == 6) { /* GPIO7: USB2 detect (GPIO7_IO12) */
            g_xdj_usb_gpio[1] = DEVICE(&s->gpio[bank]);
        }
        if (bank < 8) {         /* capture every bank for the MAIN-UCOM key hook */
            g_xdj_gpio[bank] = DEVICE(&s->gpio[bank]);
        }
        if (bank == 0) {        /* GPIO1_IO18 = host REQ of the panel MCU's bootloader handshake */
            xdj_subucom_bl_wire(DEVICE(&s->gpio[bank]));
        }
        if (bank == 5) {        /* GPIO6_IO02..05 = source-key LEDs (rbp outputs) */
            int pin;
            for (pin = 2; pin <= 5; pin++) {
                qdev_connect_gpio_out(DEVICE(&s->gpio[bank]), pin,
                                      qemu_allocate_irq(xdj_srcled_set, NULL, pin - 2));
            }
        }
    }
}

/*
 * Everything else the XDJ-RX needs on the SoC, called once from
 * fsl_imx6_realize() after the stock devices. The front-panel MCU SSI slave
 * is attached to ECSPI2 by the machine (hw/arm/sabrelite.c).
 */
/*
 * Boot-mode straps (SRC_SBMR1, read via the weak hook in hw/misc/imx6_src.c).
 * BOOT_CFG1[7:4] >= 8 = NAND boot: the firmware's fw_printenv then uses the NAND u-boot
 * env (/dev/mtd2) and bootmod reports BOOTMODE=NAND, so apl_start mounts the UBI data /
 * gui volumes -- the real unit's configuration. XDJ_BOOT_MEDIA=nand selects it; unset
 * keeps 0, which the legacy SD test bed (qemu/build-nand.py) is built around.
 */
uint32_t xdj_src_sbmr1(void)
{
    const char *m = getenv("XDJ_BOOT_MEDIA");
    return (m && !strcmp(m, "nand")) ? 0x80 : 0;
}

void xdj_imx6_realize(FslIMX6State *s, DeviceState *dev, DeviceState *gic)
{
    xdj_mfi_realize(gic);
    xdj_usbnc_realize(s, dev);
    xdj_display_realize(s, dev, gic);
    xdj_audio_realize(dev, gic);
    xdj_nand_realize(dev, gic);
}
