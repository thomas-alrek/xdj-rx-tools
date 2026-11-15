# XDJ-RX emulator & firmware toolkit

[![License: GPL v2+](https://img.shields.io/badge/license-GPL--2.0--or--later-blue.svg)](LICENSE)
[![Platform: macOS (Apple Silicon)](https://img.shields.io/badge/platform-macOS%20%C2%B7%20Apple%20Silicon-lightgrey.svg)](#platform-support)
[![QEMU 11.1.0](https://img.shields.io/badge/QEMU-11.1.0-orange.svg)](https://www.qemu.org/)
[![Rust](https://img.shields.io/badge/Rust-stable-dea584.svg)](https://www.rust-lang.org/)
[![Firmware: XDJ-RX 2.21](https://img.shields.io/badge/firmware-XDJ--RX%202.21-black.svg)](#2-get-the-firmware-and-its-key)
[![Version: 1.0.0](https://img.shields.io/badge/version-1.0.0-brightgreen.svg)](#)

Run the **Pioneer DJ XDJ-RX**'s own, unmodified firmware on your Mac — the real rekordbox UI,
both decks, the mixer, USB libraries, firmware updates — and build and test **custom firmware**
for it.

The XDJ-RX is an i.MX6-based all-in-one DJ system. This project emulates its board in a patched
QEMU (display, audio, front panel, USB, NAND flash, the panel microcontroller's bootloader), wraps it
in a desktop app with a virtual control surface, and provides `xdj`, a toolkit for unpacking,
modifying and repacking the firmware's `.UPD` update files.

> **Interoperability and preservation on hardware you own.** No Pioneer/AlphaTheta firmware,
> firmware contents or keys are included here: you download the official firmware yourself, and
> the decryption key comes from Pioneer's own published GPL source release.

## Features

- **Stock firmware, unmodified**: boots to the live rekordbox UI; decks play, the mixer works, and
  audio reaches your speakers.
- **Virtual front panel**: every deck, mixer and browse control (except the microphone), with the
  LEDs, level meters and jog rings driven by the firmware.
- **Scriptable over MCP**: an AI agent or test harness can load firmware, press any control, insert
  USB images and take screenshots.
- **A real unit, not a test rig**: the app emulates the NAND flash and installs the firmware with
  Pioneer's own updater, exactly as a factory-fresh unit. UTILITY → VERSION No. reads the real
  version.
- **rekordbox USB sticks**: load a disk image (or a real stick) into USB1/USB2; browse and play its
  library; MASTER REC records the mix to USB2.
- **Service modes**: test mode (versions, LED/switch/fader oracles), firmware-update mode, and a
  power button that performs the unit's real standby handshake.
- **Custom firmware**: `xdj fw unpack` → edit → `xdj fw repack`, then *File → Install Firmware
  Update…* — Pioneer's updater installs it on the emulated unit like an official release.
- **Reverse-engineering docs** for the firmware's main application, the front-panel protocol and
  the hardware it drives.

## Getting started (macOS, from scratch)

Assumes a Mac with Apple Silicon and **nothing installed**. Expect ~20 minutes, mostly compiling.

### 1. Install the tools

```sh
# Apple's command-line developer tools (compilers, git, python3)
xcode-select --install

# Homebrew (https://brew.sh) — then follow the two "Next steps" lines it prints
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# Rust (https://rustup.rs) — accept the defaults, then open a new terminal
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh

# Libraries QEMU links against, plus the packaging/dev tools
brew install meson ninja pkg-config glib pixman libslirp gnutls libusb capstone \
             jpeg-turbo libpng zstd dylibbundler node uv
```

### 2. Get the firmware and its key

```sh
git clone https://github.com/thomas-alrek/xdj-rx-tools.git
cd xdj-rx-tools
mkdir -p build

# The official XDJ-RX 2.21 firmware update (from AlphaTheta's support site)
curl -fSL -o build/XDJ-RX_v221.zip \
  https://downloads.support.alphatheta.com/firmwares/all-in-one-dj-systems/XDJ-RX/XDJ-RX_v221.zip

# The AES key: pulled from Pioneer's published GPL source (~280 MB download, cached).
# Writes .env (XDJRX_AES_KEY=…) and build/aes256.key — both git-ignored.
uv run tools/fetch_key.py
```

### 3. Build the emulator (QEMU) and the app

```sh
qemu/build-qemu.sh                 # downloads QEMU 11.1.0, applies our patches, builds (~10 min)
packaging/build-macos-app.sh       # builds the app and assembles dist/XDJ-RX.app
open dist/XDJ-RX.app
```

### 4. First run

1. **Add the key** — drop `build/aes256.key` on the key box (or paste the 64 hex characters from
   `.env`).
2. **Drop the firmware** — drag `build/XDJ-RX_v221.zip` onto the window. The app prepares it
   (a few seconds), then powers the unit on and **installs the firmware** with Pioneer's own
   updater, like a new unit from the factory (about a minute).
3. When the update screen says *"Firmware update is complete"*, the app boots the unit from its
   flash by itself. You'll see the rekordbox home screen; from then on the app just boots it (*File → Power
   On at Launch* is on by default).

### 5. Use it

- **Controls**: click and drag the virtual panel. Hold **MENU** for UTILITY. Everything is also in
  the menu bar (Mixer, Deck 1/2, FX, Master…).
- **USB**: *File → Load USB1/USB2 Image…* (a `.img`/`.dmg` rekordbox stick image, or a real
  device). The browser shows USB1; MASTER REC records to USB2. Build an image from a rekordbox
  collection with `tools/build-rb-usb.sh`.
- **Power**: the POWER button asks the firmware to shut down cleanly (as on the unit).
  *File → Power On in Test Mode* opens the service test mode.
- **Updates**: *File → Install Firmware Update…* takes any `.UPD` (official or your own).
- **Logs**: *View → Sub uCom Log* (panel traffic and LEDs) / *View → System Log* (the Linux console).
- **Automation (MCP)**: while the app runs, an MCP server at `http://127.0.0.1:8765/mcp` exposes the
  unit to AI agents and scripts — power, `load_firmware`, every control by name, USB, screenshots,
  LEDs, console. Claude Code: `claude mcp add --transport http xdj-rx http://127.0.0.1:8765/mcp`.
  Tools: [docs/standalone-app.md §7a](docs/standalone-app.md#7a-mcp-server-automation).

## Custom firmware

```sh
cd crates/xdj-fw && cargo build --release && cd ../..     # the `xdj` tool
X=crates/xdj-fw/target/release/xdj

$X fw unpack build/XDJ-RX_v221.zip build/unpacked          # iso/ rootfs/ pioneer/ gui/ data/ user/
$EDITOR build/unpacked/rootfs/etc/rc.d/rcS                 # change, add, delete files
$X fw diff   build/XDJ-RX_v221.zip build/unpacked          # review what changed
$X fw repack build/XDJ-RX_v221.zip build/unpacked --release 2.31 -o build/MY.UPD
```

Then *File → Install Firmware Update…* → `build/MY.UPD`. The firmware's update check is only a CRC
and a model prefix — there is no signature — so Pioneer's updater installs a rebuilt image like an
official one. Best practices, the overlay workflow and the formats: [docs/custom-firmware.md](docs/custom-firmware.md).
Never commit firmware contents.

## Repository layout

| path | what |
|---|---|
| [`app/`](app) | the desktop app: `shell/` (Tauri window), `host/` (`xdj-host`: runs QEMU, the virtual-panel server, firmware prep, the NAND unit), `ui/` |
| [`crates/xdj-fw/`](crates/xdj-fw) | the firmware toolkit and `xdj` CLI: `.UPD` crypto, ISO9660/cramfs/tar codecs, custom builds, the NAND image |
| [`qemu/`](qemu) | the emulator: `xdj/` (XDJ-RX device models), `patches/` (changes to stock QEMU files), `build-qemu.sh`; plus the SD-card test bed (`build-nand.*`, `watch.sh`) |
| [`tools/`](tools) | `xdj-panel/` (panel pages, dev server, RE/debug helpers), `test/regress.py`, `fw/make-test-upd.sh`, `fetch_key.py`, `build-rb-usb.sh` |
| [`packaging/`](packaging) | `build-macos-app.sh`, `collect-licenses.py` |
| [`docs/`](docs) | technical documentation (below) |
| `run.sh` | developer quick start: build and boot the SD-card test bed in a QEMU window |

## Documentation

| doc | covers |
|---|---|
| [docs/standalone-app.md](docs/standalone-app.md) | the desktop app: architecture, provisioning, power, packaging, debugging |
| [docs/custom-firmware.md](docs/custom-firmware.md) | building, installing and verifying custom firmware |
| [docs/emulator-status.md](docs/emulator-status.md) | what works, what doesn't, open items |
| [docs/subucom-frame-map.md](docs/subucom-frame-map.md) | the front-panel control map (every button, fader, knob, encoder) |
| [docs/subucom-led-map.md](docs/subucom-led-map.md) | LEDs, level meters, jog rings |
| [docs/service-modes.md](docs/service-modes.md) | test mode, factory reset, update mode, the power/standby handshake |
| [docs/firmware-update-and-nand.md](docs/firmware-update-and-nand.md) | the updater, the panel MCU bootloader, NAND flash emulation |
| [docs/usb.md](docs/usb.md) | USB slots, images and the insert rules |
| [docs/rbp-and-panel-protocol.md](docs/rbp-and-panel-protocol.md) | the firmware's main app (`rbp`) and the panel protocol |
| [docs/debug-console-protocol.md](docs/debug-console-protocol.md) | `rbp`'s built-in debug console |
| [docs/version-display-and-override.md](docs/version-display-and-override.md) | where the displayed version comes from |
| [docs/testing.md](docs/testing.md) | the test suites and when to run them |

## Development

- **SD-card test bed** (a QEMU window, root shell on the console, gdb): `./run.sh` builds and boots
  it from scratch (also needs `brew install e2fsprogs`). Debug launcher with gdb/monitor sockets:
  `tools/xdj-panel/debug/relaunch.sh`.
- **App in development**: `(cd app/host && cargo build) && (cd app/shell && cargo run)`
  (first `cd app/ui && npm install`).
- **Tests** ([docs/testing.md](docs/testing.md)): `cargo test --release` in `crates/xdj-fw`;
  `tools/xdj-panel/test_panel_parity.py`; the end-to-end suite `tools/test/regress.py`
  (`--app dist/XDJ-RX.app` tests the packaged app).
- [CLAUDE.md](CLAUDE.md) is the orientation file for AI-assisted sessions; `.claude/skills/` holds
  task-specific how-tos (mapping controls, QEMU patches, packaging, custom firmware…).

## Platform support

| | status |
|---|---|
| macOS, Apple Silicon | ✅ supported (app ad-hoc signed — on another Mac, right-click → Open the first time) |
| macOS, Intel | ⬜ untested (the build scripts assume Homebrew in `/opt/homebrew`) |
| Linux | 🔶 the code is largely portable; no packaging script yet |
| Windows | ⬜ not yet (the host talks to QEMU over Unix sockets) |

## License

`GPL-2.0-or-later` — see [LICENSE](LICENSE). GPLv2 compatibility matters because the device models
(`qemu/xdj/`) and patches (`qemu/patches/`) are built into QEMU. The packaged app redistributes
third-party software under its own licenses ([THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md));
every license text ships inside the app in `Contents/Resources/licenses/`.

Not covered and never included: Pioneer/AlphaTheta firmware, its contents and the firmware key.
"Pioneer DJ", "XDJ-RX" and "rekordbox" are trademarks of their owners; this project is not
affiliated with or endorsed by them.

## Acknowledgements

- [QEMU](https://www.qemu.org/), [noVNC](https://novnc.com/), [Tauri](https://tauri.app/) and
  [audio-knobs](https://www.npmjs.com/package/audio-knobs).
- [nzoschke/xdj-emu](https://github.com/nzoschke/xdj-emu), an emulator for the later XDJ-RX3: its
  panel write-up (the browse encoder as a 16-bit counter in the panel frame) pointed the way for the
  XDJ-RX's encoder and several other panel fields.
- Pioneer DJ's published GPL/LGPL source release for the XDJ-RX (kernel, u-boot, drivers).
