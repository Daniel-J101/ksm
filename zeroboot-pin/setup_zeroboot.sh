#!/usr/bin/env bash
# Create (or bring up to date) the zeroboot checkout the ksm experiments use:
#   pinned upstream commit + ksm patches + Firecracker CI guest kernel + build.
#
# Run as your NORMAL user (not root). 00_provision.sh calls it for you after
# installing system packages; you can also re-run it on its own at any time.
# Idempotent: already-applied patches are detected (exact tree match) and skipped; the kernel is
# only downloaded if it's missing or its checksum is wrong.
#
#   bash zeroboot-pin/setup_zeroboot.sh            # ZB_DIR defaults to ~/zeroboot
#   ZB_DIR=/local/zeroboot bash zeroboot-pin/setup_zeroboot.sh
#   SKIP_BUILD=1 bash zeroboot-pin/setup_zeroboot.sh   # checkout + patch + kernel only
set -euo pipefail

PIN_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=../env.sh
source "$PIN_DIR/../env.sh"
eval "$(grep -E '^[A-Z][A-Z0-9_]*=' "$PIN_DIR/UPSTREAM")"

if [ "$(id -u)" = 0 ]; then
    echo "FATAL: run as your normal user, not root (root-owned target/ and ~/.cargo"
    echo "       break later builds). 00_provision.sh handles the sudo parts."
    exit 1
fi

echo "== zeroboot @ ${ZB_COMMIT:0:12} -> $ZB_DIR =="
if [ ! -d "$ZB_DIR/.git" ]; then
    git clone "$ZB_REPO" "$ZB_DIR"
fi
cd "$ZB_DIR"
git cat-file -e "$ZB_COMMIT^{commit}" 2>/dev/null || git fetch origin
head="$(git rev-parse HEAD)"
if [ "$head" != "$ZB_COMMIT" ]; then
    if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
        echo "FATAL: $ZB_DIR is at ${head:0:12} (want ${ZB_COMMIT:0:12}) and has local edits."
        echo "       Commit/stash them or point ZB_DIR elsewhere."
        exit 1
    fi
    git -c advice.detachedHead=false checkout "$ZB_COMMIT"
fi

echo "== patches =="
# Patches stack (later ones touch code earlier ones added), so "is patch k
# applied?" can't be checked per patch. Instead compare git trees exactly:
# build pin+patches[0..k] in a throwaway index for each k and find which one
# the working tree (tracked files) equals; then apply only the rest.
patches=("$PIN_DIR"/patches/*.patch)
tmpidx="$(mktemp)"; trap 'rm -f "$tmpidx"' EXIT
tree_after() {  # tree of pin + first $1 patches
    GIT_INDEX_FILE="$tmpidx" git read-tree "$ZB_COMMIT"
    for ((i = 0; i < $1; i++)); do GIT_INDEX_FILE="$tmpidx" git apply --cached "${patches[$i]}"; done
    GIT_INDEX_FILE="$tmpidx" git write-tree
}
# working tree = tracked changes + new files under src/ (patches may add files there)
actual="$(GIT_INDEX_FILE="$tmpidx" git read-tree "$ZB_COMMIT" && GIT_INDEX_FILE="$tmpidx" git add -u . \
          && GIT_INDEX_FILE="$tmpidx" git add -A src && GIT_INDEX_FILE="$tmpidx" git write-tree)"
have=-1
for ((k = ${#patches[@]}; k >= 0; k--)); do
    if [ "$(tree_after $k)" = "$actual" ]; then have=$k; break; fi
done
if [ "$have" -lt 0 ]; then
    echo "FATAL: $ZB_DIR matches no prefix of the ksm patch series -- tree has diverged."
    echo "       See: git -C $ZB_DIR diff   (or move it aside and re-run)"
    exit 1
fi
for ((i = 0; i < ${#patches[@]}; i++)); do
    name="$(basename "${patches[$i]}")"
    if [ "$i" -lt "$have" ]; then
        echo "  already applied: $name"
    else
        git apply "${patches[$i]}"; echo "  applied:         $name"
    fi
done

echo "== guest kernel =="
kern="$ZB_DIR/guest/vmlinux-fc"
if ! echo "$KERNEL_SHA256  $kern" | sha256sum -c --status 2>/dev/null; then
    echo "  downloading $KERNEL_URL"
    curl -fsSL --retry 3 -o "$kern.part" "$KERNEL_URL"
    echo "$KERNEL_SHA256  $kern.part" | sha256sum -c --status \
        || { echo "FATAL: kernel checksum mismatch"; rm -f "$kern.part"; exit 1; }
    mv "$kern.part" "$kern"
fi
echo "  ok: $kern (sha256 verified)"

if [ "${SKIP_BUILD:-0}" = 1 ]; then echo "SKIP_BUILD=1: not building"; exit 0; fi

echo "== Rust $RUST_TOOLCHAIN =="
# shellcheck disable=SC1091
source "$HOME/.cargo/env" 2>/dev/null || true
if ! command -v rustup >/dev/null; then
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
        | sh -s -- -y --default-toolchain "$RUST_TOOLCHAIN" --profile minimal
    # shellcheck disable=SC1091
    source "$HOME/.cargo/env"
fi
rustup toolchain list | grep -q "^$RUST_TOOLCHAIN" \
    || rustup toolchain install "$RUST_TOOLCHAIN" --profile minimal

echo "== build =="
cargo "+$RUST_TOOLCHAIN" build --release
command -v musl-gcc >/dev/null || { echo "FATAL: musl-gcc missing (apt install musl-tools; 00_provision.sh does this)"; exit 1; }
make guest
ls -l target/release/zeroboot guest/init guest/vmlinux-fc
echo "VERIFY: zeroboot $(git rev-parse --short HEAD) + $(ls "$PIN_DIR"/patches | wc -l) ksm patches built"
