# Phase 2 — persistent, code-running Zeroboot sandboxes

One `zeroboot serve` process holds an R2E-Gym task template. Clients create
long-lived sandboxes forked from it. Each sandbox runs shell commands (R2E's
tools, `pytest`, file edits) across many calls, with idle "LLM think" gaps in
between. Memory stays resident between calls. This is what Phase 4 replays
DeepSeek traces on. Design, findings and progress are in [`PLAN.md`](PLAN.md).

## Fresh node → passing gate

```bash
# after phase1_zeroboot_cow/00_provision.sh (pinned Zeroboot incl. patches 0005-0007)
cd ksm/phase2_sandbox
sudo bash 00_provision.sh                         # data disk /mnt/data, docker, R2E-Gym venv
source ../env.sh
sudo -E $R2E_DIR/.venv/bin/python 20_build_r2e_template.py --repo aiohttp      # ~3 min
sudo -E $R2E_DIR/.venv/bin/python 50_sandbox_gate.py $TEMPLATES_DIR/aiohttp/f0d74880deec   # ~13 min
```

Quick gate while iterating: `... 50_sandbox_gate.py <template> --n 2 --hold-min 1 --gap 1,3`.

## Pieces

| File | What |
|---|---|
| `agent/zbagent.c` | Guest PID 1. Template boot: copy the root fs into tmpfs, `pivot_root`, detach the disk (forks have no disk), mount proc/sys/dev/shm, quiet the kernel console, print `ZEROBOOT_TEMPLATE_READY`. Then serves the framed serial protocol documented at the top of the file. |
| `10_image_to_rootfs.sh` | docker image (or `container:<id>`) → `rootfs.ext4` with `/zb-init`, `/zb-env` (image env; `ENV_PATH` overrides PATH), `/zb-config`; `manifest.json`. |
| `20_build_r2e_template.py` | One R2E-Gym task → template under `$TEMPLATES_DIR/<repo>/<commit12>/`. Runs **R2E-Gym's own** `RepoEnv` (setup_env) + `add_commands` (file_editor, search, execute_bash, finish) in docker, exports that container, snapshots with guest RAM = content/0.9 + 1.5 GB. |
| `zbsandbox.py` | Client: `Sandbox` (create/exec/put/close) and `ZerobootRuntime`, which has the same `run()` / `run_tests()` / `copy_to_container()` behaviour as R2E's `DockerRuntime`. |
| `50_sandbox_gate.py` | The Phase 2 gate: docker reference vs. one sandbox vs. N concurrent sandboxes held ≥ 10 min. |
| `00_provision.sh` | Node prep for this phase (see root README table). |

## API (served by `zeroboot serve <template_dir>`, patch 0007)

| Call | Body → response |
|---|---|
| `POST /v1/sandboxes` | `{template?, seed?, epoch_ns?}` → `{id, fork_ms, init_ms}` |
| `POST /v1/sandboxes/{id}/exec` | `{cmd, cwd?, timeout_ms?=300000, max_output?=1 MiB}` → `{output, exit_code, truncated, timed_out, exec_ms}` |
| `POST /v1/sandboxes/{id}/files` | `{path, content_b64, mode?="644"}` → `{ok}` (creates parent dirs) |
| `DELETE /v1/sandboxes/{id}` | frees the fork's memory |
| `GET /v1/sandboxes` | `[{id, template, age_s, requests}]` |

`exec` runs `/bin/sh -c cmd` in `cwd` (default: the image's working dir, `/testbed`
for R2E) with the image's environment. stdout and stderr are merged, as with docker
exec. Exit code is 124 on timeout and 128+N if killed by a signal. `seed` is mixed
into each sandbox's kernel RNG, so forks don't share random streams. `epoch_ns`
sets the guest wall clock (default: now).

## Results

**Gate PASS (2026-11-19, `results/gate-aiohttp-20261119-150345.json`).** Task: aiohttp#0
(`f0d74880deec`), 3 GiB guest RAM, 1285 MB in-RAM root.

- Docker reference: tests fail before the ground-truth patch (reward 0) and pass after it (reward 1).
- One sandbox matches docker on **every step**: exit codes, parsed per-test results, rewards, outputs.
- **16 sandboxes held 13.5 min** with 5–40 s idle gaps, 3–4 full rounds each (≈ 560
  tool calls). **0 differences** from docker.
- Serve process peak: Anonymous 845 MiB for 16 sandboxes (≈ 53 MiB private per
  sandbox, vs. 3 GiB guest RAM each); serve loads the template in 5 s.

Per-step time, docker vs. sandbox (single run): tests 2.71 s vs 1.73 s (cold) and
0.67 vs 0.71 s; view 0.30 vs 0.24; search 0.11 vs 0.06; edits/patch ≤ 0.1 s both.
The sandbox is as fast as docker or faster: no docker exec overhead, files
already in RAM.

## Known differences from docker (accepted)

- **Directory listing order.** tmpfs and docker's overlayfs-on-ext4 return
  `readdir` entries in different orders. Tools that walk directories (R2E's
  `search`) report the same matches in a different order. Test results, file
  contents and exit codes are identical.
- **Single vCPU** per sandbox (Zeroboot limit; the paper has the same).
- **Serial transport** ≈ 91 KB/s out, 40 KB/s in. Big outputs cost time
  (1 MiB ≈ 11.5 s). R2E observations are small; revisit if Phase 3 traces
  disagree.
