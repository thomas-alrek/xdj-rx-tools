#!/usr/bin/env bash
#
# install-vmnet-bridge.sh — put the emulated XDJ-RX on your LAN (macOS).
#
# Optional: without it the unit runs on QEMU's private user-mode NAT, which is all
# you need unless another machine has to reach the unit.
#
# Installs lima-vm's socket_vmnet (pinned release, checksum-verified) to
# /opt/socket_vmnet and a launchd daemon that bridges one host interface:
#
#   /var/run/socket_vmnet.bridged.<ifname>   (group staff, so QEMU runs as you)
#
# The desktop app (xdj-host) and qemu/watch.sh find that socket on every power-on
# and attach the unit's Ethernet port to it: the unit gets its own DHCP lease from
# your router and is reachable from any machine on the network. See README.md "Networking".
#
# Only this one-time install uses sudo (vmnet bridging needs root, or an Apple-granted
# entitlement); the emulator itself is still launched as you.
#
# Or, with `mac`, a network between the unit and this Mac only (vmnet shared mode,
# /var/run/socket_vmnet): the Mac is 192.168.105.1, macOS's DHCP server leases the unit
# an address, broadcasts reach apps on the Mac (rekordbox LINK), and the unit reaches the
# internet through NAT. Needs no LAN and works while the Mac is on Wi-Fi.
#
#   tools/net/install-vmnet-bridge.sh              # bridge the default-route interface
#   tools/net/install-vmnet-bridge.sh en1          # bridge a specific interface
#   tools/net/install-vmnet-bridge.sh mac          # unit <-> this Mac only
#   tools/net/install-vmnet-bridge.sh --uninstall [ifname|mac]
#
set -euo pipefail

VER=1.2.2
PREFIX=/opt/socket_vmnet

[ "$(uname)" = Darwin ] || { echo "macOS only" >&2; exit 1; }
[ "$(id -u)" != 0 ] || { echo "run as yourself; the script calls sudo where it must" >&2; exit 1; }

UNINSTALL=0
if [ "${1:-}" = --uninstall ]; then UNINSTALL=1; shift; fi
IFN="${1:-$(route -n get default 2>/dev/null | awk '/interface:/{print $2; exit}')}"
[ -n "$IFN" ] || { echo "no default-route interface; pass one (e.g. en0)" >&2; exit 1; }

if [ "$IFN" = mac ]; then   # upstream's default (shared-mode) daemon
  LABEL="io.github.lima-vm.socket_vmnet"
  SOCK="/var/run/socket_vmnet"
  LOG=shared
  VMNET_ARGS="<string>--vmnet-gateway=192.168.105.1</string>"
  WHAT="the Mac-only network"
else
  ifconfig "$IFN" >/dev/null 2>&1 || { echo "no such interface: $IFN" >&2; exit 1; }
  LABEL="io.github.lima-vm.socket_vmnet.bridged.$IFN"
  SOCK="/var/run/socket_vmnet.bridged.$IFN"
  LOG="bridged.$IFN"
  VMNET_ARGS="<string>--vmnet-mode=bridged</string>
    <string>--vmnet-interface=$IFN</string>"
  WHAT="the $IFN bridge"
fi
PLIST="/Library/LaunchDaemons/$LABEL.plist"

if [ "$UNINSTALL" = 1 ]; then
  sudo launchctl bootout "system/$LABEL" 2>/dev/null || true
  sudo rm -f "$PLIST" "$SOCK"
  echo "removed the daemon for $WHAT ($PREFIX is left in place; sudo rm -rf $PREFIX to remove it)"
  exit 0
fi

# 1. socket_vmnet itself, root-owned under /opt (a root daemon must not run a
#    user-writable binary, so not Homebrew's copy).
if [ "$("$PREFIX/bin/socket_vmnet" --version 2>/dev/null | awk '{print $NF}')" != "$VER" ]; then
  ARCH="$(uname -m)"
  case "$ARCH" in   # the release's SHA256SUMS (macOS bash 3.2: no associative arrays)
    arm64)  SHA=c7bf62308fbcfdc29bdfb8373c9b1951f7ac2396446e4390919796a94972e6dc ;;
    x86_64) SHA=2968a82c97e692c2d36f87230152e8018e00589c1b598e8257775adfe83800a1 ;;
    *) echo "unsupported arch $ARCH" >&2; exit 1 ;;
  esac
  TGZ="socket_vmnet-$VER-$ARCH.tar.gz"
  TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
  echo "==> downloading socket_vmnet $VER ($ARCH)"
  curl -fsSL -o "$TMP/$TGZ" "https://github.com/lima-vm/socket_vmnet/releases/download/v$VER/$TGZ"
  echo "$SHA  $TMP/$TGZ" | shasum -a 256 -c - >/dev/null \
    || { echo "checksum mismatch for $TGZ" >&2; exit 1; }
  echo "==> installing $PREFIX (sudo)"
  sudo tar Cxzf / "$TMP/$TGZ" opt/socket_vmnet
fi

# 2. The launchd daemon (upstream's plist for this mode).
echo "==> installing launchd daemon $LABEL (sudo)"
sudo mkdir -p /var/log/socket_vmnet
sudo tee "$PLIST" >/dev/null <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>Program</key><string>$PREFIX/bin/socket_vmnet</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PREFIX/bin/socket_vmnet</string>
    $VMNET_ARGS
    <string>$SOCK</string>
  </array>
  <key>StandardErrorPath</key><string>/var/log/socket_vmnet/$LOG.stderr</string>
  <key>StandardOutPath</key><string>/var/log/socket_vmnet/$LOG.stdout</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>UserName</key><string>root</string>
  <key>ProcessType</key><string>Interactive</string>
</dict>
</plist>
EOF
sudo chown root:wheel "$PLIST"; sudo chmod 644 "$PLIST"
sudo launchctl bootout "system/$LABEL" 2>/dev/null || true
sudo launchctl bootstrap system "$PLIST"
sudo launchctl enable "system/$LABEL"

for _ in $(seq 1 20); do [ -S "$SOCK" ] && break; sleep 0.25; done
if [ -S "$SOCK" ]; then
  echo "==> ready: $SOCK ($WHAT)"
  echo "    power the unit on (or off and on) to attach it."
else
  echo "!! $SOCK did not appear; see /var/log/socket_vmnet/$LOG.stderr" >&2; exit 1
fi
