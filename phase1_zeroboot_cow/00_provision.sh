#!/usr/bin/env bash
# Phase 1, step 0 -- provision a FRESH CloudLab Ubuntu 24.04 node to build & run
# Zeroboot. Everything a new node needs is done here; nothing is done by hand.
#
#   sudo bash 00_provision.sh
#
# Root part (this script):  KVM check, apt packages, Firecracker binary, and
#                           fixing root-owned ~/.cargo ~/.rustup $ZB_DIR left by
#                           older versions of this script.
# User part (delegated):    ../zeroboot-pin/setup_zeroboot.sh as $SUDO_USER --
#                           clone pinned zeroboot, apply ksm patches, fetch the
#                           guest kernel, build zeroboot + guest/init.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=../env.sh
source "$HERE/../env.sh"
eval "$(grep -E '^[A-Z][A-Z0-9_]*=' "$KSM_ROOT/zeroboot-pin/UPSTREAM")"
ARCH="$(uname -m)"

[ "$(id -u)" = 0 ] || { echo "run with sudo: sudo bash $0"; exit 1; }
[ "$REAL_USER" != root ] || { echo "run via sudo from your normal user (SUDO_USER unset)"; exit 1; }

echo "== 0. KVM available? =="
test -e /dev/kvm || { echo "FATAL: /dev/kvm missing. Pick a CloudLab bare-metal node with VT-x/AMD-V."; exit 1; }
ls -l /dev/kvm; echo "VERIFY: /dev/kvm present"

echo "== 1. base packages (musl-tools: guest agent; e2fsprogs: rootfs) =="
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y build-essential pkg-config libssl-dev e2fsprogs musl-tools \
    curl jq git ca-certificates cpu-checker python3
kvm-ok || echo "WARN: kvm-ok unhappy; confirm HW virt is on"

echo "== 2. Firecracker $FC_VERSION =="
# MUST be v1.12.0: the snapshot layout the vmstate parser targets.
have="$(firecracker --version 2>/dev/null | head -1 | awk '{print $2}')" || true
if [ "$have" != "$FC_VERSION" ] || [ "${FORCE_FC:-0}" = 1 ]; then
    tmp="$(mktemp -d)"
    url="https://github.com/firecracker-microvm/firecracker/releases/download/${FC_VERSION}/firecracker-${FC_VERSION}-${ARCH}.tgz"
    echo "downloading $url (had: ${have:-none})"
    curl -fsSL "$url" -o "$tmp/fc.tgz"
    tar -C "$tmp" -xzf "$tmp/fc.tgz"
    install -m0755 "$tmp/release-${FC_VERSION}-${ARCH}/firecracker-${FC_VERSION}-${ARCH}" /usr/local/bin/firecracker
    rm -rf "$tmp"
fi
firecracker --version | head -1; echo "VERIFY: firecracker at $(command -v firecracker)"

echo "== 3. ownership of user build state =="
# Older provisioning ran rustup/cargo as root with HOME preserved, leaving
# root-owned ~/.cargo, ~/.rustup and $ZB_DIR/target. Hand them back.
grp="$(id -gn "$REAL_USER")"
for d in "$REAL_HOME/.cargo" "$REAL_HOME/.rustup" "$ZB_DIR"; do
    if [ -e "$d" ] && [ -n "$(find "$d" -not -user "$REAL_USER" -print -quit)" ]; then
        echo "  chown -R $REAL_USER:$grp $d"
        chown -R "$REAL_USER:$grp" "$d"
    fi
done

echo "== 4. zeroboot (as $REAL_USER) =="
sudo -u "$REAL_USER" -H env ZB_DIR="$ZB_DIR" SKIP_BUILD="${SKIP_BUILD:-0}" \
    bash "$KSM_ROOT/zeroboot-pin/setup_zeroboot.sh"

cat <<EOF

== provisioning done ==
Next (minimal agent rootfs -> snapshot -> the CoW proof):
  bash 10_build_agent_rootfs.sh    # tiny ext4 whose PID1 is guest/init
  sudo bash 20_make_template.sh    # zeroboot template ... /init  -> snapshot
  python3 tools/inspect_vmstate.py $WORKDIR/snapshot/vmstate   # parser gate
  sudo bash 25_bench.sh            # zeroboot bench -> Phase 1 CoW proof

Zeroboot dir : $ZB_DIR
Guest kernel : $ZB_DIR/guest/vmlinux-fc   (Firecracker CI 6.1.128, sha256-pinned)
Guest agent  : $ZB_DIR/guest/init         (built by make guest)
EOF
