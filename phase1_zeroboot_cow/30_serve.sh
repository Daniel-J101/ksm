#!/usr/bin/env bash
# Phase 1, step 3 -- start the Zeroboot API server that holds the snapshot and
# forks VMs in-process.  zeroboot serve <workdir>[,lang:workdir2,...] [port]
#
# Runs in the foreground; open a second shell for 40_cow_probe.py, or use
# BACKGROUND=1 to detach and write a pidfile.
#
#   sudo bash 30_serve.sh                 # foreground
#   sudo BACKGROUND=1 bash 30_serve.sh    # detached; pid in serve.pid
set -euo pipefail

ZB_DIR="${ZB_DIR:-$HOME/zeroboot}"
WORKDIR="${WORKDIR:-$ZB_DIR/workdir-r2e}"
PORT="${PORT:-8080}"
ZB="$ZB_DIR/target/release/zeroboot"
HERE="$(cd "$(dirname "$0")" && pwd)"

test -x "$ZB" || { echo "build zeroboot first (00_provision.sh)"; exit 1; }

# No api_keys.json -> auth disabled (fine for a single-tenant benchmark node).
# To require a key: echo '["zb_live_phase1"]' > api_keys.json and export
# ZEROBOOT_API_KEYS_FILE + ZEROBOOT_API_KEY.
export ZEROBOOT_API_KEYS_FILE="${ZEROBOOT_API_KEYS_FILE:-$HERE/api_keys.json}"

echo "serving $WORKDIR on 127.0.0.1:$PORT"
if [ "${BACKGROUND:-0}" = "1" ]; then
    nohup sudo "$ZB" serve "$WORKDIR" "$PORT" >"$HERE/serve.log" 2>&1 &
    echo $! > "$HERE/serve.pid"
    sleep 2
    echo "pid $(cat "$HERE/serve.pid")  log $HERE/serve.log"
    curl -s "localhost:$PORT/v1/health" | (jq . 2>/dev/null || cat) || \
        echo "health not ready yet; tail $HERE/serve.log"
else
    exec sudo "$ZB" serve "$WORKDIR" "$PORT"
fi
