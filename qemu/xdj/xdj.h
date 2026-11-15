/*
 * Pioneer XDJ-RX emulation -- shared interface of the qemu/xdj/ device models.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#ifndef HW_ARM_XDJ_H
#define HW_ARM_XDJ_H

#include "hw/arm/fsl-imx6.h"

/* hooks called from hw/arm/fsl-imx6.c (fsl_imx6_realize) */
void xdj_imx6_gpio_pre_realize(FslIMX6State *s, int bank);
void xdj_imx6_gpio_post_realize(FslIMX6State *s, int bank);
void xdj_imx6_realize(FslIMX6State *s, DeviceState *dev, DeviceState *gic);

/* per-module realize (xdj-board.c calls these) */
void xdj_mfi_realize(DeviceState *gic);
void xdj_usbnc_realize(FslIMX6State *s, DeviceState *dev);
void xdj_display_realize(FslIMX6State *s, DeviceState *dev, DeviceState *gic);
void xdj_audio_realize(DeviceState *dev, DeviceState *gic);

/* panel harness state shared with the GPIO glue (xdj-panel.c) */
extern DeviceState *g_xdj_usb_gpio[2];
extern DeviceState *g_xdj_gpio[8];
void xdj_srcled_set(void *opaque, int n, int level);
void xdj_subucom_bl_wire(DeviceState *gpio1);   /* GPIO1_IO18 = bootloader REQ */

/* called from stock QEMU files patched by qemu/patches/ */
void xdj_dvfs_gpt_kick(void);          /* hw/misc/imx6_ccm.c */
void xdj_subucom_frame_start(void);    /* hw/ssi/imx_spi.c   */

#endif
