# Changelog: XDJ-RX custom firmware

Custom firmware for the Pioneer XDJ-RX, built on Pioneer's official 2.21. Full notes, settings
and the install guide per release: [docs/releases.md](docs/releases.md).

## 3.01

- **New: KEY DISPLAY (CLASSIC / CAMELOT, default CLASSIC).** Keys in Camelot notation ("8A"
  instead of "Am") in the browser's KEY lists and key columns, on the deck panel next to the
  waveforms, and SORT by KEY in Camelot order. The stick isn't changed.
- **New: SCREEN SYNC (OFF / 1.5 / 3.0 / 4.5 / 6.0 MS, default OFF), experimental.** Times the
  screen update to the display's refresh against a tear line through the scrolling waveform; for
  testing on real units.
- **Fixed:** colour waveforms and hot cues A–H in their colours show from the first moment a
  track loads (3.00 showed the stock blue waveform and green A–D cues until the load finished).
- Tested on a real XDJ-RX: installs and runs without instability so far.
- Output MD5 `c33963859ac201a5df7e4e0e591ceac6`. [Release notes](docs/release-notes-3.01.md).

## 3.00

- exFAT USB sticks, FLAC (with tags), WAV 32-bit / float / up to 192 kHz, rekordbox's Contents
  folder in [FOLDER].
- 60 fps screen, RGB / 3-band waveforms, smooth waveform scrolling, hot cues A–H in their
  rekordbox colours.
- Hot cues E–H on SHIFT + pads, beat jump on the pads.
- Safe start (hold MIDI at power-on); factory reset restores the defaults.
- Output MD5 `5492b333f6eb4b853cb7cfa113c48659`. [Release notes](docs/release-notes-3.00.md).
