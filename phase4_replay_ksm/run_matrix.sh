#!/usr/bin/env bash
# Phase 4 measurement matrix for one task: REPS x {rollout, gaf} x {nocomp, ksm},
# interleaved (nocomp then ksm within each rep/cohort) so slow drift hits both.
#
#   sudo -E bash run_matrix.sh $TEMPLATES_DIR/aiohttp/f0d74880deec ../traces/aiohttp/f0d74880deec
#   sudo -E REPS=1 COHORTS=gaf bash run_matrix.sh ...
# KSM knobs: tools/ksm_ctl.sh defaults (PAGES_TO_SCAN=10000, SLEEP_MILLISECS=20), env-overridable.
# Then: python3 report.py
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
T="${1:?template dir}"; TR="${2:?traces dir (contains rollout/ gaf/)}"
: "${REPS:=3}" "${COHORTS:=rollout gaf}" "${TAG:=}"
[ "$(id -u)" = 0 ] || { echo "run with sudo -E"; exit 1; }
for rep in $(seq 0 $((REPS - 1))); do
    for c in $COHORTS; do
        for cfg in nocomp ksm; do
            echo "=== rep $rep  $c  $cfg  ($(date -u +%H:%M:%S))"
            python3 "$HERE/replay.py" --template-dir "$T" --cohort "$TR/$c" --config "$cfg" --rep "$rep" \
                ${TAG:+--tag "$TAG"} | grep -E '^\{|^wrote'
        done
    done
done
