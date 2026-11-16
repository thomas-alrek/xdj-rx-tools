/* xdjmod core internals (not for feature modules). SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef XDJMOD_INTERNAL_H
#define XDJMOD_INTERNAL_H

int  xm_core_sites_ok(void);      /* menu.c: the core's own patch sites hold 2.21's words */
void xm_cfg_load(void);           /* config.c */
void xm_cfg_reset(void);          /* config.c: forget every saved value */
void xm_menu_begin(void);         /* menu.c */
void xm_menu_install(void);       /* menu.c: point rbp at the extended UTILITY table */
int  xm_menu_rows(void);
void xm_menu_apply_all(void);     /* menu.c: apply(choice) for every row */
void xm_factory_reset_install(void);

#endif
