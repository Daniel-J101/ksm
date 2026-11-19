# ksm — AgentZip reproduction on CloudLab

The shared development directory for every CloudLab node in this project. Anything
a fresh node needs is either a script in here or written down in here. If you
change a node by hand (a package, a sysctl, an edit to `~/zeroboot`), turn it into
a script or patch here before you commit.

## Fresh node → Phase 1 result

Ubuntu 24.04 bare-metal node with `/dev/kvm`:

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
| `phase1_zeroboot_cow/` | Phase 1: Zeroboot running, CoW sharing measured. See its [README](phase1_zeroboot_cow/README.md). |

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

Per-run state (not persistent): snapshots and rootfs in `$WORKDIR` (`/tmp`, root-owned
after `20_make_template.sh`), and `ulimit -n 65536` raised inside `25_bench.sh` /
`30_serve.sh` for that process only. No sysctls, kernel modules or services are
changed.

Tested on: Ubuntu 24.04.4, host kernel `6.18.0-061800-generic`, 2026-11-19.
