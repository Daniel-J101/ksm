# ROADMAP — measure how much KSM saves when many coding agents share a Zeroboot template

## Context

Goal: reproduce AgentZip's sandbox setup (`ksm/agentzip.pdf`, §5) closely enough to
answer **"how much memory does turning on KSM save when N agents run concurrently
from one template?"**, plus the paper's supporting analyses (§6.5 similarity).

Decided with the user:
- **Configs:** NoComp vs **KSM** only (no zswap, no AgentZip codecs for now).
- **Workload:** run a real DeepSeek agent on R2E-Gym tasks **once**, record traces,
  then **replay** them as often as needed (paper §5.3).
- **Scale:** pilot (1 repo × 1 task) → full (1 task × 10 repos, paper's list).
- Everything must be reproducible on fresh CloudLab nodes: scripts/patches in `ksm`,
  Zeroboot changes as `zeroboot-pin/patches/000N-*.patch`, node settings in scripts
  and listed in the root README's "What provisioning changes" table.

Paper facts we're matching:
- Zeroboot CoW template per workload.
- **Rollout** = 16 trajectories/task from the same prompt at varied temperatures.
- **GAF** = 4 trajectories/task with different role prompts (minimal patch, failure
  reproduction, test-guided repair, root-cause diagnosis) and temperatures.
- Trace = tool actions plus `think_time_ms_before`, replayed identically per config.
- Metric `M(t)` = sandbox-owned physical memory, sampled every 1 s → `M_avg`;
  saving `S = 1 − M_avg(KSM)/M_avg(NoComp)`; wall-time slowdown.
- Their KSM+zswap result: 51.24% saving on Rollout and 21.25% on GAF. They don't
  report KSM alone, so our number is new. Expect it below theirs: the paper argues
  CoW already captures most exact duplicates.

## What I found in the code that shapes the plan

1. **KSM can't touch forks today.** Fork memory is `mmap(MAP_PRIVATE|MAP_NORESERVE, memfd)`
   (`zeroboot/src/vmm/kvm.rs:67-70`) with no `MADV_MERGEABLE`, so `ksmd` never scans
   it. Fix: madvise in `fork_cow` (patch), or `prctl(PR_SET_MEMORY_MERGE)` (host
   kernel 6.18 supports it). Whether KSM merges CoW'd anon pages inside a private
   memfd mapping must be **verified empirically first**.
2. **KSM is already on on this node** (`/sys/kernel/mm/ksm/run=1`). Every
   measurement must set KSM state explicitly through a script.
3. **Forks have no disk and no network:** only serial port I/O, in-kernel irqchip
   + PIT, 1 vCPU. MMIO (virtio-blk) reads return `0xff`. So a sandbox's whole
   filesystem (repo + Python env) must be **in guest RAM at snapshot time**
   (tmpfs/initramfs root), not on a block device.
4. **Forks are ephemeral:** one fork per `/v1/exec`, dropped after
   (`api/handlers.rs:310`). HLT with no pending input ends the run
   (`kvm.rs:262-270`). Agents need **persistent** sandboxes across many tool calls.
5. **The stock guest agent only does echo/cat.** We need a real exec/file protocol.
6. Node: 2 NUMA nodes, 56 CPUs, 251 GB. KSM's `merge_across_nodes` and pinning matter.

## Phases

### Phase 1 — CoW substrate ✅ done
Zeroboot pinned, patched and reproducible; `bench` proves sharing and isolation.

### Phase 1b — CoW under execution + **KSM feasibility gate** ✅ done (2026-11-19)
Gate passed: with patch 0003, KSM merged 458 MiB of the ideal 448 MiB (N=8 × 64 MiB
identical pages); 0 without the patch; serve Pss drop = `pages_sharing`. See
`phase1_zeroboot_cow/README.md` → Phase 1b. Original plan:
- Run the existing 1b plan (`init_agentzip.c`, `30_serve.sh`, `40_cow_probe.py`)
  with KSM explicitly **off**.
- Add `ksm_ctl.sh` (off / on / unmerge / status). It pins knobs (`pages_to_scan`,
  `sleep_millisecs`, `merge_across_nodes`, `use_zero_pages`) and records them in
  results.
- Add patch `0003-fork-mergeable` (madvise `MADV_MERGEABLE` on fork mappings,
  behind an env flag) or a prctl in `serve`.
- Extend `work` to optionally write an identical, non-zero pattern in every fork.
- **Gate:** N forks write identical data, then KSM on → `pages_sharing` rises,
  serve-process memory drops ≈ (N−1)×touched. KSM off → no change. If this fails,
  stop and rethink before Phase 2.

### Phase 2 — Persistent, code-running sandboxes (largest engineering step)
- **Zeroboot patch `0004-persistent-sandboxes`:**
  - `POST /v1/sandboxes` forks and keeps the VM; `POST /v1/sandboxes/{id}/exec`;
    `DELETE /v1/sandboxes/{id}`.
  - The vCPU runs only during an exec. Between execs (LLM think time) the VM is
    paused but its memory stays resident, which is the regime AgentZip measures.
- **Guest agent v2** (C, PID 1): framed serial protocol for
  `exec(cmd, cwd, timeout) → stdout/stderr/exit`, `write_file`, `read_file`
  (base64), output truncation, and a userspace PRNG/entropy reseed per fork.
- **Template builder:**
  - Convert an R2E-Gym task's docker image → rootfs.
  - The guest copies it into tmpfs (finding 3) and warms it (import the package,
    run test collection once), then snapshot.
  - Template RAM sized by measurement (likely 2–4 GB).
  - Adds docker to provisioning.
- Lives in `phase2_sandbox/`.
- **Gate:** 16 concurrent sandboxes held 10+ min each. Each runs the task's tests,
  edits a file, re-runs them, with results matching the same commands in the docker
  image.

### Phase 3 — Record traces once with DeepSeek
- Use R2E-Gym's agent scaffold and tools (bash, file editor, search, finish), with
  the runtime backend pointed at our sandbox API. Record and replay then run in the
  same environment.
- **Trace JSONL** per trajectory:
  - Metadata: task, repo, model, temperature, role prompt, cohort id.
  - Per step: tool, args, `think_time_ms_before` (measured LLM latency), exit code,
    output length/hash.
- Rollout: 16 temps; GAF: 4 roles × temps. Step cap + cost cap per run.
- API key from env, never committed. Traces committed to `ksm/traces/` (small).
- Pilot first: 1 repo × 1 task (16 + 4 trajectories). Then 1 task × 10 repos.
- Lives in `phase3_traces/`.
- **Gate:** the pilot traces replay end to end (Phase 4 replayer) with the same exit
  codes as recorded.

### Phase 4 — Replay + KSM measurement (the headline result)
- **Replayer:** for one cohort, create N sandboxes concurrently from the task
  template. Per step, sleep `think_time_ms_before`, then exec. Record wall time.
- **Sampler (1 Hz):**
  - Serve-process `smaps_rollup` (Anonymous = CoW'd private pages).
  - `/proc/<pid>/ksm_stat` plus `/sys/kernel/mm/ksm/{pages_shared,pages_sharing}`.
  - `M_KSM(t) = private − pages saved by merging`. The exact formula gets written
    down and validated against the 1b gate.
- **Configs:** NoComp (`ksm_ctl.sh off` + unmerge) vs KSM (fixed knobs), 3 reps
  each, NUMA-pinned.
- **Output:** per-repo and geomean `S` and slowdown for Rollout and GAF, with
  plots in the paper's Fig 5/6 style.
- **"Other stuff"**, all without implementing AgentZip:
  - KSM sensitivity: scan rate, cohort size 1/2/4/8/16, `merge_across_nodes`.
  - KSM CPU cost (ksmd time).
  - **§6.5-style offline similarity analysis** from periodic private-page dumps:
    exact-duplicate fraction (KSM's ceiling), same-VA agreement across siblings,
    template-delta compressibility, zero/RLE pages. This quantifies what KSM leaves
    on the table.
- Lives in `phase4_replay_ksm/`.

### Later / optional
zswap configs, AgentZip codecs, and prefetching (paper §6.2–6.4). Out of scope for now.

## Main risks
- KSM not merging pages in the MAP_PRIVATE memfd mapping (checked at the 1b gate).
- R2E environments too big for an in-RAM template. Mitigation: trim the image, or
  size template RAM up (node has 251 GB).
- Serial bandwidth for big tool outputs. Mitigation: truncate like R2E does;
  measure in Phase 2.
- ksmd too slow to matter within short sandbox lifetimes. That would itself be a
  result; we report it alongside the knob settings.
- DeepSeek model availability/cost; the exact model id gets recorded in the traces.
- Differences from the paper: host kernel 6.18 vs 5.15, Xeon E5-2683 v3 vs
  Platinum 8480C. Documented in results.

## Verification
Each phase ends at its gate above. The gate is run by a script in that phase's
directory on a freshly provisioned node, and its output is saved under that phase's
`results/`. Final check: re-provision a fresh node from `ksm` only and reproduce
the pilot's Phase 4 numbers within run-to-run noise.

## Immediate next step
Phase 2: persistent, code-running sandboxes.
