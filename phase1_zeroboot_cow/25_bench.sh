#!/usr/bin/env bash
# Phase 1, step 3 (PRIMARY DELIVERABLE) -- prove CoW sharing with Zeroboot's own
# `bench`. No guest code, no HTTP, no custom agent needed: bench forks many VMs
# from the one snapshot and reports process RSS, then runs an isolation test.
#
# What to read in the output:
#   Phase 4 "Concurrent fork test": for 10 / 100 / 1000 forks it prints total
#     process RSS. If forks CoW-share the snapshot, RSS stays ~flat (~one
#     snapshot) as the count grows 100x -- that is the sharing proof. The naive
#     no-CoW cost would be count x mem_mib.
#   Phase 5 "Memory isolation test": fork B cannot read fork A's secret, and A is
#     unchanged after B writes -- CoW gives private copies on write.
#
#   sudo bash 25_bench.sh
set -euo pipefail

ZB_DIR="${ZB_DIR:-$HOME/zeroboot}"
WORKDIR="${WORKDIR:-$ZB_DIR/workdir-agent}"
ZB="$ZB_DIR/target/release/zeroboot"
HERE="$(cd "$(dirname "$0")" && pwd)"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
OUT="$HERE/results/bench-$STAMP.txt"

test -x "$ZB" || { echo "build zeroboot first (00_provision.sh)"; exit 1; }
test -f "$WORKDIR/snapshot/mem" || { echo "no snapshot in $WORKDIR (run 20_make_template.sh)"; exit 1; }
mkdir -p "$HERE/results"

snap_mib=$(( $(stat -c%s "$WORKDIR/snapshot/mem") / 1024 / 1024 ))
echo "snapshot = ${snap_mib} MiB   (naive no-CoW: 1000 forks would need $((snap_mib*1000)) MiB)"
echo "running: $ZB bench $WORKDIR"
echo

# bench prints the table to stdout and progress to stderr; keep both.
"$ZB" bench "$WORKDIR" 2>&1 | tee "$OUT"

echo
echo "================= Phase 1 CoW reading ================="
echo "Concurrent-fork RSS (should stay ~${snap_mib} MiB as count goes 10 -> 1000):"
grep -E "concurrent:" "$OUT" | sed 's/^/  /' || echo "  (no concurrent lines -- check $OUT)"
echo "Isolation:"
grep -E "Isolation|isolation" "$OUT" | sed 's/^/  /' || echo "  (no isolation lines -- check $OUT)"
echo
echo "Interpretation: flat RSS across a 100x fork-count increase = forks share the"
echo "snapshot via mmap(MAP_PRIVATE) CoW; PASS isolation = writes get private pages."
echo "saved: $OUT"
