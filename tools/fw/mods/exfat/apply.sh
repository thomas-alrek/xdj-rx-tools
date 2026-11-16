#!/usr/bin/env bash
# exFAT mod: mount exFAT USB sticks through FUSE (the stock kernel has fuse.ko; the
# firmware already loads it for its own use).
#
#   tools/fw/mods/exfat/apply.sh UNPACKED OVERLAY
#
# Adds to the rootfs overlay:
#   /sbin/mount.exfat-fuse        static ARM build (build-exfat-fuse.sh)
#   /lib/udev/xdj_is_exfat        exFAT probe for the udev rule
#   /etc/udev/rules.d/12-usb-memory-auto-mount.rules
#                                 the stock rules + one exFAT rule after the HFS+ one:
#                                 same mount point and the same /proc/udev_<slot>
#                                 notification as FAT32, so rbp sees an ordinary stick.
# rbp classifies a mount by its /proc/mounts type (0x132b0c): "fuseblk" matches none of
# vfat/hfsplus/fuse and falls through to the FAT code (3), like a FAT32 stick.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
UNP="$1"; OV="$2"
BIN="$(cd "$HERE/../../../.." && pwd)/build/mods/exfat/mount.exfat-fuse"
[ -x "$BIN" ] || "$HERE/build-exfat-fuse.sh"

RULES=etc/udev/rules.d/12-usb-memory-auto-mount.rules
mkdir -p "$OV/rootfs/sbin" "$OV/rootfs/lib/udev" "$OV/rootfs/$(dirname "$RULES")"
install -m 0755 "$BIN" "$OV/rootfs/sbin/mount.exfat-fuse"
install -m 0755 "$HERE/xdj_is_exfat" "$OV/rootfs/lib/udev/xdj_is_exfat"

SRC="$UNP/rootfs/$RULES"
[ -f "$OV/rootfs/$RULES" ] && SRC="$OV/rootfs/$RULES"     # stack on an earlier mod
RULE='ACTION=="add", PROGRAM="/lib/udev/xdj_is_exfat %N", RESULT=="exfat", RUN+="/bin/sh -c '"'"'/bin/mkdir -p /media/%E{usb_slot}/%k; /sbin/modprobe fuse 2>/dev/null; /sbin/mount.exfat-fuse -o umask=0,noatime /dev/%k /media/%E{usb_slot}/%k; /root/pioneer/decrypt_autoexec.sh /media/%E{usb_slot}/%k; /bin/echo -n mount /media/%E{usb_slot}/%k > /proc/udev_%E{usb_slot}'"'"'", GOTO="media_auto_mount_end"'
awk -v rule="$RULE" '{print} /RESULT=="hfsplus"/ && !done {print "### exFAT (xdj mod) ###"; print rule; done=1}' \
  "$SRC" > "$OV/rootfs/$RULES.new"
grep -q xdj_is_exfat "$OV/rootfs/$RULES.new" || { echo "exfat: HFS+ rule not found in $SRC" >&2; exit 1; }
mv "$OV/rootfs/$RULES.new" "$OV/rootfs/$RULES"
echo "exfat: mount.exfat-fuse + udev rule"
