/*
 * xdjmod settings: integers by key in /root/rbp/xdjmod.conf ("key=value" lines).
 * /root/rbp is the unit's writable data volume (apl_start mounts it before rbp starts);
 * rbp's own settings stay in XdjSettings.dat, untouched. Saved atomically (temp file +
 * rename) on every change. An ALL update reformats the volume, clearing this too.
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#include "xdjmod.h"
#include "internal.h"

#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define CFG_PATH "/root/rbp/xdjmod.conf"
#define CFG_MAX  64

static struct { char key[32]; int val; } cfg[CFG_MAX];
static int ncfg;
static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;

static int find(const char *key)
{
    for (int i = 0; i < ncfg; i++)
        if (strcmp(cfg[i].key, key) == 0)
            return i;
    return -1;
}

void xm_cfg_load(void)
{
    FILE *f = fopen(CFG_PATH, "r");
    char line[96];
    ncfg = 0;
    if (!f)
        return;
    while (ncfg < CFG_MAX && fgets(line, sizeof(line), f)) {
        char *eq = strchr(line, '=');
        if (!eq || line[0] == '#')
            continue;
        *eq = 0;
        if (strlen(line) >= sizeof(cfg[0].key))
            continue;
        strcpy(cfg[ncfg].key, line);
        cfg[ncfg].val = atoi(eq + 1);
        ncfg++;
    }
    fclose(f);
}

static void save_locked(void)
{
    FILE *f = fopen(CFG_PATH ".tmp", "w");
    if (!f) {
        xm_log("cannot write " CFG_PATH);
        return;
    }
    fprintf(f, "# xdjmod settings (UTILITY rows added by the firmware mods)\n");
    for (int i = 0; i < ncfg; i++)
        fprintf(f, "%s=%d\n", cfg[i].key, cfg[i].val);
    fflush(f);
    fsync(fileno(f));
    fclose(f);
    rename(CFG_PATH ".tmp", CFG_PATH);
}

int xm_cfg_get(const char *key, int def)
{
    pthread_mutex_lock(&lock);
    int i = find(key), v = i < 0 ? def : cfg[i].val;
    pthread_mutex_unlock(&lock);
    return v;
}

void xm_cfg_set(const char *key, int val)
{
    pthread_mutex_lock(&lock);
    int i = find(key);
    if (i < 0 && ncfg < CFG_MAX && strlen(key) < sizeof(cfg[0].key)) {
        i = ncfg++;
        strcpy(cfg[i].key, key);
    }
    if (i >= 0) {
        cfg[i].val = val;
        save_locked();
    }
    pthread_mutex_unlock(&lock);
}

void xm_cfg_reset(void)
{
    pthread_mutex_lock(&lock);
    ncfg = 0;
    unlink(CFG_PATH);
    pthread_mutex_unlock(&lock);
}
