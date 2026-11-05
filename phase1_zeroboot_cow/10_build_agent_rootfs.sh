#!/usr/bin/env bash
# Phase 1, step 1 -- build the MINIMAL rootfs Zeroboot boots & snapshots.
#
# The kernel cmdline is `init=<init_path>` (firecracker.rs) and runs that path as
# PID 1. Our PID 1 is the compiled guest agent (guest/init). For a CoW *memory*
# proof nothing else is needed in the rootfs -- no Python, no R2E repo. (Baking a
# repo waits until we have a Python-capable agent; the stock agent only does
# echo/cat over serial.)
#
#   bash 10_build_agent_rootfs.sh            # default agent = guest/init
#   AGENT=guest/init_agentzip bash 10_...    # Phase 1b agent with work/hold builtins
set -euo pipefail

ZB_DIR="${ZB_DIR:-$HOME/zeroboot}"
AGENT="${AGENT:-$ZB_DIR/guest/init}"         # compiled static PID1 binary
# Default to LOCAL disk, never NFS: CloudLab home dirs (/users, /proj) are NFS,
# and Firecracker's 512 MB snapshot write to NFS exceeds Zeroboot's 5s socket
# timeout (-> "os error 11"). /tmp is local on CloudLab images.
WORKDIR="${WORKDIR:-/tmp/zb-workdir-agent}"
SIZE_MB="${SIZE_MB:-32}"                      # agent is tiny; 32 MB is plenty
ROOTFS="$WORKDIR/rootfs.ext4"

test -x "$AGENT" || { echo "FATAL: agent binary $AGENT missing. Run 'make guest' (00_provision.sh) or build the Phase 1b agent."; exit 1; }
command -v mkfs.ext4 >/dev/null || { echo "need e2fsprogs"; exit 1; }

mkdir -p "$WORKDIR"
echo "== build $ROOTFS (${SIZE_MB} MB) with PID1 = $AGENT =="
rm -f "$ROOTFS"
truncate -s "${SIZE_MB}M" "$ROOTFS"
mkfs.ext4 -F -q "$ROOTFS"

mnt="$(mktemp -d)"
trap 'mountpoint -q "$mnt" && sudo umount "$mnt"; rmdir "$mnt" 2>/dev/null || true' EXIT
sudo mount -o loop "$ROOTFS" "$mnt"
# init.c mounts proc/sys/dev itself; it only needs the mountpoints to exist.
sudo mkdir -p "$mnt/proc" "$mnt/sys" "$mnt/dev"
sudo cp "$AGENT" "$mnt/init"
sudo chmod +x "$mnt/init"
sync
sudo umount "$mnt"; trap - EXIT; rmdir "$mnt"

echo "VERIFY: rootfs contents:"
debugfs -R 'ls -l /' "$ROOTFS" 2>/dev/null | sed 's/^/  /' || true
cat <<EOF

== rootfs built ==
  rootfs : $ROOTFS
  PID1   : /init   (pass init_path=/init to the template step)
Next: sudo WORKDIR=$WORKDIR bash 20_make_template.sh
EOF
