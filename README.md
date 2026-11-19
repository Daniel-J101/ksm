# ksm — AgentZip reproduction on CloudLab

The shared development directory for every CloudLab node in this project. Anything
a fresh node needs is either a script in here or written down in here. If you
change a node by hand (a package, a sysctl, an edit to `~/zeroboot`), turn it into
a script or patch here before you commit.

## Fresh node → Phase 1 result

Ubuntu 24.04 bare-metal node with `/dev/kvm` (Phase 2+ also needs an empty spare disk, `sdb` by default):

```bash
cd ksm/phase1_zeroboot_cow
sudo bash 00_provision.sh                 # everything below in "What provisioning changes"
bash      10_build_agent_rootfs.sh
sudo bash 20_make_template.sh
python3   tools/inspect_vmstate.py /tmp/zb-workdir-agent/snapshot/vmstate   # must say ALL CHECKS PASS
sudo bash 25_bench.sh                     # -> results/bench-<utc>.txt
```

The Rust build takes ~20 s and the template ~10 s; most of the time is apt and the first rustup download.

## Layout

| Path | What |
|---|---|
| `env.sh` | Shared defaults sourced by every script: `ZB_DIR` (`~/zeroboot`), `WORKDIR` (`/tmp/zb-workdir-agent`), `ZB`. Resolves the real user's home even under sudo. |
| `zeroboot-pin/` | Pinned upstream Zeroboot commit + our patches + setup script. See its [README](zeroboot-pin/README.md). |
| `phase1_zeroboot_cow/` | Phase 1 + 1b: Zeroboot running, CoW sharing measured, KSM feasibility gate. See its [README](phase1_zeroboot_cow/README.md). |
| `phase3_traces/` | Phase 3: record DeepSeek agent trajectories once (R2E-Gym in docker) for replay. [`README.md`](phase3_traces/README.md). |
| `traces/` | Compact recorded traces (Phase 4 replay input), `<repo>/<commit12>/<rollout|gaf>/<k>.json`. |
| `tools/ksm_ctl.sh` | Put KSM in a known, recorded state (`status` / `off` / `on`, knobs pinned via env). Every experiment uses it. |
| `phase2_sandbox/` | Phase 2: persistent, code-running sandboxes from R2E-Gym task templates. How to run: [`README.md`](phase2_sandbox/README.md); design and progress: [`PLAN.md`](phase2_sandbox/PLAN.md). |
| `tools/setup_data_disk.sh` | Format/mount an empty data disk at `/mnt/data` (refuses non-empty disks). |
| `ROADMAP.md` | Phases from here to the headline result (KSM saving for N concurrent agents). |

## What provisioning changes on a node

`phase1_zeroboot_cow/00_provision.sh` (run with sudo) is the only thing that
changes the system. It is safe to re-run.

| Change | Where | Why |
|---|---|---|
| apt: `build-essential pkg-config libssl-dev e2fsprogs musl-tools curl jq git ca-certificates cpu-checker python3` | system | build zeroboot, static guest agent, ext4 rootfs |
| Firecracker **v1.12.0** | `/usr/local/bin/firecracker` | `zeroboot template` boots a VM to snapshot it; the parser targets 1.12.0's snapshot layout |
| `chown -R $SUDO_USER` of root-owned `~/.cargo`, `~/.rustup`, `$ZB_DIR` | your home | leftovers from building under sudo |
| rustup + Rust **1.100.0** (as your user) | `~/.cargo`, `~/.rustup` | pinned toolchain from `zeroboot-pin/UPSTREAM` |
| zeroboot clone @ pinned commit + `zeroboot-pin/patches/*` | `$ZB_DIR` (`~/zeroboot`) | the binary every phase uses |
| guest kernel `vmlinux-fc` (Firecracker CI 6.1.128, sha256-checked) | `$ZB_DIR/guest/` | gitignored upstream, so it has to be downloaded |
| **Phase 2** (`phase2_sandbox/00_provision.sh`): format + mount an **empty** disk (default `/dev/sdb`, refuses non-empty) as ext4 `LABEL=ksmdata` at `/mnt/data` (`docker/ templates/ traces/ work/ hf/`), fstab `nofail` | `/mnt/data`, `/etc/fstab` | docker images + in-RAM templates need far more than `/`'s 64 GB |
| docker.io + `python3-venv`; `/etc/docker/daemon.json` `data-root=/mnt/data/docker`; `$SUDO_USER` added to group `docker` | system | build templates from R2E-Gym images; record traces (Phase 3) |
| R2E-Gym @ `0d94c4e` + venv (`uv 0.8.4`, `uv sync --frozen`) | `/mnt/data/r2e/R2E-Gym` | template setup steps + trace recording |

**KSM state is a node setting experiments change.** CloudLab's Ubuntu image
starts KSM at boot (`qemu-kvm.service`: `run=1`, `pages_to_scan=100`,
`sleep_millisecs=200`). `tools/ksm_ctl.sh off|on` overrides that until reboot, and
every result file records the knobs that were in effect. Nothing persists across
reboots, so after one, re-run `ksm_ctl.sh` before measuring.

**Secrets:** the DeepSeek key lives in `~/.config/ksm/deepseek.env` (mode 600, outside
`ksm`, one per node; see `phase3_traces/README.md`). `*.env` is gitignored.

Per-run state (not persistent): snapshots and rootfs in `$WORKDIR` (`/tmp`, root-owned
after `20_make_template.sh`), and `ulimit -n 65536` raised inside `25_bench.sh` /
`30_serve.sh` for that process only. No other sysctls, kernel modules or services are
changed.

Tested on: Ubuntu 24.04.4, host kernel `6.18.0-061800-generic`, 2026-11-19.
