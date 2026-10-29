# numa_ksm_bench

How much slower is a process whose memory KSM has merged onto the *other*
NUMA node? This measures the NUMA-Tax from the nuKSM paper (Fig. 3) with two
native processes instead of two VMs.

Two copies of `ksm_bench` run at once: instance **A** pinned to node 0 and
instance **B** pinned to node 1. Each builds a byte-identical buffer and marks
it `MADV_MERGEABLE`. KSM keeps one physical copy of each merged page, so one
instance ends up reading memory from the other socket. After merging finishes,
the driver times accesses and compares them with the same instance running
with KSM off.

Self-contained: nothing here depends on the rest of the artifact.

## Requirements

- Linux 5.19+ with KSM (needs `/proc/<pid>/ksm_merging_pages`), at least two NUMA nodes, root
- `gcc`, `make`, `python3` (standard library only), `numactl` (provides `numastat`)
- Free memory: about 2× the largest working set per node (default 4G, so about 9 GiB per node)
- Swap off (`swapoff -a`) is recommended

```
sudo apt install build-essential numactl
make
```

## Run

Start with the smoke test, about 2 minutes:

```
sudo python3 run_bench.py quick
```

It runs a 1 GiB pointer chase with KSM off and then with KSM on. It should end
with `OK: cross-node merge left one instance with ~100% remote pages`, and the
`*` instance should show a slowdown greater than 1. If it doesn't, look in
`results/<run>/logs/` before going further.

Then the full set, unattended (about 3 hours at the default fast scan rate):

```
sudo nohup python3 run_bench.py all > all.log 2>&1 &
```

| Plan | What it varies |
|---|---|
| `quick` | Smoke test: ksm_off vs ksm_merge_all, 1G, chase, solo runs |
| `core` | 4 configs × `--sizes` × `--patterns` × `--modes` × `--reps` |
| `write` | Write fraction during the run (`--write-fracs`), with a fresh merge for every KSM run |
| `shared` | Fraction of pages identical between A and B (`--shared-fracs`) |
| `all` | `core`, then `write`, then `shared` |

Useful options: `--sizes 16M,4G`, `--reps 5`, `--duration 10`,
`--cpus 2,30` (the CPU used on node 0 and on node 1; default is the second
CPU of each node), `--paper-rate` (scan at the paper's 1000 pages / 100 ms),
`--keep-ksmd`, `--keep-numa-balancing`. See `--help` for the rest.

To re-print the tables for a finished or interrupted run:
`python3 analyze.py results/<run>`.

## What is compared

| Config | A (cpu, mem) | B (cpu, mem) | KSM | Purpose |
|---|---|---|---|---|
| `ksm_off` | 0, 0 | 1, 1 | off | Baseline: everything local |
| `ksm_merge_all` | 0, 0 | 1, 1 | on, `merge_across_nodes=1` | Stock KSM, one copy for both nodes |
| `remote_control` | 0, 0 | 1, **0** | off | B forced remote with no KSM. Should roughly match the victim's slowdown under `ksm_merge_all`. |
| `ksm_per_node` | 0, 0 | 1, 1 | on, `merge_across_nodes=0` | Stock Linux mitigation: no remote penalty, but no cross-node merging either |

Access patterns (single-threaded, timed for `--duration` seconds):

- **`chase`**: dependent loads following a random cycle through every 64 B
  line. Latency-bound worst case. Reported as ns per load.
- **`seq`**: streaming sum over the buffer. Bandwidth-bound. Reported as GB/s
  and as ns per 64 B line.

Modes: `solo_a` and `solo_b` (one instance runs, the other idles) and
`concurrent` (both run at once, so they also compete for one memory
controller after merging).

## Procedure for each session

1. Write `ksm/run=2` (unmerge everything), set `merge_across_nodes`,
   `smart_scan=0`, `advisor_mode=none` and the scan rate, and leave ksmd stopped.
2. Start A, fill its buffer, then start B and fill its buffer. Buffers use
   4 KiB pages (`MADV_NOHUGEPAGE`) in every configuration.
3. KSM configs: start ksmd and poll `/proc/<pid>/ksm_merging_pages` until
   both instances have merged every identical page, or merging plateaus.
   Then stop ksmd (`run=0` keeps merged pages merged), so its scanning
   doesn't run alongside the timed runs.
4. Record where each page lives (`move_pages` query, plus raw `numastat -p`
   in the session log).
5. Untimed warmup, then the timed runs. Reads never unmerge pages, so one
   merge serves every read-only measurement.

AutoNUMA (`kernel.numa_balancing`) is turned off for the duration so it
can't move pages mid-run. All KSM knobs and `numa_balancing` are restored at
exit, including after Ctrl-C.

## Output (`results/<timestamp>-<plan>/`)

- `runs.csv`: one row per instance per timed run
- `sessions.csv`: fill and merge times, KSM counters, page placement per session
- `summary.csv`: the median table printed at the end
- `meta.json`: kernel, `lscpu`, `numactl -H`, CPU governor, original KSM settings, arguments
- `logs/`: per-session command lines, merge progress, `numastat -p` output

In the summary, `slowdown` is the median ns/op divided by the same instance's
median in `ksm_off` for the same size, pattern, mode and write fraction.
`remote` is the share of that instance's pages that live on the other node.
The instance marked `*` is the one whose copies KSM threw away.

## Things to know

- **Either instance can become the victim.** When ksmd finds a match, it
  keeps the copy belonging to whichever process it scans second, and that
  depends on scan order rather than start order. The analysis works out the
  victim from where the pages actually are.
- **`ksm/run=2` is system-wide.** It unmerges every KSM page on the machine,
  including those of any VMs that are running. Run this benchmark on an
  otherwise idle box.
- **The write sweep shows how pages get unmerged, not a steady state.** Each
  write to a merged page takes a copy-on-write fault and gives the writer a
  local private copy, so the victim speeds up during the run.
  `merged_before` and `merged_after` show how much was undone.
- **Results depend on the machine.** The penalty tracks your local and
  remote DRAM latency (`mlc --latency_matrix` if you have Intel MLC). For
  steadier numbers, disable turbo and SMT
  (`echo off > /sys/devices/system/cpu/smt/control`) and use the
  `performance` governor; `meta.json` records what was set.
- **The scan rate doesn't affect the measured penalty.** Timing starts only
  after merging finishes. A faster rate just shortens the wait.
  `--paper-rate` matches the paper and makes each merge take about 10 minutes
  at 4 GiB.
