# Phase 2 plan — persistent, code-running Zeroboot sandboxes

**Goal.** One Zeroboot `serve` process holds N long-lived sandboxes forked from an
R2E-Gym task template. Each sandbox runs arbitrary shell commands (R2E's tools,
`pytest`, file edits) over many calls, separated by idle "LLM think" gaps, with
memory staying resident. This is the substrate Phase 4 replays traces on.

**Gate (done when):** 16 concurrent sandboxes from one task template, each held
≥ 10 min with idle gaps, each runs the task's `run_tests.sh`, edits a file with
R2E's `file_editor`, and re-runs tests. Exit codes and test results must match
the same commands run in the task's docker image.

## What the code and node look like today (findings this plan is built on)

| Finding | Where | Consequence |
|---|---|---|
| A fork lives for one `/v1/exec`, then is dropped | `api/handlers.rs:310-416` | need a sandbox registry + lifecycle API |
| HLT with no pending serial input **ends the run** | `vmm/kvm.rs:262-270` | a guest that sleeps or waits on a child (pytest, `sleep`, `wait`) would be cut off; the run loop must keep going until the end marker |
| End-marker check re-scans the **whole** output on every byte | `vmm/kvm.rs:251` | O(n²) in output size; 100 KB of pytest output would crawl. Check only the tail. |
| Every guest output byte is one VM exit (8250 UART, PIO) | `vmm/serial.rs` | ~100s of KB/s at best; fine for R2E (outputs are truncated), but measure |
| Forks have **no block device, no network**; MMIO reads return `0xff` | `vmm/kvm.rs` (fork has irqchip+PIT+serial only) | the whole task filesystem must live in **guest RAM** (tmpfs) at snapshot time; the guest must never touch `/dev/vda` after fork |
| Template = boot, sleep `wait_secs`, snapshot; no readiness signal; Firecracker stdout → `/dev/null` | `vmm/firecracker.rs`, `main.rs:82` | copying GBs into tmpfs needs a "ready" marker, not a guessed sleep; socket timeout (120 s, patch 0002) too short for multi-GB Full snapshots at ~80 MB/s |
| Guest kernel 6.1 CI config has TMPFS, OVERLAY_FS, SQUASHFS, BLK_DEV_INITRD, DEVTMPFS, guest THP=y | Firecracker `microvm-kernel-ci-x86_64-6.1.config` | tmpfs root works; record guest THP mode (affects dirtied-page counts) |
| R2E runtime = `run(cmd, timeout, workdir) → (output, exit)` + `copy_to_container`; tools are scripts copied in once (`env.add_commands`) | `r2egym/agenthub/runtime/docker.py:689`, `environment/env.py:79` | a small `ZerobootRuntime` adapter covers it |
| R2E's `setup_env` runs `uv pip install chardet` (needs network) and moves `/r2e_tests` | `runtime/docker.py:538` | run setup **in docker while building the template**, not in the sandbox |
| R2E already logs `llm_exec_time` per step, and each action is one bash command | `agent/agent.py:407`, `trajectory.py:35` | traces = (think time, command) pairs |
| Images `namanjain12/<repo>_final:<commit>`: 0.3–1.8 GB compressed (orange3 largest) | Docker Hub | in-RAM templates of ~2–8 GB; node has 251 GB RAM |
| Node: `/` has 46 GB free; `sdb` (931 GB) and ~860 GB of `sda` unused; no docker | `lsblk` | docker data-root + templates go on a data disk |

## Progress

- **W0 done (2026-11-19).** `/mnt/data` on `sdb`; docker 29.1.3 with data-root
  on it; R2E-Gym `0d94c4e` + venv at `/mnt/data/r2e/R2E-Gym` (`uv sync --frozen`).
- **W1 done (2026-11-19). The finding was a clock bug, not HLT.**
  - A guest sleeping mid-command does **not** end the exec: KVM handles HLT
    in-kernel (no `VcpuExit::Hlt` while the guest slept).
  - But every fork paid a **fixed stall equal to the template's `WAIT_SECS`**
    on its first sleep or timer (nap 1 ms took 5.9 s with `WAIT_SECS=5`, 2.1 s
    with `WAIT_SECS=2`).
  - Cause: Zeroboot never restored the guest TSC. Each fork's TSC restarted at
    0, while the guest's timers were armed against the snapshot-time TSC.
  - A first fix attempt restored the wrong value: index `0x10` also matches
    Firecracker's per-record length prefix all over the vmstate.
  - **Patch 0004** parses Firecracker's MSR chunks by their exact layout,
    restores `IA32_TSC` + `TSC_DEADLINE`, and sets `KVM_SET_CLOCK` from the
    guest's pvclock.
  - Now a 1 ms nap costs 4 ms over baseline, a 1 s nap 1.05 s; guest monotonic
    time continues from the snapshot.
  - Phase 1 bench and the KSM gate still pass. `inspect_vmstate.py` checks the
    TSC MSRs too.
  - Without this, every agent tool call that sleeps, polls or times out would
    have been inflated by seconds.

- **W2 done.** Patches 0005 (run loop: tail-only marker scan, `HltPolicy`,
  cancellable timer), 0006 (template ready marker, console log, configurable
  Firecracker timeout), 0007 (persistent sandbox API).
- **W3 done.** `agent/zbagent.c`: root copied into tmpfs (aiohttp's 1.3 GB in
  10.8 s), `pivot_root`, disk detached, framed protocol. Bug found in testing and
  fixed: the staged command was not NUL-terminated, so a shorter command
  inherited the tail of the previous one.
- **W4 done.** `20_build_r2e_template.py` runs **R2E-Gym's own** `RepoEnv` +
  `add_commands` in docker, then exports that container (`10_image_to_rootfs.sh
  container:<id>`). aiohttp#0 template: 1285 MB content, 3 GiB guest RAM, built
  in 163 s.
- **W5 done.** `zbsandbox.py`: `Sandbox` plus `ZerobootRuntime` (same command
  wrapping and post-processing as R2E's `DockerRuntime.run`).
- **W6 done — GATE PASS.** 16 sandboxes × 13.5 min, ≈ 560 tool calls, 0 diffs vs
  docker (exit codes, per-test results, rewards 0→1, outputs). ≈ 53 MiB private per
  sandbox at peak. Details in README → Results.
- **Phase 2 complete (2026-11-19).**
- **Measured costs** (debian test template, 16 sandboxes):
  - create 16 sandboxes: 0.14 s; fork 3.4 ms + init 6 ms each;
  - exec round trip: 5–25 ms;
  - serial output ≈ 91 KB/s, input ≈ 40 KB/s (one VM exit per byte). Fine for
    R2E-sized observations (aiohttp test log 8.5 KB).
  - If Phase 3 traces show large outputs, the fix is `rep outsb` from the agent
    (up to 4 KB per exit).
- **Environment differences found (documented, accepted):**
  - Directory listing order differs: tmpfs vs docker's overlayfs-on-ext4 give
    different `readdir` order. The `search` tool finds the same matches but
    lists files in a different order. The gate compares it order-insensitively.
    Replay doesn't depend on observations.

## Decisions (agreed 2026-11-19)

1. **Record traces in R2E-Gym's own docker runtime, replay in Zeroboot.** This
   changes ROADMAP Phase 3, which said "record against our sandbox API". Reasons:
   recording needs no Zeroboot work (Phase 3 can start in parallel with this
   phase), it uses R2E's tested runtime unmodified, and the replayed commands
   run against a template built from the same image. Replay fidelity is checked
   by comparing exit codes per step.
2. **In-RAM root on tmpfs.** The guest PID 1 mounts the ext4 rootfs, copies it
   into a tmpfs, unmounts the disk, `chroot`s, warms up, then signals ready.
   Fallback if templates get too big: compressed squashfs in RAM.
3. **One OS thread per sandbox** owns its `ForkedVm` (KVM vCPU ioctls stay on
   one thread; the per-thread SIGALRM timeout keeps working). The HTTP handler
   talks to it over a channel.
4. **The vCPU only runs during an exec.** Between execs the sandbox is
   suspended but its memory stays mapped, which matches AgentZip's "LLM-idle"
   sandboxes. On each exec the guest clock jumps forward; that's fine.
5. **Serial stays the transport**, with a framed, base64 protocol and the
   quadratic marker scan fixed. A shared-memory mailbox is an optimization
   only if measurements say serial is too slow.

## Work items (in order; each ends with something runnable)

**W0. Node prep (scripted).**
- `tools/setup_data_disk.sh`: format and mount an **empty** disk (default `sdb`)
  at `/mnt/data`. It refuses to run if the disk has a partition table or
  filesystem.
- `phase2_sandbox/00_provision.sh`: install docker with
  `data-root=/mnt/data/docker`, plus `python3-venv`. It creates an R2E-Gym venv
  pinned to commit `0d94c4e`.
- Both get rows in the root README table.

**W1. Spike: guest idle behavior (1–2 h, de-risks W2).** With a modified run
loop, check what KVM gives us when the guest blocks in a 1 s `sleep` or a
child `wait`: repeated `VcpuExit::Hlt`, or KVM blocking in-kernel? That decides
whether "HLT without input" means "continue" or "sleep until the next timer
tick".

**W2. Zeroboot patches** (each its own numbered patch; 0004 is the W1 clock fix):
- `0005-runloop-fixes`: tail-only marker scan; with no pending input, HLT keeps
  going until the end marker or timeout (persistent mode only, so `bench` is
  unchanged); output cap.
- `0006-template-ready-marker`: `template` reads Firecracker's console and
  snapshots when the guest prints `ZEROBOOT_TEMPLATE_READY` (with a timeout).
  Socket timeout is configurable (`ZEROBOOT_FC_TIMEOUT_SECS`, default 120)
  for multi-GB snapshots.
- `0007-persistent-sandboxes`:
  - `POST /v1/sandboxes {template, seed, epoch}` → `{id}`.
  - `POST /v1/sandboxes/{id}/exec {cmd, cwd, timeout}` → `{output, exit_code, exec_ms}`.
  - `POST /v1/sandboxes/{id}/files {path, b64, mode}`.
  - `DELETE /v1/sandboxes/{id}`; `GET /v1/sandboxes` (count, per-sandbox age).
  - Fork mappings keep honoring `ZEROBOOT_KSM_MERGEABLE` (patch 0003).

**W3. Guest agent v2** (`phase2_sandbox/agent/zbagent.c`, static musl):
- Template path: mount `/dev/vda`, create a tmpfs, `cp -a`, unmount, `chroot`.
  Mount proc/sys/dev/tmp, set guest THP to a recorded mode, run the warm-up
  hook, `drop_caches`, print the ready marker, block on serial.
- Protocol: raw tty (no echo or CRLF mangling). Requests are base64 lines:
  - `INIT <seed> <epoch>`: reseed kernel entropy (RNDADDENTROPY), set the clock.
  - `EXEC <id> <cwd> <timeout> <b64cmd>`: fork, `/bin/sh -c`, merged
    stdout+stderr, kill on timeout, exit code 124 like `timeout(1)`.
  - `PUT <path> <mode> <b64>`.
- Responses are framed `ZB <id> OUT <b64>` … `ZB <id> EXIT <code>`.

**W4. Template builder** (`phase2_sandbox/20_build_r2e_template.py`, as built: runs R2E's own setup, then exports the container):
- Pull the image and start a container (with network).
- Replay R2E's `setup_env` steps, and copy R2E's tool scripts into place the
  same way `env.add_commands` does.
- `docker export` → ext4 sized to the content, with the agent injected at `/zb-init`.
- `zeroboot template` with `mem_mib = content + headroom` (measured; start at
  content + 1.5 GB). Writes a manifest (image, commit, sizes, knobs) next to
  the snapshot.
- Templates live under `/mnt/data/templates/<repo>/<commit>/`.

**W5. Host client** (`phase2_sandbox/zbsandbox.py`, stdlib only):
- A `Sandbox` class: `create`, `exec`, `put`, `close`.
- `ZerobootRuntime`, which exposes R2E's `run()` / `copy_to_container()` /
  `run_tests()` signatures, so Phase 4's replayer and an R2E agent can both
  drive a sandbox.

**W6. Gate** (`phase2_sandbox/50_sandbox_gate.py`):
- Pilot task: one aiohttp task (smallest repo family).
- Checks:
  - 16 sandboxes with idle gaps;
  - `run_tests.sh` → edit → `run_tests.sh`, compared against docker;
  - per-exec latency;
  - serial throughput (1 MB of output);
  - memory per idle sandbox (Pss, via the Phase 1b probe helpers).

## Risks

- ~~W1 result~~ resolved: HLT is handled in-kernel. The clock bug it uncovered
  is fixed (0004).
- **tmpfs copy of large images:** orange3/pandas may need 6–8 GB templates and
  slow (~100 s) Full snapshots. Mitigations: raise the timeout (0005); trim
  docs/caches from the image; squashfs fallback.
- **Single vCPU:** test suites run slower than in docker. That's accepted; the
  paper's Zeroboot has the same limit. Record exec times.
- **Python in the guest needs things the minimal kernel lacks** (e.g. `/dev/shm`
  for multiprocessing): mount it in W3, find the rest via the gate's
  docker-vs-sandbox comparison.

## Needs input later

- (Phase 3) a DeepSeek API key, and which model id counts as
  "DeepSeek-V4".
