#!/usr/bin/env bash
# Put KSM in a known, recorded state. Every ksm experiment calls this instead of
# relying on whatever the node booted with (on CloudLab Ubuntu images
# qemu-kvm.service sets run=1 at boot; other images may differ).
#
#   sudo bash tools/ksm_ctl.sh status           # print knobs + counters as JSON
#   sudo bash tools/ksm_ctl.sh off              # unmerge everything, then stop ksmd
#   sudo bash tools/ksm_ctl.sh on               # pinned knobs below, then run=1
#   sudo PAGES_TO_SCAN=5000 bash tools/ksm_ctl.sh on   # override a knob
#
# Knobs `on` sets (env overrides). Defaults favor fast convergence for gates and
# sensitivity sweeps; the value used is always recorded by `status`.
: "${PAGES_TO_SCAN:=10000}"       # kernel default 100
: "${SLEEP_MILLISECS:=20}"        # kernel default 20 (Ubuntu image ships 200)
: "${MERGE_ACROSS_NODES:=1}"      # 1 = kernel default; changing needs all pages unmerged
: "${USE_ZERO_PAGES:=0}"          # 0 = kernel default
: "${MAX_PAGE_SHARING:=256}"      # kernel default
: "${ADVISOR_MODE:=none}"         # none = fixed pages_to_scan (reproducible)
: "${SMART_SCAN:=1}"              # kernel default
set -euo pipefail

K=/sys/kernel/mm/ksm
[ -d "$K" ] || { echo "FATAL: $K missing (kernel without CONFIG_KSM)"; exit 1; }

w() { # write knob, tolerating knobs this kernel doesn't have
    local f="$K/$1"
    if [ -e "$f" ]; then echo "$2" > "$f"; else echo "  (no $1 on this kernel; skipped)" >&2; fi
}

unmerge() {
    echo 2 > "$K/run"
    # run=2 unmerges synchronously, but give ksmd a moment and verify.
    for _ in $(seq 1 100); do
        [ "$(cat $K/pages_shared)" = 0 ] && [ "$(cat $K/pages_sharing)" = 0 ] && break
        sleep 0.1
    done
    [ "$(cat $K/pages_shared)" = 0 ] || { echo "WARN: pages_shared still $(cat $K/pages_shared)" >&2; }
}

status() {
    python3 - "$K" <<'EOF'
import json, os, sys
k = sys.argv[1]
d = {}
for f in sorted(os.listdir(k)):
    try:
        v = open(os.path.join(k, f)).read().strip()
    except OSError:
        continue
    d[f] = int(v) if v.lstrip("-").isdigit() else v
thp = "/sys/kernel/mm/transparent_hugepage"
for f in ("enabled", "shmem_enabled", "defrag"):
    try:
        d["thp_" + f] = open(os.path.join(thp, f)).read().strip()
    except OSError:
        pass
print(json.dumps(d, indent=1))
EOF
}

[ "$(id -u)" = 0 ] || [ "${1:-status}" = status ] || { echo "needs sudo"; exit 1; }

case "${1:-status}" in
    status) status ;;
    off)
        unmerge
        echo 0 > "$K/run"
        echo "KSM off (all pages unmerged)" >&2 ;;
    on)
        unmerge     # clean slate; also required before changing merge_across_nodes
        w merge_across_nodes "$MERGE_ACROSS_NODES"
        w pages_to_scan "$PAGES_TO_SCAN"
        w sleep_millisecs "$SLEEP_MILLISECS"
        w use_zero_pages "$USE_ZERO_PAGES"
        w max_page_sharing "$MAX_PAGE_SHARING"
        w advisor_mode "$ADVISOR_MODE"
        w smart_scan "$SMART_SCAN"
        echo 1 > "$K/run"
        echo "KSM on: pages_to_scan=$PAGES_TO_SCAN sleep_ms=$SLEEP_MILLISECS merge_across_nodes=$MERGE_ACROSS_NODES use_zero_pages=$USE_ZERO_PAGES" >&2 ;;
    *) echo "usage: $0 status|off|on"; exit 1 ;;
esac
