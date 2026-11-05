# Phase 1 — Reproduce the AgentZip sandbox environment (Zeroboot + CoW)

**Goal (from the plan):** get Zeroboot running on CloudLab and *prove that
multiple VMs actually share physical memory through copy-on-write* — launch 16
sandboxes from one snapshot. **No LLM yet.**

This reproduces the sandbox substrate of the AgentZip paper (`../../agentzip.pdf`,
§3.1 / §5.1): "We implement AgentZip on top of Zeroboot, a KVM-based sandbox
runtime that creates sandboxes from Firecracker memory snapshots using CoW
cloning. All sandboxes belonging to the same workload use the same snapshot as
their template and initially share its memory through copy-on-write mappings.
Pages modified during execution become sandbox-owned private pages."

## Run order (on the CloudLab node, as root)

```bash
sudo bash 00_provision.sh          # KVM, Firecracker >=1.12, Rust, build Zeroboot, guest kernel
python3 ../../numa_ksm_bench/agentzip_ksm_bench/fetch_tasks.py \
        --repo scrapy --num 1 --out task.json      # get a repo's docker_image + pkg
sudo bash 10_build_r2e_rootfs.sh \
        --docker-image "$(jq -r .[0].docker_image task.json)" \
        --pkg "$(jq -r .[0].pkg task.json)" --repo scrapy
sudo WORKDIR=$HOME/zeroboot/workdir-r2e bash 20_make_template.sh   # the snapshot
sudo BACKGROUND=1 WORKDIR=$HOME/zeroboot/workdir-r2e bash 30_serve.sh
sudo python3 40_cow_probe.py --sweep 1,2,4,8,16 --hold 20 --touch-mb 0   # SHARING
sudo python3 40_cow_probe.py --sweep 1,2,4,8,16 --hold 20 --touch-mb 50  # CoW isolation
```

## Why each step

| Step | What it does | Paper mapping |
|---|---|---|
| `00_provision.sh` | installs KVM/Firecracker/Rust, builds Zeroboot, fetches a guest `vmlinux` | §5.1 host setup |
| `10_build_r2e_rootfs.sh` | bakes one R2E repo (`/testbed` + deps) into a bootable Zeroboot rootfs; `init.py` **warms** the package so its pages are in the snapshot | §5.1 template = base OS + libs + repo |
| `20_make_template.sh` | `zeroboot template …` → the immutable snapshot every fork CoW-shares | the "template" / `template_id` |
| `30_serve.sh` | `zeroboot serve …` → one process that forks VMs in-process | the sandbox runtime |
| `40_cow_probe.py` | holds N forks resident, measures physical memory vs N×snapshot | the CoW proof |

## What success looks like

`40_cow_probe.py` prints a sweep. The headline is the last row:

```
== Phase 1 result ==
16 forks of a 512 MB snapshot cost ~X MB of private memory
(naive no-CoW would be 8192 MB) -> CoW eliminated ~9X%.
```

- **`--touch-mb 0`** (every fork only reads): serve-process growth should be
  near zero and `cow_sharing_pct` ≈ 99%+. That is the proof that forks **share**
  the snapshot.
- **`--touch-mb 50`** (every fork dirties 50 MB): `per_fork_private_MB` ≈ 50,
  growth ≈ 50×N. Private memory tracks **touched bytes, not snapshot size** —
  that is CoW isolating writes. Fork creation time (`fork_time_ms`) should be
  sub-millisecond-ish, matching Zeroboot's "1000 forks in 815 ms".

Results land in `results/cow-<ts>-touch{0,50}.json` and `.csv`.

## How the measurement works (and why it's valid)

Zeroboot runs every fork as an **in-process KVM VM** inside the single
`zeroboot serve` process, each `mmap(MAP_PRIVATE)`-ing the one snapshot file
(Zeroboot `ARCHITECTURE.md`; `src/vmm/kvm.rs`). So the whole CoW story shows up
in that process's `/proc/<pid>/smaps_rollup`:

- the snapshot is mapped **once** at serve start → already in the baseline `Pss`;
- each live fork adds only its **`Private_Dirty`** CoW pages.

The probe samples `smaps_rollup` (serve-process view) **and** `/proc/meminfo`
(system view) while N forks are held, subtracts the no-fork baseline, and
compares to `N × snapshot_MB`. Both views should agree that cost ≪ naive.

## Faithfulness to the paper — and the gaps (read before trusting results)

1. **Stock Zeroboot is ephemeral exec, not persistent sandboxes.** The public
   repo exposes only `POST /v1/exec` (fork → run → destroy) and a fixed `bench`.
   The paper's *persistent, E2B-compatible, lifecycle-managed* sandboxes are
   **AgentZip's own layer on top of Zeroboot and are not in the open repo.** For
   Phase 1 (a CoW proof) ephemeral exec is exactly right — we just hold forks
   resident with a sleeping payload. For later trajectory replay, run each
   trajectory as **one long-lived exec** that sleeps `think_time_ms_before`
   between tool actions.
2. **No networking inside forks (serial I/O only).** Can't `pip`/`git` inside a
   fork → every repo must be pre-baked into the rootfs (step 10). The real
   study needs **one template per repo** (the paper's 10 repos).
3. **In-process forks ⇒ host-level memory accounting.** You measure aggregate
   private memory at the serve process, not per-container cgroups (unlike the
   earlier docker+KSM harness). Great for a CoW proof; per-sandbox attribution
   would need instrumenting Zeroboot.
4. **The KSM endgame measures the baseline the paper calls weak.** Because CoW
   already captures exact-page sharing at fork, KSM finds little *additional*
   exact dedup on resident forks — which is precisely §6.1. The interesting KSM
   number is residual exact dedup across *diverged* private pages; the large
   residual *similarity* (76–96%, §6.5) is what KSM can't touch and AgentZip's
   delta/dictionary codecs target.
5. **Kernel / model.** CloudLab node is `6.8.0-138` (the "6.18" in the brief is
   almost certainly a typo for 6.8) — fine: KVM, userfaultfd, KSM all present
   (paper used 5.15; newer is OK). **DeepSeek-V4** in the paper is future-dated;
   use whatever DeepSeek you have API access to for later trace generation — the
   model only decides trajectory *content*, not CoW/KSM mechanics.

## On-node things to verify (I could not test these from the WSL dev box)

- `00`: the exact `vmlinux` path Zeroboot's `template` expects, and that the CI
  kernel URL is still live / Zeroboot-compatible. Prefer a `guest/` kernel if
  the repo ships one.
- `10`: that the overlaid rootfs still **boots and execs** under Zeroboot
  (`zeroboot test-exec <workdir> 'python3 -c "import <pkg>"'`). Guest python ABI
  must match the exported site-packages; if imports fail, rebuild deps against
  the guest python instead of copying site-packages.
- `40`: `smaps_rollup` of the root-owned serve process needs root (run with
  `sudo`); confirm a fork actually stays resident for the whole `--hold` window
  (it should: the exec call blocks until the guest code returns).

## Next phases (not built yet)

- **Phase 2 — Trace record:** drive each sandbox with DeepSeek once; record the
  tool-action sequence + `think_time_ms_before` per call (paper §5.3).
- **Phase 3 — Deterministic replay + KSM baseline:** replay traces with no LLM,
  sample `M(t)` once/sec, and measure KSM's realized dedup across the cohort
  (builds on `../../numa_ksm_bench/agentzip_ksm_bench/`).
- **Phase 4 — AgentZip codecs:** template-delta + cohort-dictionary + RLE over
  the same resident cohort to quantify the residual the paper targets.
