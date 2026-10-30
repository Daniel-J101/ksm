# agentzip_ksm_bench

How much memory can **KSM** actually reclaim across a cohort of AI-agent
sandboxes? This measures the **KSM baseline** that the AgentZip paper
(`../agentzip.pdf`) argues is weak for high-fanout agent workloads: KSM only
merges *byte-identical* 4 KiB pages, and most exact sharing is already handled
by copy-on-write at sandbox creation (§2.2, §6.1).

You do **not** need an LLM. The LLM in the paper only decides which commands an
agent runs; KSM savings depend only on the *memory contents* of the sibling
sandboxes. We drive each sandbox with a deterministic, LLM-free workload and
measure what KSM merges.

Self-contained; nothing here depends on the rest of the nuKSM artifact (it only
borrows the `/sys/kernel/mm/ksm` control pattern from `../numa_ksm_bench`).

## What a "sandbox" and a "cohort" are here

- **Sandbox** = one R2E-Gym docker container (`docker_image` from the dataset),
  with the target repo already checked out at `/testbed`. This is the paper's
  copy-on-write *template* model, with a docker image standing in for the
  Firecracker snapshot.
- **Cohort** = N sibling sandboxes measured together:
  - `identical` — the same task ×N. Upper bound; proves the pipeline merges.
  - `siblings` — N *different* tasks from one repo. Shared template/libraries
    but divergent private pages — the realistic regime where KSM leaves
    savings on the table and AgentZip's delta/dictionary codecs would win.

## Requirements (set these when you instantiate the CloudLab node)

This is the part CloudLab controls, so get it right in the profile:

- **Kernel ≥ 5.17** for `PR_SET_MEMORY_MERGE` (transparent per-container KSM
  without modifying the apps). **≥ 5.19** also gives `/proc/<pid>/ksm_merging_pages`
  for per-sandbox merge counts. Ubuntu 22.04's default kernel is 5.15 — pick
  **Ubuntu 24.04**, or install the HWE/6.x kernel, or boot the nuKSM kernel.
- **root** (writes `/sys/kernel/mm/ksm`).
- **native Docker** (`apt install docker.io`), not Docker Desktop.
- An **otherwise-idle node** — KSM's `pages_sharing`/`pages_shared` counters
  are system-wide, and `run=2` unmerges every KSM page on the machine.
- Enough RAM for N × (image working set). A 2-socket Xeon CloudLab node (e.g.
  `c6420`, `r650`) with ≥128 GB comfortably runs the paper's 16-way fanout and
  is also the right shape for the nuKSM NUMA experiments.
- `swapoff -a` recommended so measured memory is real residency.

Check readiness:
```bash
uname -r                                   # want >= 5.17
ls /sys/kernel/mm/ksm/run                  # KSM present
ls /proc/self/ksm_merging_pages            # per-proc counters (>= 5.19)
docker info >/dev/null && echo docker-ok
pip install datasets                        # for fetch_tasks.py
```

## Run

```bash
# 1. Pick tasks. List what repos exist, then build a siblings cohort:
python3 fetch_tasks.py --list-repos
python3 fetch_tasks.py --repo scrapy --num 8 --out tasks_scrapy.json

# 2. Measure. Start with the identical cohort (sanity: should merge a lot):
sudo python3 run_agentzip_ksm.py --manifest tasks_scrapy.json --cohort identical -n 4

# 3. The real measurement: divergent siblings.
sudo python3 run_agentzip_ksm.py --manifest tasks_scrapy.json --cohort siblings -n 8
```

Useful flags: `--hold-secs` (how long sandboxes keep memory resident),
`--pages-to-scan` / `--sleep-ms` (ksmd scan rate; faster = shorter wait, same
result), `--test-cmd` (pytest args per sandbox), `--merge-max`. See `--help`.

## What it does each run

1. Save and reset KSM: `run=2` (unmerge all), `merge_across_nodes=1`,
   `advisor_mode=none`, `smart_scan=0`, fixed scan rate, ksmd stopped.
2. `docker run` N sandboxes. Each runs `container_entry.py`, which:
   `prctl(PR_SET_MEMORY_MERGE)` so its anon memory is KSM-eligible → optionally
   applies the task's GT patch → imports the package and runs its pytest
   collection in-process (memory need not pass tests, only be resident) →
   touches a ready-file and holds.
3. Record each sandbox's anon memory (cgroup `memory.stat anon`).
4. Start ksmd; poll `pages_sharing` until it plateaus; stop ksmd (merged pages
   stay merged).
5. Report **memory reclaimed = `pages_sharing` × 4 KiB**, `pages_shared`
   (unique pages kept), and the saving as a % of cohort anon memory.
6. Restore all KSM settings and remove containers (even on Ctrl-C/error).

## Output (`results/<timestamp>-<cohort>/`)

- `summary.json` — pages_sharing/shared, MiB reclaimed, % of anon, merge time.
- `runs.csv` — per-sandbox anon memory before/after.
- `scratch/` — staged GT patches and sandbox ready-files.

## Reading the result (and connecting it to the paper)

`pages_sharing × 4 KiB` is what KSM reclaims — its *realized* dedup. The
`identical` cohort shows KSM's best case. The gap between `identical` and
`siblings` is the paper's whole point: once sandboxes diverge, exact-page
sharing collapses even though the pages stay *similar*. That residual
similarity — template-relative and cross-sandbox — is what KSM cannot touch and
what AgentZip's template-delta and cohort-dictionary codecs target (paper §6.1,
§6.5). To quantify that residual (the 76–96% redundancy figure), the natural
next step is an offline page-identity vs page-similarity analysis over the same
cohort; ask and it can be added here.

## Caveats

- `PR_SET_MEMORY_MERGE` is reset by `execve()`, so `container_entry.py` sets it
  in its own (final) process and keeps the workload in-process. A sandbox whose
  real memory lives in exec'd subprocesses would need the flag set there too.
- `extract_patch()` in `fetch_tasks.py` guesses the shape of
  `parsed_commit_content`; verify it on-node. Without a patch, sandboxes run on
  the clean tree and still produce realistic memory — you just lose the GT edit.
- Counters are system-wide: run on an idle node.
