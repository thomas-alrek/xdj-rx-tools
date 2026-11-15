# Third-party software in the XDJ-RX desktop app

This repository is `GPL-2.0-or-later` ([LICENSE](LICENSE)). The packaged app
(`packaging/build-macos-app.sh` → `XDJ-RX.app`) also redistributes the software below, each under
its own license. Every license text ships inside the app in `Contents/Resources/licenses/` (collected
at packaging time from the exact files that were bundled).

**Never included:** Pioneer/AlphaTheta firmware, its contents, or the firmware AES key — the user
supplies them.

## QEMU 11.1.0 — `Contents/Resources/qemu-system-arm`
GPL-2.0 (some files under GPL-compatible licenses; see QEMU's `LICENSE`). Modified by this project.
**Corresponding source:** the upstream release <https://download.qemu.org/qemu-11.1.0.tar.xz> plus,
from this repository at the app's commit, `qemu/patches/` (changes to QEMU files), `qemu/xdj/`
(device models, built into the binary) and `qemu/build-qemu.sh` (the exact configure/build). Built
`--disable-libssh` so OpenSSL (Apache-2.0, GPLv2-incompatible) is not linked.

## Shared libraries QEMU links — `Contents/Resources/libs/`
Homebrew builds, bundled unmodified as separate dynamic libraries (each can be replaced, as the LGPL
requires). Source: the upstream release of the listed version.

| library (files) | version | license |
|---|---|---|
| GLib (libglib, libgio, libgobject, libgmodule) | 2.88.1 | LGPL-2.1-or-later |
| GnuTLS (libgnutls) | 3.8.13 | LGPL-2.1-or-later |
| Nettle (libnettle, libhogweed) | 4.0 | LGPL-3.0-or-later OR GPL-2.0-or-later |
| GMP (libgmp) | 6.3.0 | LGPL-3.0-or-later OR GPL-2.0-or-later |
| libidn2 | 2.3.8 | LGPL-3.0-or-later OR GPL-2.0-or-later |
| libunistring | 1.4.2 | LGPL-3.0-or-later OR GPL-2.0-or-later |
| libtasn1 | 4.21.0 | LGPL-2.1-or-later |
| gettext runtime (libintl) | 1.0 | LGPL-2.1-or-later |
| libusb | 1.0.30 | LGPL-2.1-or-later |
| p11-kit | 0.26.2 | BSD-3-Clause |
| PCRE2 (libpcre2-8) | 10.47 | BSD-3-Clause (with PCRE2 exception) |
| Capstone | 5.0.9 | BSD-3-Clause |
| libslirp | 4.9.3 | BSD-3-Clause |
| zstd | 1.5.7 | BSD-3-Clause OR GPL-2.0-only |
| pixman | 0.46.4 | MIT |
| libpng | 1.6.58 | libpng license |
| libjpeg-turbo | 3.2.0 | IJG + BSD-3-Clause + zlib |

`packaging/build-macos-app.sh` copies each library's own license files from its Homebrew keg into
`licenses/<library>/` (GLib ships none in its keg; its LGPL-2.1 text is taken from QEMU's
`COPYING.LIB`). If you rebuild against different Homebrew versions, this table changes with them.

## Web UI — `Contents/Resources/ui/`
| component | license |
|---|---|
| noVNC (`ui/novnc/`, incl. vendored pako) | MPL-2.0 (pako: MIT) — `ui/novnc/LICENSE.txt` |
| audio-knobs (`ui/knobs/`) | MIT |

## Rust crates — `MacOS/XDJ-RX`, `Resources/xdj-host`
Tauri and the other crates compiled into the two binaries (see `app/shell/Cargo.lock`,
`app/host/Cargo.lock`) are MIT, Apache-2.0, BSD, Zlib, Unicode-3.0, MPL-2.0 and similar —
all GPL-compatible for a `GPL-2.0-or-later` program. The packaging script copies every crate's
license files into `licenses/rust/<crate>-<version>/`. The webview itself is the system's WebKit,
not bundled.
