# Phase 4 — replay traces in Zeroboot, measure what KSM saves

Replays a recorded cohort (Phase 3) into Zeroboot sandboxes (Phase 2) and samples
sandbox-owned memory every second, once with KSM off (**NoComp**) and once with
KSM on (**KSM**). This answers the headline question: how much does turning on
KSM save when N coding agents run concurrently from one template?

## Run

```bash
source ../env.sh
T=$TEMPLATES_DIR/aiohttp/f0d74880deec
sudo -E bash run_matrix.sh $T ../traces/aiohttp/f0d74880deec      # 3 reps x {rollout,gaf} x {nocomp,ksm}, ~30 min
$R2E_DIR/.venv/bin/python report.py                               # table -> results/summary.md, plots -> results/mt-*.png
# one run:
sudo -E python3 replay.py --template-dir $T --cohort ../traces/aiohttp/f0d74880deec/rollout --config ksm --rep 0
```

## Method

- **Replay.** All trajectories of the cohort start together, one sandbox each,
  forked from the task template, as in paper §5.2: Rollout's 16 run concurrently,
  GAF's 4 run concurrently. Before each recorded agent command the sandbox sits
  idle for the recorded `think_time_ms_before`. The command then runs through
  `ZerobootRuntime.run`, which wraps it exactly like R2E's `DockerRuntime.run`.
  A sandbox is deleted when its trajectory ends.
- **Configs.**
  - Both configs: fork mappings are `MADV_MERGEABLE` (patch 0003), and serve is
    pinned to NUMA node 0.
  - **NoComp:** `ksm_ctl.sh off`.
  - **KSM:** `ksm_ctl.sh on` (`pages_to_scan=10000`, `sleep_millisecs=20`,
    `merge_across_nodes=1`, `use_zero_pages=0`). This aggressive scan rate gives
    roughly an upper bound on what KSM can find.
  - Runs are interleaved NoComp/KSM within each repetition.
- **Metric** (paper §5.4, without a compression pool): sandbox-owned physical memory
  `M(t) = (serve Anonymous − its value before any sandbox) − pages_sharing × 4 KiB`.
  - `Anonymous` = the forks' copy-on-write private pages.
  - `pages_sharing` = pages KSM deduplicated. The Phase 1b gate showed serve's Pss
    drops by exactly this amount.
  - `M_avg` = average of M(t) over the replay's wall time.
  - `S = 1 − M_avg(KSM)/M_avg(NoComp)`; slowdown = wall(KSM)/wall(NoComp).
- **Fidelity.** Every replayed command's exit code is compared to the recording,
  and its output hash too. Output differences seen so far are only pytest durations,
  `ls` timestamps, object addresses and tmpfs directory order and sizes.

## Pilot results (2026-11-19)

Task aiohttp#0 (`f0d74880deec`), 3 GiB guest RAM per sandbox, DeepSeek-flash traces,
3 reps each, host E5-2683 v3 / kernel 6.18.

| cohort | sandboxes | M_avg NoComp (MiB) | M_avg KSM (MiB) | **KSM saving S** | wall NoComp (s) | slowdown | ksmd CPU (s) | exit codes match |
|---|---|---|---|---|---|---|---|---|
| Rollout | 16 | 679.7 ± 1.3 | 609.2 ± 4.4 | **10.4 ± 0.6 %** | 80.7 | 1.011× | 46.3 | 2778/2778 |
| GAF | 4 | 207.0 ± 0.5 | 199.8 ± 0.7 | **3.5 ± 0.5 %** | 79.0 | 1.008× | 42.8 | 720/720 |

![M(t) Rollout](results/mt-aiohttp-f0d74880deec-rollout.png)

Reading it:
- **Most sharing is already done by copy-on-write.** Each sandbox has 3 GiB of
  guest RAM but owns only ~40–60 MiB privately. KSM then removes another ~10% of
  that for 16 sibling sandboxes and ~3.5% for 4 GAF candidates. More, and more
  similar, siblings give KSM more byte-identical private pages to merge.
- **The cost is CPU, not latency:** ksmd used ~45 s of CPU per ~80 s run (about
  half a core, all the time) at this scan rate. Wall-time slowdown is ≈ 1%.
- **For comparison with the paper** (not like for like: the paper only reports
  KSM+zswap and zswap): Rollout 51.24% vs 48.66% with zswap alone; GAF 21.25% vs
  4.86%. Our KSM-alone numbers are consistent with its claim that exact-match
  dedup has little left after CoW.

Caveats: one small task (aiohttp, short trajectories, ~80 s); KSM at an aggressive
scan rate; a single node. Next: scan-rate sensitivity, §6.5-style similarity
analysis (how much of what KSM misses is near-duplicate), and the 10-repo collection.
