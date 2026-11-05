#!/usr/bin/env bash
# Phase 1, step 0 -- provision a CloudLab Ubuntu 24.04 node to build & run Zeroboot.
#
# Run ON THE CLOUDLAB NODE (not the WSL dev box -- WSL2 has no /dev/kvm).
#   sudo bash 00_provision.sh
#
# Installs: KVM check, build deps, Rust, Firecracker >=1.12, a guest kernel,
# and builds Zeroboot from source. Each piece prints a VERIFY line; if any fails,
# fix it before moving on -- this script is intentionally fail-loud (set -e).
set -euo pipefail

FC_VERSION="${FC_VERSION:-v1.12.1}"          # Firecracker >= 1.12 (per Zeroboot docs)
ARCH="$(uname -m)"                            # x86_64 on the CloudLab node
ZB_DIR="${ZB_DIR:-$HOME/zeroboot}"
GUEST_DIR="${GUEST_DIR:-$ZB_DIR/guest}"

echo "== 0. KVM available? =="
test -e /dev/kvm || { echo "FATAL: /dev/kvm missing. Enable VT-x/AMD-V; on CloudLab pick a bare-metal node."; exit 1; }
ls -l /dev/kvm
echo "VERIFY: /dev/kvm present"

echo "== 1. base packages =="
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y build-essential pkg-config libssl-dev e2fsprogs \
    curl jq git ca-certificates docker.io cpu-checker python3
kvm-ok || echo "WARN: kvm-ok unhappy; continuing but confirm nested/HW virt"

echo "== 2. Rust toolchain =="
if ! command -v cargo >/dev/null; then
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
fi
# shellcheck disable=SC1090
source "$HOME/.cargo/env" 2>/dev/null || export PATH="$HOME/.cargo/bin:$PATH"
cargo --version
echo "VERIFY: cargo present"

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
firecracker --version | head -1
echo "VERIFY: firecracker installed at $(command -v firecracker)"

echo "== 4. Zeroboot source + build =="
if [ ! -d "$ZB_DIR/.git" ]; then
    git clone https://github.com/zerobootdev/zeroboot "$ZB_DIR"
fi
cd "$ZB_DIR"
cargo build --release
ls -l target/release/zeroboot
echo "VERIFY: zeroboot binary built"

echo "== 5. guest kernel (vmlinux) =="
mkdir -p "$GUEST_DIR"
# Zeroboot's template command wants a bootable vmlinux (its docs reference
# guest/vmlinux-fc). If the repo ships one under guest/, use it. Otherwise grab
# a Firecracker CI kernel. VERIFY the exact path Zeroboot expects on-node.
if [ ! -f "$GUEST_DIR/vmlinux-fc" ]; then
    if [ -f "$ZB_DIR/guest/vmlinux-fc" ]; then
        echo "using repo-provided guest/vmlinux-fc"
    else
        kurl="https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.11/${ARCH}/vmlinux-6.1.102"
        echo "downloading CI kernel $kurl (verify >=5.10 and Zeroboot-compatible)"
        curl -L "$kurl" -o "$GUEST_DIR/vmlinux-fc" || {
            echo "WARN: kernel download failed. Supply a vmlinux at $GUEST_DIR/vmlinux-fc";
        }
    fi
fi
ls -l "$GUEST_DIR/vmlinux-fc" 2>/dev/null && echo "VERIFY: guest kernel present" || echo "TODO: provide $GUEST_DIR/vmlinux-fc"

cat <<EOF

== provisioning done ==
Next:
  bash 10_build_r2e_rootfs.sh   # bake one R2E repo into a bootable rootfs
  bash 20_make_template.sh      # snapshot it  (zeroboot template ...)
  bash 30_serve.sh              # zeroboot serve ...
  sudo python3 40_cow_probe.py  # the CoW proof

Zeroboot dir : $ZB_DIR
Guest kernel : $GUEST_DIR/vmlinux-fc
EOF
