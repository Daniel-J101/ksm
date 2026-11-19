#!/usr/bin/env bash
# Format + mount an EMPTY local disk at /mnt/data for docker images, R2E
# templates and snapshots (CloudLab's / is only ~64 GB).
#
#   sudo bash tools/setup_data_disk.sh                  # DEV=/dev/sdb MNT=/mnt/data
#   sudo DEV=/dev/nvme1n1 bash tools/setup_data_disk.sh
#
# Safety: refuses any disk that is mounted, has partitions, or carries ANY
# signature (filesystem, RAID, LVM, partition table) -- except a filesystem this
# script made (label ksmdata), which it just mounts. Safe to re-run.
set -euo pipefail

DEV="${DEV:-/dev/sdb}"
MNT="${MNT:-/mnt/data}"
LABEL=ksmdata
REAL_USER="${SUDO_USER:-$(id -un)}"

[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
[ -b "$DEV" ] || { echo "FATAL: $DEV is not a block device (set DEV=...; see lsblk)"; exit 1; }

if mountpoint -q "$MNT" && [ "$(findmnt -no SOURCE "$MNT")" = "$DEV" ]; then
    echo "already mounted: $DEV at $MNT"
elif [ "$(blkid -s LABEL -o value "$DEV" 2>/dev/null || true)" = "$LABEL" ]; then
    echo "found existing $LABEL filesystem on $DEV; mounting"
else
    # --- refuse anything that isn't a blank disk ---
    if grep -q "^$DEV[0-9p ]" /proc/mounts; then echo "FATAL: $DEV (or a partition) is mounted"; exit 1; fi
    if [ "$(lsblk -nro NAME "$DEV" | wc -l)" -gt 1 ]; then echo "FATAL: $DEV has partitions:"; lsblk "$DEV"; exit 1; fi
    if blkid -p "$DEV" >/dev/null 2>&1; then echo "FATAL: $DEV has a signature:"; blkid -p "$DEV"; exit 1; fi
    if [ -n "$(wipefs -n "$DEV")" ]; then echo "FATAL: wipefs sees signatures on $DEV:"; wipefs -n "$DEV"; exit 1; fi
    echo "formatting blank $DEV ($(lsblk -dno SIZE "$DEV")) as ext4 label=$LABEL"
    mkfs.ext4 -q -L "$LABEL" -m 0 -E lazy_itable_init=1,lazy_journal_init=1 "$DEV"
fi

mkdir -p "$MNT"
mountpoint -q "$MNT" || mount -o noatime "LABEL=$LABEL" "$MNT"
uuid="$(blkid -s UUID -o value "$DEV")"
# nofail: a node rebooted without this disk still boots.
grep -q "$uuid" /etc/fstab || echo "UUID=$uuid $MNT ext4 noatime,nofail 0 2" >> /etc/fstab

grp="$(id -gn "$REAL_USER")"
for d in docker templates traces work; do mkdir -p "$MNT/$d"; done
chown "$REAL_USER:$grp" "$MNT/templates" "$MNT/traces" "$MNT/work"
df -h "$MNT" | tail -1
echo "VERIFY: $DEV mounted at $MNT (fstab: UUID=$uuid, nofail)"
