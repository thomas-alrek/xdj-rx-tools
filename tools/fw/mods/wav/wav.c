/*
 * wav mod: WAV beyond the stock 16/24-bit PCM at 44.1/48 kHz.
 *
 *   - PCM 16/24/32-bit, IEEE float 32/64-bit, WAVE_FORMAT_EXTENSIBLE (PCM/float)
 *   - mono or stereo, 8 kHz .. 192 kHz
 *   - full precision (stock plays 24-bit files at 16-bit: it keeps the top two bytes)
 *   - 88.2 kHz and up: low-pass + 2:1 decimation (4:1 from 176.4 kHz) in the reader, which
 *     then reports 44.1/48 kHz - rbp's own resampler (4-point Lagrange, no anti-alias
 *     filter) only ever sees <= 48 kHz, as with stock files.
 *
 * rbp picks the reader by file suffix (.WAV -> common::FileReadWav, vtable 0x35d588) and
 * drives it through vtable slots: 2 = parse header (0x375e0), 3 = readPcm (0x37298),
 * 7 = sample-rate check (0x2f278: 44100/48000 only). We point slots 2, 3 and 7 of the
 * WAV vtable at our versions; the object layout, rbp's InputStream and the error path
 * are used exactly as the stock functions use them.
 *
 * Reader object (common::FileReadWav): +0x04 InputStream*, +0x14 frames, +0x18 rate,
 * +0x1c channels (u16), +0x1e bits (u16), +0x20 rate*ch*bits, +0x28/+0x2c gain (float),
 * +0x30 bytes per sample (u16), +0x34 owner (error notify), +0x38 error, +0x3c first
 * read (u8), +0x6e0 data offset, +0x6e4 raw buffer (new[], freed by the stock dtor).
 * InputStream vtable: +0x0c error state, +0x10 read(buf, n), +0x5c seek(int64),
 * +0x68 ready.
 *
 * The browser and folder view take a track's length from a second, tag-reading WAV parser
 * (0x1a7544, the DBCM task) with the same stock limits; it returns -1 for anything else
 * and the deck shows END at 00:00. Its duration formula (data bytes x 600 / (ch x rate x
 * bits), in 1/75 s) is right for every format we accept, so we only relax its three format
 * checks - format tag != 1 (0x1a77f8), rate not 44.1/48 kHz (0x1a7864), bits not 16/24
 * (0x1a78d8) - to no-ops; the channel check (<= 2) stays. Formats the reader can't play
 * still fail at load.
 *
 * Addresses: docs/firmware-mods.md section 6.
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#include "xdjmod.h"

#include <math.h>
#include <pthread.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define WAV_VT        0x35d588u
#define SLOT_PARSE    (WAV_VT + 2 * 4)
#define SLOT_READ     (WAV_VT + 3 * 4)
#define SLOT_RATE     (WAV_VT + 7 * 4)
#define STOCK_PARSE   0x375e0u
#define STOCK_READ    0x37298u
#define STOCK_RATE    0x2f278u
#define RBP_ERRCHECK  0x30648u       /* int (*)(reader*): nonzero = stream failed */
/* tag parser 0x1a7544: the format checks we relax (bne to the -1 exit 0x1a77dc) */
#define TAG_TAG_BNE   0x1a77f8u
#define TAG_RATE_BNE  0x1a7864u
#define TAG_BITS_BNE  0x1a78d8u
#define ARM_NOP       0xe1a00000u    /* mov r0, r0 */

/* ---- rbp objects ---- */
struct stream { void **vt; };
typedef int  (*st_int_fn)(struct stream *);
typedef int  (*st_read_fn)(struct stream *, void *, int);
typedef void (*st_seek_fn)(struct stream *, long long);
struct owner { void **vt; };
typedef void (*owner_notify_fn)(struct owner *, int);

#define F(obj, off, type) (*(type *)((char *)(obj) + (off)))
#define R_STREAM(o)   F(o, 0x04, struct stream *)
#define R_FRAMES(o)   F(o, 0x14, uint32_t)
#define R_RATE(o)     F(o, 0x18, uint32_t)
#define R_CH(o)       F(o, 0x1c, uint16_t)
#define R_BITS(o)     F(o, 0x1e, uint16_t)
#define R_BITRATE(o)  F(o, 0x20, uint32_t)
#define R_GAIN0(o)    F(o, 0x28, float)
#define R_GAIN1(o)    F(o, 0x2c, float)
#define R_BPS(o)      F(o, 0x30, uint16_t)
#define R_OWNER(o)    F(o, 0x34, struct owner *)
#define R_ERROR(o)    F(o, 0x38, int32_t)
#define R_FIRST(o)    F(o, 0x3c, uint8_t)
#define R_DATAOFF(o)  F(o, 0x6e0, uint32_t)
#define R_RAWBUF(o)   F(o, 0x6e4, void *)

static int  st_read(struct stream *s, void *b, int n) { return ((st_read_fn)s->vt[0x10 / 4])(s, b, n); }
static void st_seek(struct stream *s, long long off) { ((st_seek_fn)s->vt[0x5c / 4])(s, off); }
static int  st_ready(struct stream *s) { return ((st_int_fn)s->vt[0x68 / 4])(s); }
static int  st_failed(struct stream *s) { return ((st_int_fn)s->vt[0x0c / 4])(s); }
static int  rbp_errcheck(void *r) { return ((int (*)(void *))RBP_ERRCHECK)(r); }

void *_Znaj(unsigned);                 /* operator new[] - the stock dtor delete[]s +0x6e4 */
void _ZdaPv(void *);                   /* operator delete[] */

static void nap_ns(long ns)
{
    struct timespec ts = { 0, ns };
    nanosleep(&ts, 0);
}

/* ---- FLAC: the flac mod's codec (flac.c), when it is built in ---- */
void *xm_flac_open(void *stream, uint32_t *rate, int *channels, int *bits, uint64_t *frames)
    __attribute__((weak));
int xm_flac_read(void *h, uint64_t frame, float *out, int n) __attribute__((weak));
void xm_flac_close(void *h) __attribute__((weak));

/* ---- per-reader format (rbp's object has no room for it) ---- */
enum fmt { FMT_PCM, FMT_FLOAT, FMT_FLAC };
struct wavinfo {
    void *obj;
    int fmt, bits, ch, decim;
    uint32_t in_frames;                /* frames in the file (before decimation) */
    float *buf;                        /* decoded input window, stereo */
    uint8_t *raw;                      /* raw input window */
    int cap;                           /* window capacity in input frames */
    int logged_read;
    void *flac;                        /* FMT_FLAC: the decoder */
    long win_first;                    /* FMT_FLAC: the window last decoded into buf */
    int win_n;
};
#define NINFO 16
static struct wavinfo infos[NINFO];
static int next_info;
static pthread_mutex_t info_lock = PTHREAD_MUTEX_INITIALIZER;

static struct wavinfo *info_of(void *obj, int create)
{
    pthread_mutex_lock(&info_lock);
    struct wavinfo *w = 0;
    for (int i = 0; i < NINFO; i++)
        if (infos[i].obj == obj) {
            w = &infos[i];
            break;
        }
    if (!w && create) {
        w = &infos[next_info];
        next_info = (next_info + 1) % NINFO;
        if (w->flac && xm_flac_close)
            xm_flac_close(w->flac);         /* a reader rbp has since destroyed */
        w->flac = 0;
        w->obj = obj;
    }
    pthread_mutex_unlock(&info_lock);
    return w;
}

/* ---- anti-alias low-pass for decimation (windowed sinc, Blackman) ---- */
#define TAPS2  63                      /* 2:1 */
#define TAPS4  127                     /* 4:1 */
static float fir2[TAPS2], fir4[TAPS4];

static void design(float *h, int taps, int decim)
{
    double fc = 0.5 / decim * 0.92;    /* cutoff, cycles per input sample (~22 kHz at 96k) */
    double sum = 0;
    int m = taps - 1;
    for (int i = 0; i < taps; i++) {
        double x = i - m / 2.0;
        double s = x == 0 ? 2 * fc : sin(2 * M_PI * fc * x) / (M_PI * x);
        double w = 0.42 - 0.5 * cos(2 * M_PI * i / m) + 0.08 * cos(4 * M_PI * i / m);
        h[i] = (float)(s * w);
        sum += h[i];
    }
    for (int i = 0; i < taps; i++)
        h[i] = (float)(h[i] / sum);
}

/* rbp's reader fields for a parsed file */
static void fill_object(void *obj, const struct wavinfo *w, uint32_t rate, uint32_t data_off)
{
    R_RATE(obj) = rate / w->decim;
    R_CH(obj) = (uint16_t)w->ch;
    R_BITS(obj) = (uint16_t)w->bits;
    R_BPS(obj) = (uint16_t)((w->bits + 7) / 8);
    R_BITRATE(obj) = (rate / w->decim) * w->ch * w->bits;
    R_GAIN0(obj) = R_GAIN1(obj) = 1.0f / 32768;   /* stock value; our read emits +-1.0 */
    R_FRAMES(obj) = w->in_frames / w->decim ? w->in_frames / w->decim : 1;
    R_DATAOFF(obj) = data_off;
    if (R_RAWBUF(obj))
        _ZdaPv(R_RAWBUF(obj));
    R_RAWBUF(obj) = _Znaj(16);                     /* the stock dtor delete[]s it */
}

static int decim_for(uint32_t rate)
{
    return rate >= 176400 ? 4 : rate > 48000 ? 2 : 1;
}

/* A FLAC file in a FileReadWav (the flac mod routes .flac here). */
static int flac_parse(void *obj)
{
    if (!xm_flac_open)
        return 0;
    struct wavinfo *w = info_of(obj, 1);
    if (!w)
        return 0;
    if (w->flac) {
        xm_flac_close(w->flac);
        w->flac = 0;
    }
    uint32_t rate = 0;
    int ch = 0, bits = 0;
    uint64_t frames = 0;
    void *h = xm_flac_open(R_STREAM(obj), &rate, &ch, &bits, &frames);
    if (!h) {
        xm_log("flac: not a FLAC stream");
        return 0;
    }
    if (ch < 1 || ch > 2 || rate < 8000 || rate > 192000 || !frames || frames > 0xffffffffu) {
        xm_log("flac: unsupported %d ch / %u Hz / %llu frames", ch, rate, frames);
        xm_flac_close(h);
        return 0;
    }
    w->fmt = FMT_FLAC;
    w->flac = h;
    w->bits = bits;
    w->ch = ch;
    w->decim = decim_for(rate);
    w->in_frames = (uint32_t)frames;
    w->win_n = 0;
    w->logged_read = 0;
    fill_object(obj, w, rate, 0);
    xm_log("flac: %u Hz %d-bit %d ch, decim %d -> %u Hz, %u frames", rate, bits, ch, w->decim,
           rate / w->decim, R_FRAMES(obj));
    return 1;
}

/* ---- header parse (vtable slot 2): 1 = ok ---- */
static uint32_t le32(const uint8_t *p) { return p[0] | p[1] << 8 | p[2] << 16 | (uint32_t)p[3] << 24; }
static uint16_t le16(const uint8_t *p) { return (uint16_t)(p[0] | p[1] << 8); }

static int wav_parse(void *obj)
{
    struct stream *s = R_STREAM(obj);
    uint8_t h[12], f[40];
    int have_fmt = 0, tag = 0, ch = 0, bits = 0;
    uint32_t rate = 0;

    R_DATAOFF(obj) = 0;
    if (!s || st_read(s, h, 12) != 12)
        return 0;
    if (!memcmp(h, "fLaC", 4) || !memcmp(h, "ID3", 3))
        return flac_parse(obj);
    if (memcmp(h, "RIFF", 4) || memcmp(h + 8, "WAVE", 4))
        return 0;
    uint32_t off = 12;
    st_seek(s, off);
    for (;;) {
        if (rbp_errcheck(obj) || st_read(s, h, 8) != 8)
            return 0;
        uint32_t size = le32(h + 4);
        if (!memcmp(h, "fmt ", 4)) {
            int n = size < sizeof(f) ? (int)size : (int)sizeof(f);
            if (n < 16 || st_read(s, f, n) != n)
                return 0;
            tag = le16(f);
            ch = le16(f + 2);
            rate = le32(f + 4);
            bits = le16(f + 14);
            if (tag == 0xfffe) {               /* extensible: the subformat GUID's tag */
                if (n < 26)
                    return 0;
                tag = le16(f + 24);
            }
            have_fmt = 1;
        } else if (!memcmp(h, "data", 4)) {
            if (!have_fmt)
                return 0;
            off += 8;
            break;
        }
        off += 8 + size + (size & 1);           /* chunks are word-aligned */
        st_seek(s, off);
    }

    int fmt;
    if (tag == 1 && (bits == 16 || bits == 24 || bits == 32))
        fmt = FMT_PCM;
    else if (tag == 3 && (bits == 32 || bits == 64))
        fmt = FMT_FLOAT;
    else {
        xm_log("wav: unsupported format tag %d, %d bits", tag, bits);
        return 0;
    }
    if (ch < 1 || ch > 2 || rate < 8000 || rate > 192000) {
        xm_log("wav: unsupported %d ch / %u Hz", ch, rate);
        return 0;
    }
    int decim = decim_for(rate);
    uint32_t data_size = le32(h + 4);
    int frame_bytes = ch * (bits / 8);
    uint32_t in_frames = data_size / frame_bytes;

    struct wavinfo *w = info_of(obj, 1);
    if (!w)
        return 0;
    w->fmt = fmt;
    w->bits = bits;
    w->ch = ch;
    w->decim = decim;
    w->in_frames = in_frames;
    w->logged_read = 0;
    if (w->flac && xm_flac_close) {
        xm_flac_close(w->flac);
        w->flac = 0;
    }
    fill_object(obj, w, rate, off);
    xm_log("wav: %u Hz %d-bit %s %d ch, decim %d -> %u Hz, %u frames, data @%u", rate, bits,
           fmt == FMT_FLOAT ? "float" : "pcm", ch, decim, rate / decim, R_FRAMES(obj), off);
    return 1;
}

/* sample-rate check (slot 7): parse already limited the range */
static int wav_rate_ok(void *obj, int rate)
{
    return rate >= 8000 && rate <= 192000;
}

/* ---- decode n input frames at `raw` into stereo floats ---- */
static void decode(const struct wavinfo *w, const uint8_t *raw, float *out, int n)
{
    int bps = w->bits / 8, fb = bps * w->ch;
    for (int i = 0; i < n; i++, raw += fb) {
        float v[2] = { 0, 0 };
        for (int c = 0; c < w->ch; c++) {
            const uint8_t *p = raw + c * bps;
            float x;
            if (w->fmt == FMT_FLOAT) {
                if (w->bits == 32) {
                    uint32_t u = le32(p);
                    memcpy(&x, &u, 4);
                } else {
                    uint64_t u = le32(p) | (uint64_t)le32(p + 4) << 32;
                    double d;
                    memcpy(&d, &u, 8);
                    x = (float)d;
                }
                if (x > 1.0f)
                    x = 1.0f;
                else if (x < -1.0f)
                    x = -1.0f;
            } else if (w->bits == 16) {
                x = (int16_t)le16(p) * (1.0f / 32768);
            } else if (w->bits == 24) {
                int32_t s = (int32_t)((uint32_t)p[0] << 8 | (uint32_t)p[1] << 16 | (uint32_t)p[2] << 24);
                x = (float)(s >> 8) * (1.0f / 8388608);
            } else {
                x = (float)(int32_t)le32(p) * (1.0f / 2147483648.0f);
            }
            v[c] = x;
        }
        out[2 * i] = v[0];
        out[2 * i + 1] = w->ch == 2 ? v[1] : v[0];
    }
}

/* FMT_FLAC: decode [first, first+n) into w->buf, keeping what overlaps the previous
 * window (the decimator's windows overlap by its length), so decoding stays sequential. */
static int flac_window(void *obj, struct wavinfo *w, long first, int n)
{
    int keep = 0;
    if (w->win_n && first >= w->win_first && first < w->win_first + w->win_n) {
        int shift = (int)(first - w->win_first);
        keep = w->win_n - shift;
        if (keep > n)
            keep = n;
        memmove(w->buf, w->buf + 2 * shift, (size_t)keep * 2 * sizeof(float));
    }
    memset(w->buf + 2 * keep, 0, (size_t)(n - keep) * 2 * sizeof(float));
    w->win_first = first;
    w->win_n = 0;                            /* until the decode below succeeds */
    long lo = first + keep;
    if (lo < 0)
        lo = 0;
    long hi = first + n;
    if (hi > (long)w->in_frames)
        hi = w->in_frames;
    if (hi > lo) {
        if (R_FIRST(obj)) {                  /* first read: wait for the stream, as stock */
            while (!st_ready(R_STREAM(obj)) && !rbp_errcheck(obj))
                nap_ns(1000000);
            R_FIRST(obj) = 0;
        }
        int want = (int)(hi - lo);
        int got = xm_flac_read(w->flac, (uint64_t)lo, w->buf + 2 * (lo - first), want);
        if (got != want) {
            xm_log("flac: decode failed at frame %ld: %d of %d frames", lo, got, want);
            return 0;
        }
    }
    w->win_n = n;
    return 1;
}

/* Read input frames [first, first+n) as stereo floats into w->buf (zeros outside the
 * file). Returns 0 on a stream error (the caller reports it). */
static int read_window(void *obj, struct wavinfo *w, long first, int n)
{
    if (w->fmt == FMT_FLAC)
        return flac_window(obj, w, first, n);
    struct stream *s = R_STREAM(obj);
    int fb = (w->bits / 8) * w->ch;
    memset(w->buf, 0, (size_t)n * 2 * sizeof(float));
    long lo = first < 0 ? 0 : first;
    long hi = first + n;
    if (hi > (long)w->in_frames)
        hi = w->in_frames;
    if (hi <= lo)
        return 1;
    int want = (int)(hi - lo) * fb;
    st_seek(s, (long long)R_DATAOFF(obj) + (long long)lo * fb);
    if (R_FIRST(obj)) {                      /* first read: wait for the stream, as stock */
        while (!st_ready(s) && !rbp_errcheck(obj))
            nap_ns(1000000);
        R_FIRST(obj) = 0;
    }
    int got = st_read(s, w->raw, want);
    if (got != want && (st_failed(s) || got < 0)) {
        xm_log("wav: read failed at frame %ld: got %d of %d bytes", lo, got, want);
        return 0;
    }
    if (got < 0)
        got = 0;
    decode(w, w->raw, w->buf + 2 * (lo - first), got / fb);
    return 1;
}

static int ensure_window(struct wavinfo *w, int frames)
{
    if (frames <= w->cap)
        return 1;
    free(w->buf);
    free(w->raw);
    w->buf = malloc((size_t)frames * 2 * sizeof(float));
    w->raw = malloc((size_t)frames * 16);      /* up to 2 ch x 8 bytes */
    w->cap = (w->buf && w->raw) ? frames : 0;
    w->win_n = 0;                              /* the FLAC window cache is gone */
    return w->cap != 0;
}

static void report_error(void *obj)
{
    if (R_ERROR(obj) != -6) {
        R_ERROR(obj) = -6;
        struct owner *o = R_OWNER(obj);
        if (o)
            ((owner_notify_fn)o->vt[0x0c / 4])(o, -6);
    }
}

/* ---- readPcm (vtable slot 3): n stereo float frames from output frame `start` ---- */
#define CHUNK 512
static void wav_read(void *obj, int start, float *out, unsigned n)
{
    struct wavinfo *w = info_of(obj, 0);
    if (!R_STREAM(obj) || !w) {
        xm_log("wav: read without a parsed header (stream %p)", (void *)R_STREAM(obj));
        memset(out, 0, (size_t)n * 8);
        return;
    }
    if (!w->logged_read) {
        w->logged_read = 1;
        xm_log("wav: first read: frame %d, %u frames", start, n);
    }
    const float *h = w->decim == 4 ? fir4 : fir2;
    int taps = w->decim == 4 ? TAPS4 : TAPS2, half = (taps - 1) / 2;
    for (unsigned done = 0; done < n;) {
        int k = n - done > CHUNK ? CHUNK : (int)(n - done);
        long ostart = (long)start + done;
        nap_ns(100000);                          /* stock yields 0.1 ms per chunk */
        if (w->decim == 1) {
            if (!ensure_window(w, k) || !read_window(obj, w, ostart, k)) {
                report_error(obj);
                memset(out + 2 * done, 0, (size_t)(n - done) * 8);
                return;
            }
            memcpy(out + 2 * done, w->buf, (size_t)k * 8);
        } else {
            int d = w->decim, in_n = k * d + taps - 1;
            long in_first = ostart * d - half;
            if (!ensure_window(w, in_n) || !read_window(obj, w, in_first, in_n)) {
                report_error(obj);
                memset(out + 2 * done, 0, (size_t)(n - done) * 8);
                return;
            }
            for (int i = 0; i < k; i++) {
                const float *x = w->buf + 2 * (i * d);
                float l = 0, r = 0;
                for (int t = 0; t < taps; t++) {
                    l += h[t] * x[2 * t];
                    r += h[t] * x[2 * t + 1];
                }
                out[2 * (done + i)] = l;
                out[2 * (done + i) + 1] = r;
            }
        }
        done += k;
    }
}

XM_MODULE(wav)
{
    if (!xm_expect32(SLOT_PARSE, STOCK_PARSE) || !xm_expect32(SLOT_READ, STOCK_READ) ||
        !xm_expect32(SLOT_RATE, STOCK_RATE))
        return;
    design(fir2, TAPS2, 2);
    design(fir4, TAPS4, 4);
    if (!xm_expect32(TAG_TAG_BNE, 0x1afffff7u) || !xm_expect32(TAG_RATE_BNE, 0x1affffdcu) ||
        !xm_expect32(TAG_BITS_BNE, 0x1affffbfu))
        return;
    int ok = xm_patch32(SLOT_PARSE, STOCK_PARSE, (uint32_t)(uintptr_t)wav_parse) &&
             xm_patch32(SLOT_READ, STOCK_READ, (uint32_t)(uintptr_t)wav_read) &&
             xm_patch32(SLOT_RATE, STOCK_RATE, (uint32_t)(uintptr_t)wav_rate_ok) &&
             xm_patch32(TAG_TAG_BNE, 0x1afffff7u, ARM_NOP) &&
             xm_patch32(TAG_RATE_BNE, 0x1affffdcu, ARM_NOP) &&
             xm_patch32(TAG_BITS_BNE, 0x1affffbfu, ARM_NOP);
    xm_log("wav: %s", ok ? "PCM 16/24/32, float 32/64, 8-192 kHz" : "not installed");
}
