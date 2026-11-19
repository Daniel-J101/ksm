#!/usr/bin/env bash
# Phase 1, step 3 -- start the Zeroboot API server that holds the snapshot and
# forks VMs in-process.  zeroboot serve <workdir>[,lang:workdir2,...] [port]
#
# Runs in the foreground; open a second shell for 40_cow_probe.py, or use
# BACKGROUND=1 to detach and write a pidfile.
#
#   sudo bash 30_serve.sh                 # foreground
#   sudo BACKGROUND=1 bash 30_serve.sh    # detached; pid/log in $WORKDIR/serve.{pid,log}
#   sudo bash 30_serve.sh stop            # stop the detached server
#   sudo ZEROBOOT_KSM_MERGEABLE=1 BACKGROUND=1 bash 30_serve.sh   # forks KSM-scannable
set -euo pipefail

source "$(cd "$(dirname "$0")" && pwd)/../env.sh"   # ZB_DIR, WORKDIR, ZB defaults
PORT="${PORT:-8080}"
HERE="$(cd "$(dirname "$0")" && pwd)"

test -x "$ZB" || { echo "build zeroboot first (00_provision.sh)"; exit 1; }

# Every live fork holds KVM VM + vCPU fds; the default 1024 soft limit caps
# concurrent forks well below what the sweeps can request.
ulimit -n "${NOFILE:-65536}" 2>/dev/null || ulimit -n "$(ulimit -Hn)"

# No api_keys.json -> auth disabled (fine for a single-tenant benchmark node).
# To require a key: echo '["zb_live_phase1"]' > api_keys.json and export
# ZEROBOOT_API_KEYS_FILE + ZEROBOOT_API_KEY.
export ZEROBOOT_API_KEYS_FILE="${ZEROBOOT_API_KEYS_FILE:-$HERE/api_keys.json}"

[ "$(id -u)" = 0 ] || { echo "run with sudo: sudo bash $0"; exit 1; }
# pid/log live next to the snapshot (local disk), not in the shared ksm dir.
PIDF="$WORKDIR/serve.pid"; LOG="$WORKDIR/serve.log"

if [ "${1:-}" = stop ]; then
    if [ -f "$PIDF" ] && kill "$(cat "$PIDF")" 2>/dev/null; then
        echo "stopped serve pid $(cat "$PIDF")"; rm -f "$PIDF"
    else
        echo "no running serve recorded in $PIDF"
    fi
    exit 0
fi

# ZEROBOOT_KSM_MERGEABLE=1 (patch 0003) makes every fork mapping KSM-scannable.
# Exported here because we run the binary directly (a nested sudo would drop it).
export ZEROBOOT_KSM_MERGEABLE="${ZEROBOOT_KSM_MERGEABLE:-0}"

echo "serving $WORKDIR on 127.0.0.1:$PORT  (KSM_MERGEABLE=$ZEROBOOT_KSM_MERGEABLE)"
if [ "${BACKGROUND:-0}" = "1" ]; then
    nohup "$ZB" serve "$WORKDIR" "$PORT" >"$LOG" 2>&1 &
    echo $! > "$PIDF"
    for _ in $(seq 1 50); do curl -sf "localhost:$PORT/v1/health" >/dev/null && break; sleep 0.2; done
    echo "pid $(cat "$PIDF")  log $LOG"
    curl -s "localhost:$PORT/v1/health" | (jq -c . 2>/dev/null || cat) || \
        echo "health not ready yet; tail $LOG"
else
    exec "$ZB" serve "$WORKDIR" "$PORT"
fi
