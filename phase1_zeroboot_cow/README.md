# Phase 1 — Reproduce the AgentZip sandbox environment (Zeroboot + CoW)

**Goal:** get Zeroboot running on CloudLab and *prove multiple VMs share physical
memory through copy-on-write* — launch many sandboxes from one snapshot. **No LLM.**

This reproduces the substrate of the AgentZip paper (`../../agentzip.pdf`, §3.1/§5.1):
sandboxes are forked from one Firecracker memory snapshot and initially share it
through CoW; written pages become sandbox-private.

> These scripts were written against the **actual Zeroboot source** (now checked
> out at `../../zeroboot/`), not the deployment doc — which is wrong in two
> places. See "What the source actually says" below.

## The fast path (primary deliverable)

```bash
# on the CloudLab node, with the zeroboot repo at ~/zeroboot
sudo bash 00_provision.sh          # KVM, Firecracker, Rust; make build + make guest
bash      10_build_agent_rootfs.sh # 32 MB ext4 whose PID1 is the compiled guest/init
sudo      bash 20_make_template.sh # zeroboot template ... /init  -> workdir-agent/snapshot
sudo      bash 25_bench.sh         # zeroboot bench -> the CoW proof
```

`25_bench.sh` runs Zeroboot's own `bench`, which **is** the Phase 1 experiment:

- **Concurrent fork test** forks **10, 100, 1000** VMs from the one snapshot and
  prints total process RSS. RSS staying ~flat (≈ one snapshot) while the count
  grows 100× is the sharing proof — the naive no-CoW cost would be `count × mem_mib`.
- **Isolation test** shows fork B can't read fork A's secret, and A is unchanged
  after B writes — CoW hands out private copies on write.

Bigger snapshot = more dramatic: `sudo MEM_MIB=1024 bash 20_make_template.sh`.

## Optional Phase 1b — CoW *under execution* (forks that dirty memory)

`bench` forks never run a vCPU, so they never dirty pages (pure sharing, upper
bound). To match the paper's regime — forks resident *and* accumulating private
state — use the patched agent + probe:

```bash
musl-gcc -static -Os -march=x86-64 -mno-avx -mno-avx2 \
    -o ~/zeroboot/guest/init_agentzip init_agentzip.c          # adds `work <mb> <spins>`
AGENT=~/zeroboot/guest/init_agentzip bash 10_build_agent_rootfs.sh
sudo bash 20_make_template.sh
sudo BACKGROUND=1 bash 30_serve.sh
sudo python3 40_cow_probe.py --sweep 1,2,4,8,16 --hold 15 --touch-mb 0    # sharing
sudo python3 40_cow_probe.py --sweep 1,2,4,8,16 --hold 15 --touch-mb 50   # private growth
```

`--touch-mb 0` → serve RSS barely moves (forks share the snapshot). `--touch-mb 50`
→ per-fork private ≈ 50 MiB: private memory tracks **touched bytes, not snapshot
size**. Results in `results/cow-*.json|csv`.

## What the source actually says (and why the earlier plan changed)

Reading `../../zeroboot/src`:

1. **`init_path` is run as PID 1 directly** — `boot_args: ... init={init_path}`
   ([firecracker.rs:96](../../zeroboot/src/vmm/firecracker.rs#L96)). So the rootfs
   just needs an executable at that path. The deploy doc's `/init.py` is wrong;
   our PID 1 is the compiled C agent, so **`init_path=/init`**.
2. **The open-source guest agent can't run code.** [guest/init.c](../../zeroboot/guest/init.c)
   handles only `echo`/`cat` over serial. The Python-capable agent used by the
   paper / managed `api.zeroboot.dev` is **not in the public repo.** So:
   - Phase 1 (a *memory* proof) needs no Python → **minimal agent rootfs**, not a
     baked R2E repo. (R2E baking waits for a real workload agent in a later phase.)
   - `40_cow_probe.py` needs the Phase 1b agent (`init_agentzip.c`); against the
     stock agent it can't hold a fork or dirty memory — use `25_bench.sh` there.
3. **A fork lives for exactly one `/v1/exec`** — `execute_code` forks, sends the
   command, waits for `ZEROBOOT_DONE`, then drops (munmaps) the VM
   ([handlers.rs:328-416](../../zeroboot/src/api/handlers.rs#L328)). To hold N
   forks we fire N concurrent requests whose guest command busy-spins. It must
   **busy-spin, not sleep**: a sleeping guest HLTs and `run_until_marker` returns
   on HLT-with-no-input ([kvm.rs:262](../../zeroboot/src/vmm/kvm.rs#L262)), ending
   the request and the fork early.
4. **Forks are in-process KVM VMs** inside the one `serve`/`bench` process, each
   `mmap(MAP_PRIVATE)` of the snapshot memfd ([kvm.rs:66](../../zeroboot/src/vmm/kvm.rs#L66)).
   So memory is measured at that process (RSS / smaps), not per-container cgroups.

## Faithfulness & remaining gaps

- **Stock Zeroboot = ephemeral exec, no persistent sandboxes, no code execution.**
  The paper's persistent E2B-compatible, code-running sandboxes are AgentZip's own
  layer. Reproducing the *workloads* (Phases 2–4) will require us to build a
  Python-capable guest agent — essentially the piece the authors built on top of
  Zeroboot. `init_agentzip.c` is the first seed of that.
- **No networking in forks (serial only)** → repos/deps must be baked into the
  rootfs once we have a real agent; one template per repo (paper's 10 repos).
- **KSM endgame** measures residual exact dedup on top of CoW — the baseline the
  paper calls weak (§6.1); the large residual *similarity* (76–96%, §6.5) is what
  AgentZip's delta/dictionary codecs target and KSM can't.
- **Node kernel** `6.8.0-138` is fine (KVM/userfaultfd/KSM present; paper used 5.15).

## Steps I could not test from the WSL dev box (verify on-node)

- `00`: `make guest` needs `musl-tools`; `make build` needs the Rust toolchain on PATH.
- `10`: loop-mounting ext4 needs root; `debugfs` listing is best-effort.
- `20`: if `template` fails, re-run with `ZEROBOOT_DEBUG=1`; confirm `snapshot/{mem,vmstate}` appear.
- `25`: `bench`'s "KB/fork" is `total_RSS / count` — read the *trend* across
  10/100/1000, not the absolute, as the sharing signal.
- `40`: needs root to read the serve process's `smaps_rollup`; pick `--spins` high
  enough to outlast `--hold` on this CPU (default 2e10 ≈ several s on a Xeon).

## Next phases (not built)

- **Phase 2 — trace record:** a Python-capable agent + DeepSeek, recording tool
  actions + `think_time_ms_before` (§5.3).
- **Phase 3 — replay + KSM baseline:** deterministic replay, `M(t)` 1/s, KSM dedup
  (builds on `../../numa_ksm_bench/agentzip_ksm_bench/`).
- **Phase 4 — AgentZip codecs:** template-delta + cohort-dictionary + RLE.
