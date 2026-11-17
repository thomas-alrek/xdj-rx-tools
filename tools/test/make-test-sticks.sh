#!/usr/bin/env bash
# Build the generated test sticks (stress test, mod tests) from synthetic audio only - no
# personal music, no downloads. macOS (diskutil/hdiutil); needs ffmpeg, flac and baken
# (brew install ffmpeg flac M-Igashi/tap/baken).
#
#   tools/test/make-test-sticks.sh [--big] [OUTDIR]          default OUTDIR: test/usb
#
# Writes (git-ignored, like every test/usb/*.img):
#   gen_lib_exfat.img   exFAT, rekordbox library (export.pdb + generated analysis): playlist
#                       "Test" with MP3, FLAC 24/96, WAV 24/96 and float WAV tracks; beat grids,
#                       hot cues A-H in rekordbox colours (baken snaps them to the palette)
#   gen_lib_fat32.img   the same library on FAT32
#   gen_flac.img        FAT32, no library: tagged FLAC tones + WAV/MP3 controls
#                       (tools/fw/mods/flac/make-flac-test-image.sh)
#   gen_wav.img         FAT32, no library: the wav mod's tones - 16/24/32-bit, float, 44.1-192 kHz
#   gen_big10k.img      (--big) rekordbox library of 10,000 short MP3 tracks, playlists of
#                       100 / 1,000 / 3,000 / 10,000 - for large-library tests (slow to build)
#
# The tracks are drum-machine-like loops (kick, hi-hat, a two-chord pad) at fixed tempos, so
# 3-band waveforms, beat grids and beat jump have something to show. baken writes the analysis
# from the audio (--generate-analysis); grid and cues come from the XML written here.
set -euo pipefail
cd "$(dirname "$0")/../.."
BIG=0
if [ "${1:-}" = --big ]; then BIG=1; shift; fi
OUT="${1:-test/usb}"
[ "$(uname)" = Darwin ] || { echo "macOS only (diskutil/hdiutil)" >&2; exit 1; }
for t in ffmpeg flac baken; do
  command -v $t >/dev/null || { echo "needs $t (brew install ffmpeg flac M-Igashi/tap/baken)" >&2; exit 1; }
done
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
TMP="$(mktemp -d)"; DEVS=()
cleanup() { for d in ${DEVS[@]+"${DEVS[@]}"}; do hdiutil detach "$d" >/dev/null 2>&1 || true; done; rm -rf "$TMP"; }
trap cleanup EXIT

# blank image with one partition, mounted: sets VOL
blank() {  # FS(ExFAT|FAT32) LABEL SIZE IMG
  rm -f "$4"; mkfile -n "$3" "$4"
  local d; d=$(hdiutil attach -imagekey diskimage-class=CRawDiskImage -nomount "$4" 2>/dev/null | awk 'NR==1{print $1}')
  DEVS+=("$d"); DEV="$d"
  diskutil partitionDisk "$d" MBR "$1" "$2" 100% >/dev/null
  VOL=$(diskutil info "${d}s1" | awk -F': *' '/Mount Point/{print $2}')
  touch "$VOL/.metadata_never_index"
}
done_img() { diskutil eject "$DEV" >/dev/null; }

# ---- 1. synthetic tracks ------------------------------------------------------------
# kick on every beat, hi-hat on the off-beat, a pad changing chord every 2 bars
loop() {  # BPM SECONDS RATE OUT [codec args...]
  local bpm=$1 secs=$2 rate=$3 out=$4; shift 4
  local b; b=$(python3 -c "print(60/$bpm)")
  local e="0.8*sin(2*PI*(50+90*exp(-mod(t,$b)*30))*mod(t,$b))*exp(-mod(t,$b)*9)"
  e="$e+0.25*(random(0)*2-1)*exp(-mod(t+$b/2,$b)*45)"
  e="$e+0.12*(sin(2*PI*if(mod(floor(t/($b*8)),2),220,196)*t)+sin(2*PI*if(mod(floor(t/($b*8)),2),330,294)*t))"
  ffmpeg -loglevel error -y -f lavfi -i "aevalsrc=exprs='$e|$e':s=$rate:d=$secs" "$@" "$out"
}
mkdir -p "$TMP/lib"
echo ">> synthetic tracks"
loop 128 90 44100 "$TMP/lib/synth_128_mp3.mp3" -c:a libmp3lame -b:a 320k \
  -metadata title="Synth 128 (MP3)" -metadata artist="XDJ Test Loops" -metadata album="Generated"
loop 124 90 96000 "$TMP/lib/synth_124_flac.flac" -c:a flac -sample_fmt s32 -bits_per_raw_sample 24 \
  -metadata title="Synth 124 (FLAC 24/96)" -metadata artist="XDJ Test Loops" -metadata album="Generated"
loop 120 90 96000 "$TMP/lib/synth_120_wav.wav" -c:a pcm_s24le
loop 132 60 48000 "$TMP/lib/synth_132_f32.wav" -c:a pcm_f32le
loop 126 120 44100 "$TMP/lib/synth_126_mp3.mp3" -c:a libmp3lame -b:a 192k \
  -metadata title="Synth 126 (MP3)" -metadata artist="XDJ Test Loops" -metadata album="Generated"

# ---- 2. the rekordbox XML: beat grids, hot cues A-H in rekordbox colours ------------
python3 - "$TMP/lib" > "$TMP/lib.xml" <<'EOF'
import os, sys, urllib.parse
d = sys.argv[1]
TRACKS = [  # file, title, kind, bpm, seconds, rate, key
    ("synth_128_mp3.mp3", "Synth 128 (MP3)", "MP3 File", 128, 90, 44100, "8A"),
    ("synth_124_flac.flac", "Synth 124 (FLAC 24/96)", "FLAC File", 124, 90, 96000, "5A"),
    ("synth_120_wav.wav", "Synth 120 (WAV 24/96)", "WAV File", 120, 90, 96000, "11B"),
    ("synth_132_f32.wav", "Synth 132 (WAV float 48k)", "WAV File", 132, 60, 48000, "2A"),
    ("synth_126_mp3.mp3", "Synth 126 (MP3)", "MP3 File", 126, 120, 44100, "9A"),
]
COLOURS = [(40, 226, 20), (48, 90, 255), (222, 68, 207), (255, 127, 42),   # A-D
           (0, 224, 255), (165, 225, 22), (180, 50, 255), (230, 40, 40)]   # E-H
out = ['<?xml version="1.0" encoding="UTF-8"?>',
       '<DJ_PLAYLISTS Version="1.0.0"><PRODUCT Name="rekordbox" Version="6.8.0" Company="AlphaTheta"/>',
       f'<COLLECTION Entries="{len(TRACKS)}">']
for i, (fn, title, kind, bpm, secs, rate, key) in enumerate(TRACKS, 1):
    p = os.path.join(d, fn)
    bar = 240 / bpm
    out.append(f'<TRACK TrackID="{i}" Name="{title}" Artist="XDJ Test Loops" Album="Generated" '
               f'Genre="Test" Kind="{kind}" Size="{os.path.getsize(p)}" TotalTime="{secs}" '
               f'AverageBpm="{bpm:.2f}" Tonality="{key}" SampleRate="{rate}" '
               f'Comments="generated by make-test-sticks.sh" '
               f'Location="file://localhost{urllib.parse.quote(p)}">')
    out.append(f'<TEMPO Inizio="0.000" Bpm="{bpm:.2f}" Metro="4/4" Battito="1"/>')
    for n in range(8):                       # hot cues A-H, every 2 bars from bar 2
        r, g, b = COLOURS[n]
        out.append(f'<POSITION_MARK Name="" Type="0" Start="{bar * (2 + 2 * n):.3f}" Num="{n}" '
                   f'Red="{r}" Green="{g}" Blue="{b}"/>')
    out.append('</TRACK>')
out.append('</COLLECTION><PLAYLISTS><NODE Type="0" Name="ROOT" Count="1">'
           f'<NODE Name="Test" Type="1" KeyType="0" Entries="{len(TRACKS)}">'
           + "".join(f'<TRACK Key="{i}"/>' for i in range(1, len(TRACKS) + 1))
           + '</NODE></NODE></PLAYLISTS></DJ_PLAYLISTS>')
print("\n".join(out))
EOF

# ---- 3. library sticks ---------------------------------------------------------------
for fs in ExFAT FAT32; do
  img="$OUT/gen_lib_$(echo $fs | tr A-Z a-z).img"
  echo ">> $img"
  blank $fs XDJGEN 1g "$img"
  baken expressport "$TMP/lib.xml" --device "$VOL" --playlist Test --no-settings \
    --generate-analysis --device-name XDJGEN 2>&1 | tail -1
  [ -f "$VOL/PIONEER/rekordbox/export.pdb" ] || { echo "no export.pdb on $img" >&2; exit 1; }
  done_img
done

# ---- 4. folder sticks ----------------------------------------------------------------
echo ">> $OUT/gen_flac.img"
tools/fw/mods/flac/make-flac-test-image.sh "$OUT/gen_flac.img" | tail -1
echo ">> $OUT/gen_wav.img"
python3 tools/fw/mods/wav/gen_test_wavs.py "$TMP/wav" >/dev/null
tools/make-fat32-image.sh "$OUT/gen_wav.img" "$TMP/wav"/*.wav | tail -1

# ---- 5. the 10,000-track library (--big) --------------------------------------------
if [ $BIG = 1 ]; then
  echo ">> $OUT/gen_big10k.img (10,000 tracks; takes a while)"
  mkdir -p "$TMP/big"
  ffmpeg -loglevel error -y -f lavfi -i "sine=frequency=220:duration=3" -ac 2 \
    -c:a libmp3lame -b:a 128k "$TMP/big/seed.mp3"
  python3 - "$TMP/big" > "$TMP/big.xml" <<'EOF'
import os, random, sys, urllib.parse
d = sys.argv[1]; N = 10000
random.seed(7)
keys = [f"{n}{m}" for m in "AB" for n in range(1, 13)]
genres = ["House", "Techno", "Trance", "Hardstyle", "DnB", "Disco", "Pop", "HipHop"]
seed = os.path.join(d, "seed.mp3"); size = os.path.getsize(seed)
tr = []
for i in range(N):
    fn = os.path.join(d, f"t{i:05d}.mp3")
    os.link(seed, fn)                        # same audio, one file each (hard links)
    tr.append(f'<TRACK TrackID="{i+1}" Name="Track {i:05d} {random.choice(genres)} Extended Mix featuring Someone" '
              f'Artist="Artist {random.randrange(1500):04d}" Album="Album {random.randrange(3000):04d}" '
              f'Genre="{random.choice(genres)}" Kind="MP3 File" Size="{size}" TotalTime="3" '
              f'AverageBpm="{random.uniform(90, 180):.2f}" Tonality="{random.choice(keys)}" '
              f'SampleRate="44100" BitRate="128" Comments="comment {i}" '
              f'Location="file://localhost{urllib.parse.quote(fn)}"/>')
pls = "".join(f'<NODE Name="P{n}" Type="1" KeyType="0" Entries="{n}">'
              + "".join(f'<TRACK Key="{i+1}"/>' for i in range(n)) + "</NODE>"
              for n in (100, 1000, 3000, 10000))
print('<?xml version="1.0" encoding="UTF-8"?>\n'
      '<DJ_PLAYLISTS Version="1.0.0"><PRODUCT Name="rekordbox" Version="6.8.0" Company="AlphaTheta"/>\n'
      f'<COLLECTION Entries="{N}">\n' + "\n".join(tr) + "\n</COLLECTION>\n"
      f'<PLAYLISTS><NODE Type="0" Name="ROOT" Count="4">{pls}</NODE></PLAYLISTS></DJ_PLAYLISTS>')
EOF
  blank FAT32 BIGLIB 2g "$OUT/gen_big10k.img"
  baken expressport "$TMP/big.xml" --device "$VOL" --no-settings --generate-analysis \
    --device-name BIGLIB 2>&1 | tail -1
  [ -f "$VOL/PIONEER/rekordbox/export.pdb" ] || { echo "no export.pdb on gen_big10k.img" >&2; exit 1; }
  done_img
fi
ls -l "$OUT"/gen_*.img
