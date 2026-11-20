# ksm — how much does KSM save when many AI coding agents share a sandbox template?

Reproduces the sandbox substrate of the AgentZip paper ([`agentzip.pdf`](agentzip.pdf)):
Zeroboot copy-on-write VM sandboxes, R2E-Gym coding tasks, DeepSeek agent traces
recorded once and replayed. It measures the memory KSM saves on top of
copy-on-write (NoComp vs KSM).

This directory is shared by every CloudLab node in the project. **Everything a
fresh node needs is a script or patch in here, or written down in here.** If you
change a node by hand (a package, a sysctl, an edit to `~/zeroboot`), turn it
into a script or patch here.

## Status (2026-11-19)

The pilot is done end to end on one task (R2E-Gym aiohttp task, commit `f0d74880deec`):

| Phase | Result | Docs |
|---|---|---|
| 1 / 1b | Zeroboot pinned + patched; CoW sharing and isolation proven; **KSM can merge fork pages only with patch 0003** (gate: 458 of 448 MiB ideal) | [phase1_zeroboot_cow](phase1_zeroboot_cow/README.md) |
| 2 | Persistent, code-running sandboxes; **16 sandboxes × 13.5 min match docker on every step** | [phase2_sandbox](phase2_sandbox/README.md) |
| 3 | 20 DeepSeek-flash trajectories (16 Rollout + 4 GAF), **all solved**, ≈ $0.11–0.28 | [phase3_traces](phase3_traces/README.md) |
| 4 | **KSM saves 10.4 ± 0.6 % (Rollout, 16 sandboxes) / 3.5 ± 0.5 % (GAF, 4)** of sandbox-owned memory | [phase4_replay_ksm](phase4_replay_ksm/README.md) |

Next steps are in [`ROADMAP.md`](ROADMAP.md) under "Immediate next step".

## Fresh node → everything above (runbook)

Needs: Ubuntu 24.04 bare-metal CloudLab node with `/dev/kvm`, an **empty** spare
disk (`/dev/sdb` by default; set `DEV=`), and internet access. Times measured on
an E5-2683 v3 node.

```bash
cd ksm
# --- Phase 1: Zeroboot (~5 min) ---------------------------------------------
sudo bash phase1_zeroboot_cow/00_provision.sh        # apt, Firecracker v1.12.0, pinned+patched zeroboot, kernel
cd phase1_zeroboot_cow
bash 10_build_agent_rootfs.sh && sudo bash 20_make_template.sh
python3 tools/inspect_vmstate.py /tmp/zb-workdir-agent/snapshot/vmstate   # must say ALL CHECKS PASS
sudo bash 25_bench.sh                                # CoW proof
# Phase 1b (KSM gate, ~5 min): see phase1_zeroboot_cow/README.md "Phase 1b"
cd ..

# --- Phase 2: sandboxes (~25 min incl. gate) ----------------------------------
sudo bash phase2_sandbox/00_provision.sh             # data disk /mnt/data, docker, R2E-Gym venv, numactl
# log out/in once so your user is in group docker (or prefix with sudo, as below)
source env.sh
sudo -E $R2E_DIR/.venv/bin/python phase2_sandbox/20_build_r2e_template.py --repo aiohttp   # ~3 min
sudo -E $R2E_DIR/.venv/bin/python phase2_sandbox/50_sandbox_gate.py $TEMPLATES_DIR/aiohttp/f0d74880deec

# --- Phase 3: traces (~5 min, < $0.50) -- ONLY if traces/ doesn't have them yet ----
# DeepSeek key, once per node (prompted, never echoed; file is outside ksm):
mkdir -p ~/.config/ksm && ( umask 077; read -rsp 'DeepSeek API key: ' k && printf 'DEEPSEEK_API_KEY=%s\n' "$k" > ~/.config/ksm/deepseek.env; echo; unset k )
T=$TEMPLATES_DIR/aiohttp/f0d74880deec
sudo -E $R2E_DIR/.venv/bin/python phase3_traces/record.py --template-dir $T --mode rollout --n 16 --parallel 16 --budget-usd 3
sudo -E $R2E_DIR/.venv/bin/python phase3_traces/record.py --template-dir $T --mode gaf --parallel 4 --budget-usd 2

# --- Phase 4: replay + KSM (~30 min) -----------------------------------------
cd phase4_replay_ksm
sudo -E bash run_matrix.sh $T ../traces/aiohttp/f0d74880deec
$R2E_DIR/.venv/bin/python report.py                  # -> results/summary.md + plots
```

The traces in `traces/` are committed, so **another node can skip Phase 3** and
replay them. It needs the same template: same R2E task. Each trace records the
template's `manifest.json` hash for checking.

## Layout

| Path | What |
|---|---|
| `env.sh` | Shared defaults sourced by every script (`ZB_DIR`, `WORKDIR`, `DATA_DIR`, `R2E_DIR`, `TEMPLATES_DIR`, `HF_HOME`). Resolves the real user's home even under sudo. |
| `zeroboot-pin/` | Pinned upstream Zeroboot commit + **7 patches** + `setup_zeroboot.sh` / `new_patch.sh`. [README](zeroboot-pin/README.md) |
| `phase1_zeroboot_cow/` | Phase 1 + 1b: Zeroboot running, CoW measured, KSM feasibility gate; `30_serve.sh` (used by every later phase), `tools/inspect_vmstate.py`. [README](phase1_zeroboot_cow/README.md) |
| `phase2_sandbox/` | Guest agent (`agent/zbagent.c`), image → rootfs, R2E template builder, `zbsandbox.py` client, gate. [README](phase2_sandbox/README.md) · [PLAN](phase2_sandbox/PLAN.md) (design log) |
| `phase3_traces/` | `record.py`: R2E-Gym agent + DeepSeek, recorded once. [README](phase3_traces/README.md) |
| `traces/` | Compact traces = Phase 4 input: `<repo>/<commit12>/<rollout|gaf>/<k>.json`. |
| `phase4_replay_ksm/` | `replay.py`, `run_matrix.sh`, `report.py`, `results/`. [README](phase4_replay_ksm/README.md) |
| `tools/` | `ksm_ctl.sh` (KSM state, used by every experiment), `setup_data_disk.sh`. |
| `ROADMAP.md` | The plan from here to the full result; per-phase status. |
| `CLAUDE.md` | Rules and orientation for AI agents picking this up. |
| `agentzip.pdf` | The paper (§5 = methodology we follow). |

## What lives where (and what's lost if a node goes away)

| Thing | Where | In git? | If lost |
|---|---|---|---|
| scripts, patches, docs, compact traces, Phase 1/2/4 results | `ksm/` | yes | — |
| Zeroboot checkout + build | `~/zeroboot` | no | `zeroboot-pin/setup_zeroboot.sh` |
| R2E task templates (rootfs, 3 GiB snapshot) | `/mnt/data/templates/<repo>/<commit12>/` | no | `20_build_r2e_template.py` (~3 min each) |
| **full traces** (agent thoughts, all observations, LLM calls, logs) | `/mnt/data/traces/...*.full.json.gz` | **no** | **re-recording costs money and gives different trajectories**. Compact traces are enough to replay. Full ones are for analysis (6 MB for the pilot). |
| R2E-Gym + venv, HF dataset cache (11 GB), docker images | `/mnt/data/{r2e,hf,docker}` | no | `phase2_sandbox/00_provision.sh` + first use |
| DeepSeek API key | `~/.config/ksm/deepseek.env` (mode 600) | **never** | re-enter on each node (one-liner above) |

## What provisioning changes on a node

`phase1_zeroboot_cow/00_provision.sh` and `phase2_sandbox/00_provision.sh` (both
`sudo`, both safe to re-run) are the only things that change the system.

| Change | Where | Why |
|---|---|---|
| apt: `build-essential pkg-config libssl-dev e2fsprogs musl-tools curl jq git ca-certificates cpu-checker python3` | system | build zeroboot, static guest agents, ext4 rootfs |
| Firecracker **v1.12.0** | `/usr/local/bin/firecracker` | `zeroboot template` boots a VM to snapshot it; the vmstate parser targets 1.12.0's layout |
| `chown -R $SUDO_USER` of root-owned `~/.cargo`, `~/.rustup`, `$ZB_DIR` | your home | leftovers from building under sudo |
| rustup + Rust **1.100.0** (as your user) | `~/.cargo`, `~/.rustup` | pinned toolchain (`zeroboot-pin/UPSTREAM`) |
| zeroboot clone @ pinned commit + `zeroboot-pin/patches/*` | `~/zeroboot` | the binary every phase uses |
| guest kernel `vmlinux-fc` (Firecracker CI 6.1.128, sha256-checked) | `~/zeroboot/guest/` | gitignored upstream |
| **Phase 2:** format + mount an **empty** disk (default `/dev/sdb`, refuses non-empty) as ext4 `LABEL=ksmdata` at `/mnt/data` (`docker/ templates/ traces/ work/ hf/`), fstab `nofail` | `/mnt/data`, `/etc/fstab` | docker images + templates need far more than `/`'s 64 GB |
| docker.io, `python3-venv`, `numactl`; `/etc/docker/daemon.json` `data-root=/mnt/data/docker`; `$SUDO_USER` in group `docker` | system | R2E-Gym images (templates, recording); NUMA pinning in Phase 4 |
| R2E-Gym @ `0d94c4e` + venv (`uv 0.8.4`, `uv sync --frozen`, editable install) | `/mnt/data/r2e/R2E-Gym` | template setup + trace recording |

**KSM state is a node setting the experiments change.** CloudLab's Ubuntu image
starts KSM at boot (`qemu-kvm.service`: `run=1`, 100 pages / 200 ms).
`tools/ksm_ctl.sh off|on` overrides that until reboot; every result file records
the knobs in effect. The experiments leave KSM **off** when they finish.

Per-run only: `ulimit -n 65536` inside `25_bench.sh` / `30_serve.sh`; serve
pinned with `numactl` when `NUMA_NODE` is set. No other sysctls, modules or
services are changed.

Tested on: Ubuntu 24.04.4, host kernel `6.18.0-061800-generic`, 2026-11-19.

## Findings and decisions a newcomer must know

Details are in each phase's README; here's why things are the way they are.

**Zeroboot (all fixed by patches; see `zeroboot-pin/README.md`)**
- Snapshot CPU-state parser broke on this node. Firecracker's vmstate sections shift
  independently, and Linux guests put a PCID in CR3 → anchor-by-content parser (0001).
- 5 s Firecracker socket timeout is too short for snapshots ≥ 400 MB (0002).
- **KSM never touched forks**: fork memory wasn't `MADV_MERGEABLE` (0003).
- **Every fork's first sleep stalled for (boot → snapshot) seconds**: the guest TSC
  wasn't restored. The MSRs had to be read from Firecracker's MSR chunks by exact
  layout, because index `0x10` collides with length prefixes (0004).
- O(n²) output scanning; timeout helper threads piling up (0005). Template ready
  marker (0006). Persistent sandbox API (0007).
- Patches **stack**: always add or change them with `zeroboot-pin/new_patch.sh`,
  never with a plain `git diff` against the pin.

**Sandboxes**
- Forks have **no disk and no network**. The guest agent copies the whole root
  into tmpfs before the snapshot. Anything that downloads must happen in docker
  at template-build time.
- Templates are built by running **R2E-Gym's own** env setup + tool install in
  docker, then exporting that container. Don't re-implement R2E setup.
- Accepted difference from docker: directory listing order (tmpfs vs overlayfs).
- Serial transport ≈ 91 KB/s out / 40 KB/s in. That's fine for observed outputs
  (≤ 30 KB).

**Traces**
- Model `deepseek-flash` (DeepSeek's API id; the user chose "Flash"). R2E-Gym
  agent unmodified, r2egym scaffold, R2E runner defaults (40 / 50 steps).
- **Function calling is required.** In R2E's text mode deepseek-flash emits its own
  DSML tool markup and R2E wastes most steps. R2E only enables fn calling for
  gpt/sonnet/o3/o4 names, so the recorder passes the label
  `gpt-compat/deepseek-flash` and maps it back in a litellm wrapper.
- Recorded in R2E's docker runtime (agreed decision), replayed into Zeroboot with
  `ZerobootRuntime`, which wraps commands byte-for-byte like `DockerRuntime.run`.
- Rollout temperatures 0.2→1.2 (16, evenly spaced). GAF roles and temperatures are
  **our** wording of the paper's four roles (`record.py: GAF_ROLES`). The paper
  doesn't publish its prompts.
- Pilot task = first aiohttp task of R2E-Gym-Lite (`--index 0`). The paper doesn't
  name its tasks.

**Measurement**
- `M(t) = serve Anonymous − baseline − pages_sharing × 4 KiB` (validated in the
  1b gate). NoComp and KSM both run with mergeable mappings; only ksmd differs.
- KSM pilot knobs are **aggressive** (10 000 pages / 20 ms = 100× the kernel
  default; 1000× this image's boot setting) → an upper bound. ksmd ran ~460 full
  scans per 80 s Rollout run. It slows tool execution (median command +17%
  Rollout / +9% GAF) but barely the wall time (+1%), because agents are idle in
  LLM think time most of the time. The scan-rate sweep is next.

## Conventions

- Run experiment scripts as `sudo -E …` (they need root for docker, KSM and
  `/proc/<pid>/smaps`), with the R2E venv's python where R2E is imported.
- Results go to `<phase>/results/` (committed); big artifacts go to `/mnt/data`.
- Secrets never enter `ksm/` (`*.env` is gitignored); never print the API key.
- The user reviews changes before committing.
