/*
 * flac mod: FLAC playback, through the wav mod's reader.
 *
 * rbp chooses a reader by file suffix in its factory (0x3dac8), asking the helper
 * 0x401d8(path, suffix) "does path end in suffix?" once per known suffix; .flac matches
 * none, so the load fails. We detour the helper: asked about ".WAV" (the literal at
 * 0x35dd04) for a path ending in ".flac", it answers yes - rbp builds a FileReadWav,
 * whose parse/read slots belong to the wav mod (wav.c). That parser recognises the
 * "fLaC" signature and decodes through the functions below (dr_flac, reading via rbp's
 * InputStream), sharing the wav mod's decimation for rates above 48 kHz.
 *
 * The browser (folder view, track info) classifies files by name in 0x1f1ce4(?, utf16 name):
 * MP3 1, AAC 2, MP4 3, M4A 4, WAV 11, AIF(F) 12, JPG 200, directory 100, else -1 (not
 * listed). Type 5 (export.pdb's FLAC) makes the whole folder list come up EMPTY, so the
 * detour classifies .flac as WAV (11): listed, and its track info comes from the WAV tag
 * parser 0x1a7544(ctx, info). That parser reads the file's first 2 KB into ctx[7] and
 * returns -1 without "RIFF"; our detour then fills the track info (length, channels,
 * rate, kbps) from the FLAC STREAMINFO block in that buffer, and the text fields from the
 * VORBIS_COMMENT block (TITLE, ARTIST, ALBUM, GENRE, COMMENT), read through the parser's
 * FILE * (ctx[1]) - the same fields the WAV parser fills from LIST/INFO.
 *
 * dr_flac (https://github.com/mackron/dr_libs, public domain / MIT-0) is fetched at a
 * pinned commit by fetch-dr_flac.sh.
 *
 * Addresses: docs/firmware-mods.md section 7.
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#include "xdjmod.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>

#define DR_FLAC_IMPLEMENTATION
#define DR_FLAC_NO_STDIO
#define DR_FLAC_NO_OGG
#define DR_FLAC_NO_CRC
#include "dr_flac.h"

/* rbp 2.21 */
#define SUFFIX_FN     0x401d8u           /* int (*)(const char *path, const char *suffix) */
#define SUFFIX_W0     0xe92d4070u        /* push {r4, r5, r6, lr} */
#define SUFFIX_W1     0xe1a05001u        /* mov r5, r1            */
#define SUFFIX_WAV    0x35dd04u          /* ".WAV", the factory's WAV test */
#define CLASSIFY_FN   0x1f1ce4u          /* int (*)(void *, const uint16_t *name) -> file type */
#define CLASSIFY_W0   0xe92d4030u        /* push {r4, r5, lr} */
#define CLASSIFY_W1   0xe1a00001u        /* mov r0, r1        */
#define TYPE_WAV      11                 /* the browser lists .flac as WAV (see above) */
#define TAG_FN        0x1a7544u          /* int (*)(int *ctx, uint8_t *info): WAV tags */
#define TAG_W0        0xe92d4ff0u        /* push {r4-fp, lr} */
#define TAG_W1        0xe1a05000u        /* mov r5, r0       */

/* rbp's InputStream (see wav.c) */
struct stream { void **vt; };
typedef int (*st_read_fn)(struct stream *, void *, int);
typedef void (*st_seek_fn)(struct stream *, long long);
typedef long long (*st_ll_fn)(struct stream *);

struct flac {
    struct stream *s;
    long long pos;                     /* our view of the stream position */
    drflac *d;
};

static size_t on_read(void *u, void *buf, size_t n)
{
    struct flac *f = u;
    int got = ((st_read_fn)f->s->vt[0x10 / 4])(f->s, buf, (int)n);
    if (got <= 0)
        return 0;
    f->pos += got;
    return (size_t)got;
}

static drflac_bool32 on_seek(void *u, int offset, drflac_seek_origin origin)
{
    struct flac *f = u;
    long long to = offset;
    if (origin == DRFLAC_SEEK_CUR)
        to += f->pos;
    else if (origin == DRFLAC_SEEK_END)
        to += ((st_ll_fn)f->s->vt[0x08 / 4])(f->s);       /* stream length */
    if (to < 0)
        return DRFLAC_FALSE;
    ((st_seek_fn)f->s->vt[0x5c / 4])(f->s, to);
    f->pos = to;
    return DRFLAC_TRUE;
}

static drflac_bool32 on_tell(void *u, drflac_int64 *cursor)
{
    *cursor = ((struct flac *)u)->pos;
    return DRFLAC_TRUE;
}

/* ---- the codec, used by wav.c (weak references there) ---- */
void *xm_flac_open(void *stream, uint32_t *rate, int *channels, int *bits, uint64_t *frames)
{
    struct flac *f = calloc(1, sizeof(*f));
    if (!f)
        return 0;
    f->s = stream;
    ((st_seek_fn)f->s->vt[0x5c / 4])(f->s, 0);
    f->d = drflac_open(on_read, on_seek, on_tell, f, 0);
    if (!f->d) {
        free(f);
        return 0;
    }
    *rate = f->d->sampleRate;
    *channels = f->d->channels;
    *bits = f->d->bitsPerSample;
    *frames = f->d->totalPCMFrameCount;
    return f;
}

/* Decode n frames from `frame` into stereo float pairs; returns frames decoded. */
int xm_flac_read(void *h, uint64_t frame, float *out, int n)
{
    struct flac *f = h;
    if (f->d->currentPCMFrame != frame && !drflac_seek_to_pcm_frame(f->d, frame))
        return 0;
    int ch = f->d->channels;
    if (ch == 2)
        return (int)drflac_read_pcm_frames_f32(f->d, (drflac_uint64)n, out);
    /* mono: decode, then duplicate to both channels from the end backwards */
    int got = (int)drflac_read_pcm_frames_f32(f->d, (drflac_uint64)n, out);
    for (int i = got - 1; i >= 0; i--)
        out[2 * i] = out[2 * i + 1] = out[i];
    return got;
}

void xm_flac_close(void *h)
{
    struct flac *f = h;
    if (f) {
        drflac_close(f->d);
        free(f);
    }
}

/* ---- the factory's suffix test ---- */
typedef int (*suffix_fn)(const char *, const char *);
static suffix_fn suffix_orig;

static int suffix_hook(const char *path, const char *suffix)
{
    if ((uintptr_t)suffix == SUFFIX_WAV && path) {
        size_t n = strlen(path);
        if (n > 5 && !strcasecmp(path + n - 5, ".flac"))
            return 1;                     /* a FileReadWav; wav.c sees "fLaC" */
    }
    return suffix_orig(path, suffix);
}

/* ---- the browser's file-type classifier ---- */
typedef int (*classify_fn)(void *, const uint16_t *, int);
static classify_fn classify_orig;

static int classify_hook(void *ctx, const uint16_t *name, int fsflag)
{
    if (name) {
        int n = 0;
        while (name[n])
            n++;
        if (n > 5 && name[n - 5] == '.') {
            static const char flac[] = "FLAC";
            int k = 0;
            while (k < 4 && name[n - 4 + k] < 0x80 && (name[n - 4 + k] & ~0x20) == flac[k])
                k++;
            if (k == 4)
                return TYPE_WAV;
        }
    }
    return classify_orig(ctx, name, fsflag);
}

/* ---- the browser's track info (WAV tag parser) for a FLAC file ---- */
typedef int (*tag_fn)(int *ctx, uint8_t *info);
static tag_fn tag_orig;

static int rate_code(uint32_t r)
{
    switch (r) {                       /* rbp's codes (0x1a7868 ff.) */
    case 96000: return 0;  case 88200: return 1;  case 48000: return 3;  case 44100: return 4;
    case 32000: return 5;  case 24000: return 6;  case 22050: return 7;  case 16000: return 8;
    case 12000: return 9;  case 11025: return 10; case 8000: return 11;  default: return 15;
    }
}

/* Track-info text fields - 8-bit (Latin-1) text, as the WAV parser copies LIST/INFO into
 * them (INAM 0x50, IPRD 0x150, IART 0x250, ICMT 0x350, IGNR 0x550). The browser's album row
 * (disc icon) shows +0x550, so ALBUM goes there. */
#define INFO_TITLE    0x50               /* 127 bytes */
#define INFO_GENRE    0x150
#define INFO_ARTIST   0x250
#define INFO_COMMENT  0x350              /* 255 bytes */
#define INFO_ALBUM    0x550

static uint32_t le32p(const uint8_t *p) { return p[0] | p[1] << 8 | p[2] << 16 | (uint32_t)p[3] << 24; }

/* UTF-8 (Vorbis comments) -> Latin-1 (rbp's text); anything beyond U+00FF becomes '?'. */
static void set_text(uint8_t *info, unsigned off, unsigned max, const char *v, uint32_t n)
{
    if (info[off])
        return;                                   /* first value wins (e.g. many ARTISTs) */
    const uint8_t *u = (const uint8_t *)v, *end = u + n;
    unsigned o = 0;
    while (u < end && o < max) {
        uint32_t c = *u++;
        if (c >= 0x80) {
            int more = c >= 0xf0 ? 3 : c >= 0xe0 ? 2 : c >= 0xc0 ? 1 : 0;
            c &= 0x3f >> more;
            for (; more && u < end && (*u & 0xc0) == 0x80; more--)
                c = c << 6 | (*u++ & 0x3f);
            if (more || c > 0xff)
                c = '?';
        }
        info[off + o++] = (uint8_t)c;
    }
    info[off + o] = 0;
}

/* Copy Vorbis comments (FLAC metadata block type 4) into the track info. */
static void read_vorbis_comments(FILE *fp, uint8_t *info)
{
    long saved = ftell(fp);
    uint8_t h[4];
    long pos = 4;                                 /* after "fLaC" */
    for (int blocks = 0; blocks < 64 && fseek(fp, pos, SEEK_SET) == 0 && fread(h, 1, 4, fp) == 4;
         blocks++) {
        uint32_t len = (uint32_t)h[1] << 16 | h[2] << 8 | h[3];
        if ((h[0] & 0x7f) == 4 && len >= 8 && len <= (1u << 20)) {
            uint8_t *b = malloc(len);
            if (b && fread(b, 1, len, fp) == len) {
                uint32_t o = 4 + le32p(b);        /* skip the vendor string */
                uint32_t count = o + 4 <= len ? le32p(b + o) : 0;
                o += 4;
                for (uint32_t i = 0; i < count && o + 4 <= len; i++) {
                    uint32_t n = le32p(b + o);
                    o += 4;
                    if (n > len - o)
                        break;
                    const char *e = (const char *)b + o, *eq = memchr(e, '=', n);
                    o += n;
                    if (!eq)
                        continue;
                    size_t kl = eq - e;
                    const char *v = eq + 1;
                    uint32_t vl = n - kl - 1;
                    if (kl == 5 && !strncasecmp(e, "TITLE", 5))
                        set_text(info, INFO_TITLE, 127, v, vl);
                    else if (kl == 6 && !strncasecmp(e, "ARTIST", 6))
                        set_text(info, INFO_ARTIST, 127, v, vl);
                    else if (kl == 5 && !strncasecmp(e, "ALBUM", 5))
                        set_text(info, INFO_ALBUM, 127, v, vl);
                    else if (kl == 5 && !strncasecmp(e, "GENRE", 5))
                        set_text(info, INFO_GENRE, 127, v, vl);
                    else if ((kl == 7 && !strncasecmp(e, "COMMENT", 7)) ||
                             (kl == 11 && !strncasecmp(e, "DESCRIPTION", 11)))
                        set_text(info, INFO_COMMENT, 255, v, vl);
                }
            }
            free(b);
            break;
        }
        if (h[0] & 0x80)                          /* last metadata block */
            break;
        pos += 4 + len;
    }
    fseek(fp, saved, SEEK_SET);
}

static int tag_hook(int *ctx, uint8_t *info)
{
    int r = tag_orig(ctx, info);
    const uint8_t *b = (const uint8_t *)(uintptr_t)ctx[7];     /* the file's first 2 KB */
    if (r != -1 || !b || memcmp(b, "fLaC", 4) || (b[4] & 0x7f) != 0)
        return r;                                                /* not FLAC + STREAMINFO */
    const uint8_t *si = b + 8;                                   /* STREAMINFO body */
    uint32_t rate = (uint32_t)si[10] << 12 | si[11] << 4 | si[12] >> 4;
    unsigned ch = ((si[12] >> 1) & 7) + 1;
    uint64_t total = (uint64_t)(si[13] & 15) << 32 | (uint32_t)si[14] << 24 |
                     (uint32_t)si[15] << 16 | si[16] << 8 | si[17];
    if (!rate || !total)
        return r;
    uint32_t *w = (uint32_t *)info;
    uint32_t fsize = w[1];
    uint64_t secs75 = (total * 75 + rate - 1) / rate;           /* length in 1/75 s */
    w[0] = fsize;                                                /* "data" bytes */
    w[2] = (uint32_t)secs75;
    w[3] = 0;
    w[4] = 0;                                                    /* data offset */
    info[0x4a] = 3;                                              /* length valid */
    info[0x4d] = (uint8_t)((info[0x4d] & 0xc0) | (ch & 7) << 3 | 5);
    info[0x4e] = (uint8_t)((info[0x4e] & 0xf0) | rate_code(rate));
    uint64_t kbps = secs75 ? (uint64_t)fsize * 8 * 75 / secs75 / 1000 : 0;
    *(uint16_t *)(info + 0x46) = (uint16_t)(kbps > 0xffff ? 0xffff : kbps);
    info[0x4c] = (uint8_t)((info[0x4c] & 0xe0) | 1);             /* parsed */
    if (ctx[1])
        read_vorbis_comments((FILE *)(uintptr_t)ctx[1], info);
    return 0;
}

XM_MODULE(flac)
{
    if (!xm_expect32(SUFFIX_WAV, 0x5641572eu))   /* ".WAV" */
        return;
    suffix_orig = (suffix_fn)xm_detour(SUFFIX_FN, SUFFIX_W0, SUFFIX_W1, (void *)suffix_hook);
    if (!suffix_orig) {
        xm_log("flac: not installed");
        return;
    }
    classify_orig = (classify_fn)xm_detour(CLASSIFY_FN, CLASSIFY_W0, CLASSIFY_W1,
                                           (void *)classify_hook);
    tag_orig = (tag_fn)xm_detour(TAG_FN, TAG_W0, TAG_W1, (void *)tag_hook);
    xm_log("flac: FLAC via the wav reader%s%s", classify_orig ? ", listed in the browser" : "",
           tag_orig ? ", with track info" : "");
}
