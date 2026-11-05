#!/usr/bin/env bash
# Phase 1, step 2 -- snapshot the rootfs into a Zeroboot template (the "snapshot"
# that every fork will CoW-share). Wraps:  zeroboot template <kernel> <rootfs>
# <workdir> [wait_secs] [init_path] [mem_mib]
#
#   sudo bash 20_make_template.sh
set -euo pipefail

ZB_DIR="${ZB_DIR:-$HOME/zeroboot}"
GUEST_DIR="${GUEST_DIR:-$ZB_DIR/guest}"
KERNEL="${KERNEL:-$GUEST_DIR/vmlinux-fc}"
WORKDIR="${WORKDIR:-$ZB_DIR/workdir-r2e}"      # from 10_build_r2e_rootfs.sh
ROOTFS="${ROOTFS:-$WORKDIR/rootfs.ext4}"
WAIT_SECS="${WAIT_SECS:-15}"                    # give init.py time to warm imports
INIT_PATH="${INIT_PATH:-/init.py}"
MEM_MIB="${MEM_MIB:-512}"                       # snapshot RAM; = per-fork naive cost
ZB="$ZB_DIR/target/release/zeroboot"

test -x "$ZB"      || { echo "build zeroboot first (00_provision.sh)"; exit 1; }
test -f "$KERNEL"  || { echo "missing kernel $KERNEL"; exit 1; }
test -f "$ROOTFS"  || { echo "missing rootfs $ROOTFS (run 10_build_r2e_rootfs.sh)"; exit 1; }

# Firecracker MUTATES the rootfs during boot -> always snapshot a fresh copy.
FRESH="$WORKDIR/rootfs.boot.ext4"
cp --reflink=auto "$ROOTFS" "$FRESH"

echo "zeroboot template $KERNEL $FRESH $WORKDIR $WAIT_SECS $INIT_PATH $MEM_MIB"
ZEROBOOT_MEM_MIB="$MEM_MIB" sudo "$ZB" template "$KERNEL" "$FRESH" "$WORKDIR" \
    "$WAIT_SECS" "$INIT_PATH" "$MEM_MIB"

echo
echo "VERIFY: snapshot files now in $WORKDIR:"
ls -lh "$WORKDIR" | sed 's/^/  /'
echo "mem_mib=$MEM_MIB  -> naive no-CoW cost of 16 forks = $((MEM_MIB*16)) MB"
echo "Next: bash 30_serve.sh"
