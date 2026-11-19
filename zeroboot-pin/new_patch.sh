#!/usr/bin/env bash
# Write the changes in $ZB_DIR that are NOT yet covered by the ksm patch series
# as the next numbered patch.
#
#   bash zeroboot-pin/new_patch.sh restore-guest-clock     # -> patches/000N-restore-guest-clock.patch
#   bash zeroboot-pin/new_patch.sh --dry-run               # just show the pending diff stat
#
# The diff is taken against (pinned commit + every existing patch), so it
# applies cleanly on top of the series however the patches overlap.
set -euo pipefail

PIN_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=../env.sh
source "$PIN_DIR/../env.sh"
eval "$(grep -E '^[A-Z][A-Z0-9_]*=' "$PIN_DIR/UPSTREAM")"
[ $# -eq 1 ] || { echo "usage: $0 <short-name> | --dry-run"; exit 1; }

cd "$ZB_DIR"
patches=("$PIN_DIR"/patches/*.patch)
tmpidx="$(mktemp)"; trap 'rm -f "$tmpidx"' EXIT
GIT_INDEX_FILE="$tmpidx" git read-tree "$ZB_COMMIT"
for p in "${patches[@]}"; do GIT_INDEX_FILE="$tmpidx" git apply --cached "$p"; done
series_tree="$(GIT_INDEX_FILE="$tmpidx" git write-tree)"

# Working tree as a git tree: tracked changes anywhere + NEW files under src/
# (other untracked files, e.g. agent binaries built into guest/, stay out).
GIT_INDEX_FILE="$tmpidx" git read-tree "$ZB_COMMIT"
GIT_INDEX_FILE="$tmpidx" git add -u .
GIT_INDEX_FILE="$tmpidx" git add -A src
work_tree="$(GIT_INDEX_FILE="$tmpidx" git write-tree)"

if [ "$series_tree" = "$work_tree" ]; then
    echo "nothing to do: $ZB_DIR already equals pin + ${#patches[@]} patches"; exit 0
fi
git diff --stat "$series_tree" "$work_tree"
[ "$1" = --dry-run ] && exit 0

next=$(printf '%04d' $(( ${#patches[@]} + 1 )))
out="$PIN_DIR/patches/$next-$1.patch"
git diff "$series_tree" "$work_tree" > "$out"
echo "wrote $out -- re-run setup_zeroboot.sh on a fresh ZB_DIR to confirm it applies"
