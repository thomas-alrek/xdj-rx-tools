#!/usr/bin/env python3
"""Generate the wav mod's test tones (see make-wav-test-image.sh).

    gen_test_wavs.py OUTDIR [SECONDS]

Each file: 1 kHz left / 2 kHz right at -6 dBFS; hi-res files add an ultrasonic tone at
-12 dBFS on both channels (30 kHz at 88.2/96 kHz, 60 kHz at 176.4/192 kHz) that a correct
decimator removes. Pure stdlib.
"""
import math
import os
import struct
import sys

# name, rate, bits, float?, extensible?, ultrasonic Hz
FILES = [
    ("t_44k_16", 44100, 16, False, False, 0),     # stock plays it (control)
    ("t_48k_24", 48000, 24, False, False, 0),     # stock plays it, at 16-bit precision
    ("t_48k_f32", 48000, 32, True, False, 0),     # stock rejects float
    ("t_96k_24", 96000, 24, False, False, 30000),
    ("t_96k_f32x", 96000, 32, True, True, 30000),  # float, WAVE_FORMAT_EXTENSIBLE
    ("t_192k_24x", 192000, 24, False, True, 60000),
]
GUID_TAIL = bytes.fromhex("000000001000800000aa00389b71")


def sample(v, bits, flt):
    if flt:
        return struct.pack("<f", v)
    m = (1 << (bits - 1)) - 1
    i = max(-m - 1, min(m, int(round(v * m))))
    return i.to_bytes(bits // 8, "little", signed=True)


def write(path, rate, bits, flt, ext, ultra, secs):
    ch, bps = 2, bits // 8
    frames = rate * secs
    data = bytearray()
    a, u = 10 ** (-6 / 20), 10 ** (-12 / 20) if ultra else 0
    for n in range(frames):
        t = n / rate
        us = u * math.sin(2 * math.pi * ultra * t) if ultra else 0
        l = a * math.sin(2 * math.pi * 1000 * t) + us
        r = a * math.sin(2 * math.pi * 2000 * t) + us
        data += sample(l / (1 + u), bits, flt) + sample(r / (1 + u), bits, flt)
    tag = 0xFFFE if ext else (3 if flt else 1)
    fmt = struct.pack("<HHIIHH", tag, ch, rate, rate * ch * bps, ch * bps, bits)
    if ext:
        sub = 3 if flt else 1
        fmt += struct.pack("<HHI", 22, bits, 3) + struct.pack("<H", sub) + GUID_TAIL
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(data)) + data
    with open(path, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", len(body)) + body)


def main():
    out = sys.argv[1]
    secs = int(sys.argv[2]) if len(sys.argv) > 2 else 20
    os.makedirs(out, exist_ok=True)
    for name, rate, bits, flt, ext, ultra in FILES:
        write(os.path.join(out, name + ".wav"), rate, bits, flt, ext, ultra, secs)
        print(name, rate, bits, "float" if flt else "pcm", "ext" if ext else "")


if __name__ == "__main__":
    main()
