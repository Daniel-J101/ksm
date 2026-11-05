#!/usr/bin/env bash
# Phase 1, step 0 -- provision a CloudLab Ubuntu 24.04 node to build & run Zeroboot.
#
# Run ON THE CLOUDLAB NODE (WSL2 has no /dev/kvm).  sudo bash 00_provision.sh
#
# Verified against the actual Zeroboot source (not the deploy doc):
#   * the guest kernel guest/vmlinux-fc SHIPS in the repo -- no kernel download.
#   * the guest agent is guest/init.c, compiled with musl-gcc via `make guest`.
#   * Firecracker is still needed: `zeroboot template` boots a VM to snapshot it.
set -euo pipefail

FC_VERSION="${FC_VERSION:-v1.12.1}"          # Firecracker >= 1.12 (per DEPLOYMENT.md)
ARCH="$(uname -m)"
ZB_DIR="${ZB_DIR:-$HOME/zeroboot}"

echo "== 0. KVM available? =="
test -e /dev/kvm || { echo "FATAL: /dev/kvm missing. Pick a CloudLab bare-metal node with VT-x/AMD-V."; exit 1; }
ls -l /dev/kvm; echo "VERIFY: /dev/kvm present"

echo "== 1. base packages (incl. musl-tools for the guest agent, e2fsprogs for the rootfs) =="
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y build-essential pkg-config libssl-dev e2fsprogs musl-tools \
    curl jq git ca-certificates cpu-checker python3
kvm-ok || echo "WARN: kvm-ok unhappy; confirm HW virt is on"

echo "== 2. Rust toolchain =="
if ! command -v cargo >/dev/null; then
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
fi
# shellcheck disable=SC1090
source "$HOME/.cargo/env" 2>/dev/null || export PATH="$HOME/.cargo/bin:$PATH"
cargo --version; echo "VERIFY: cargo present"

echo "== 3. Firecracker $FC_VERSION =="
if ! command -v firecracker >/dev/null || [ "${FORCE_FC:-0}" = "1" ]; then
    tmp="$(mktemp -d)"
    url="https://github.com/firecracker-microvm/firecracker/releases/download/${FC_VERSION}/firecracker-${FC_VERSION}-${ARCH}.tgz"
    echo "downloading $url"
    curl -L "$url" -o "$tmp/fc.tgz"
    tar -C "$tmp" -xzf "$tmp/fc.tgz"
    install -m0755 "$tmp/release-${FC_VERSION}-${ARCH}/firecracker-${FC_VERSION}-${ARCH}" /usr/local/bin/firecracker
    rm -rf "$tmp"
fi
firecracker --version | head -1; echo "VERIFY: firecracker at $(command -v firecracker)"

echo "== 4. build Zeroboot binary + guest agent =="
test -d "$ZB_DIR" || { echo "FATAL: $ZB_DIR not found. Put the zeroboot repo there (or set ZB_DIR)."; exit 1; }
cd "$ZB_DIR"
make build                 # cargo build --release  -> target/release/zeroboot
make guest                 # musl-gcc -static ... -> guest/init  (the PID1 serial agent)
ls -l target/release/zeroboot guest/init guest/vmlinux-fc
echo "VERIFY: zeroboot binary, guest/init agent, and shipped guest/vmlinux-fc all present"

cat <<EOF

== provisioning done ==
Next (minimal agent rootfs -> snapshot -> the CoW proof):
  bash 10_build_agent_rootfs.sh    # tiny ext4 whose PID1 is guest/init
  sudo bash 20_make_template.sh    # zeroboot template ... /init  -> snapshot
  sudo bash 25_bench.sh            # zeroboot bench -> Phase 1 CoW proof (authors' own benchmark)

Zeroboot dir : $ZB_DIR
Guest kernel : $ZB_DIR/guest/vmlinux-fc   (shipped)
Guest agent  : $ZB_DIR/guest/init         (built by make guest)
EOF
