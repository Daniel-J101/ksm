#!/usr/bin/env bash
# Phase 1, step 2 -- snapshot the rootfs into a Zeroboot template (the "snapshot"
# every fork CoW-shares). Writes <workdir>/snapshot/{mem,vmstate}.
#   zeroboot template <kernel> <rootfs> <workdir> [wait_secs] [init_path] [mem_mib]
#
#   sudo bash 20_make_template.sh
#   sudo MEM_MIB=1024 bash 20_make_template.sh     # bigger snapshot = more dramatic sharing
set -euo pipefail

ZB_DIR="${ZB_DIR:-$HOME/zeroboot}"
KERNEL="${KERNEL:-$ZB_DIR/guest/vmlinux-fc}"   # ships in the repo
WORKDIR="${WORKDIR:-/tmp/zb-workdir-agent}"   # LOCAL disk, not NFS (see 10_build_agent_rootfs.sh)
ROOTFS="${ROOTFS:-$WORKDIR/rootfs.ext4}"
WAIT_SECS="${WAIT_SECS:-5}"                     # agent reaches idle in <1s; 5 is safe
INIT_PATH="${INIT_PATH:-/init}"                 # our PID1 is the compiled agent, NOT /init.py
MEM_MIB="${MEM_MIB:-512}"                        # snapshot RAM; naive no-CoW cost per fork
ZB="$ZB_DIR/target/release/zeroboot"

test -x "$ZB"     || { echo "build zeroboot first (00_provision.sh)"; exit 1; }
test -f "$KERNEL" || { echo "missing kernel $KERNEL"; exit 1; }
test -f "$ROOTFS" || { echo "missing rootfs $ROOTFS (run 10_build_agent_rootfs.sh)"; exit 1; }

# Firecracker MUTATES the rootfs during boot -> always snapshot a fresh copy.
FRESH="$WORKDIR/rootfs.boot.ext4"
cp "$ROOTFS" "$FRESH"

echo "zeroboot template $KERNEL $FRESH $WORKDIR $WAIT_SECS $INIT_PATH $MEM_MIB"
ZEROBOOT_MEM_MIB="$MEM_MIB" "$ZB" template "$KERNEL" "$FRESH" "$WORKDIR" \
    "$WAIT_SECS" "$INIT_PATH" "$MEM_MIB"

echo; echo "VERIFY: snapshot written:"
ls -lh "$WORKDIR/snapshot" 2>/dev/null | sed 's/^/  /' || { echo "  no snapshot/ -- template failed; re-run with ZEROBOOT_DEBUG=1"; exit 1; }
echo "mem_mib=$MEM_MIB  ->  naive no-CoW cost of 16 forks = $((MEM_MIB*16)) MB, of 1000 forks = $((MEM_MIB*1000)) MB"
echo "Next: sudo WORKDIR=$WORKDIR bash 25_bench.sh"
