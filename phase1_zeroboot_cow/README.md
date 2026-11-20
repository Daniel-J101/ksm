# Phase 1 — Reproduce the AgentZip sandbox environment (Zeroboot + CoW)

**Goal:** get Zeroboot running on CloudLab and *prove multiple VMs share physical
memory through copy-on-write* — launch many sandboxes from one snapshot. **No LLM.**

This reproduces the substrate of the AgentZip paper (`../../agentzip.pdf`, §3.1/§5.1):
sandboxes are forked from one Firecracker memory snapshot and initially share it
through CoW; written pages become sandbox-private.

> These scripts were written against the **actual Zeroboot source**, not the
> deployment doc — which is wrong in two places. See "What the source actually
> says" below. Zeroboot is **not vendored**: it's a pinned upstream commit plus
> ksm patches, built at `~/zeroboot` by [`../zeroboot-pin/`](../zeroboot-pin/README.md).

## The fast path (primary deliverable)

```bash
# on a fresh CloudLab node, from this directory
sudo bash 00_provision.sh          # apt, Firecracker v1.12.0, pinned+patched zeroboot, kernel, build
bash      10_build_agent_rootfs.sh # 32 MB ext4 whose PID1 is the compiled guest/init
sudo      bash 20_make_template.sh # zeroboot template ... /init  -> $WORKDIR/snapshot
python3   tools/inspect_vmstate.py /tmp/zb-workdir-agent/snapshot/vmstate  # gate: anchors OK?
sudo      bash 25_bench.sh         # zeroboot bench -> the CoW proof
```

Paths (`ZB_DIR`, `WORKDIR`) come from [`../env.sh`](../env.sh) and can be
overridden per command. Verified end to end from a fresh clone on 2026-11-19.

`25_bench.sh` runs Zeroboot's own `bench`, which **is** the Phase 1 experiment:

- **Concurrent fork test** forks **10, 100, 1000** VMs from the one snapshot and
  prints total process RSS. RSS staying ~flat (≈ one snapshot) while the count
  grows 100× is the sharing proof — the naive no-CoW cost would be `count × mem_mib`.
- **Isolation test** shows fork B can't read fork A's secret, and A is unchanged
  after B writes — CoW hands out private copies on write.

Bigger snapshot = more dramatic: `sudo MEM_MIB=1024 bash 20_make_template.sh`.

## Phase 1b — CoW under execution + KSM feasibility gate ✅

`bench` forks never run a vCPU, so they never dirty pages (pure sharing, upper
bound). Phase 1b holds forks resident *while they dirty private pages* (the
paper's regime), then checks that **KSM can merge those pages at all**, which
everything in Phase 4 depends on.

```bash
export WORKDIR=/tmp/zb-workdir-1b     # keep the Phase 1 snapshot separate
musl-gcc -static -Os -march=x86-64 -mno-avx -mno-avx2 \
    -o ~/zeroboot/guest/init_agentzip init_agentzip.c          # adds `work <mb> <spins> [seed]`
AGENT=~/zeroboot/guest/init_agentzip bash 10_build_agent_rootfs.sh
sudo -E bash 20_make_template.sh
python3 tools/inspect_vmstate.py $WORKDIR/snapshot/vmstate

# CoW under execution, KSM off
sudo bash ../tools/ksm_ctl.sh off
sudo -E BACKGROUND=1 bash 30_serve.sh
sudo python3 40_cow_probe.py --sweep 1,2,4,8,16 --hold 15 --spins 7000000000 --touch-mb 0
sudo python3 40_cow_probe.py --sweep 1,2,4,8,16 --hold 15 --spins 7000000000 --touch-mb 50
sudo -E bash 30_serve.sh stop

# KSM gate (restarts serve itself for each condition; ~3 min)
sudo -E python3 45_ksm_gate.py          # --n 8 --touch-mb 64 --hold 30 by default
```

**Why the KSM gate matters.** Fork memory is `mmap(MAP_PRIVATE)` of the snapshot
memfd, and ksmd only scans VMAs marked `MADV_MERGEABLE`, which upstream Zeroboot
never sets. Before patch `0003`, turning KSM on did nothing to sandboxes.
`ZEROBOOT_KSM_MERGEABLE=1` (passed through by `30_serve.sh`) enables it.

`work <mb> <spins> [seed]`: seed `0` writes pages that are distinct within a
fork but **identical across forks**, so only cross-fork merging can happen. A
nonzero seed (the probe's `--content unique` passes fork index + 1) makes every
fork's pages different.

### Results (2026-11-19, 512 MiB snapshot, E5-2683 v3, host 6.18)

CoW, KSM off (`results/cow-20261119-1332*`, `-1334*`): each fork owns ≈ 9 MiB
(guest-kernel dirtying during an exec) **plus exactly what it touches**
(touch 50 → 60.6 MiB/fork), flat from N=1 to 16. CoW avoids 88–98% of the
naive `N × 512 MiB`.

KSM gate (`results/ksm-gate-20261119-133751.json`), N=8, 64 MiB/fork, ideal
saving (N−1)×64 = 448 MiB:

| | KSM | `MADV_MERGEABLE` | content | saved (`pages_sharing`) |
|---|---|---|---|---|
| D | off | yes | same | 0 MiB |
| A | on | **no** (upstream) | same | **0 MiB** |
| B | on | yes | same | **458 MiB** (102% of ideal; Pss drop 458 MiB) |
| C | on | yes | unique | 10 MiB |

- B goes above 100% because ~10 MiB of guest-kernel pages are also identical
  across forks; C shows that same 10 MiB.
- The serve process's **Pss drop equals `pages_sharing`**, so Pss is a sound
  sandbox-owned-memory metric with KSM on (RSS does not drop: a merged page is
  still mapped by every fork).
- **Convergence:** with `ksm_ctl.sh on` defaults (`pages_to_scan=10000`,
  `sleep_millisecs=20`) KSM reached steady state within the 3 s ramp. This
  image boots with 100 pages / 200 ms (≈ 2 MB/s), which would need minutes for
  the same 450 MiB. Scan rate is a first-class knob for Phase 4.

`MemAvailable` deltas (`used+` in probe output) swing by ±100s of MiB from
unrelated host activity at this scale. Use the smaps and KSM counters instead.

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
   - `40_cow_probe.py` / `45_ksm_gate.py` need the Phase 1b agent (`init_agentzip.c`);
     against the stock agent they can't hold a fork or dirty memory.
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
- **Node kernel** `6.18.0-061800-generic` (host) is fine (KVM/userfaultfd/KSM present; paper used 5.15).

## vmstate parser: independent-anchor fix (was the blocker)

`zeroboot bench`/`serve` first call `load_snapshot` → `parse_vmstate`
([vmstate.rs:4](../../zeroboot/src/vmm/vmstate.rs#L4)). The **original** parser
located every CPU-state field by a single *global* offset shift anchored on the
IOAPIC base `0xFEC00000`, with the rest at fixed distances from it. That fails on
this node: Firecracker's versionize vmstate has variable-length sections
(MSR/CPUID vectors, per-device state), so sections shift **independently**.
Confirmed with `tools/inspect_vmstate.py`: IOAPIC at `0x5a4` (ref `0x591`,
shift +19) but the vCPU block moved the *other* way, so the assumed IOAPIC→EFER
distance (`0x2564`) no longer held (real `0x209c`) and the single-shift
auto-detect `bail`ed with *"IOAPIC base address 0xFEC00000 not found"* — the exact
"sections shift independently" bug the CHANGELOG lists as unfixed. (Changing the
Firecracker patch version does not help: vmstate is the same 14677 B under 1.12.0
and 1.12.1.)

**Fix — anchor two things by *content*, independently, then derive the rest:**

1. **IOAPIC** — its MMIO base constant `0xFEC00000`.
2. **EFER / vCPU block** — the EFER (`0xD01`/`0x501`) + APIC-base (`0xFEE00xxx`)
   adjacency, cross-checked against a plausible CR0 (PE|PG set) and CR3
   (nonzero page-table base, **PCID-aware**) just before it. The CR check disambiguates the real EFER from the
   same value sitting in the MSR list.

`LAPIC..XSAVE` is one *rigid* kvm-vcpu struct, so once EFER is located the
remaining fields follow at fixed, build-stable deltas — no global shift. The old
`REF_*` table is kept only to compute those deltas, never as absolute positions.

The fix is [`../zeroboot-pin/patches/0001-vmstate-independent-anchor-pcid.patch`](../zeroboot-pin/patches/0001-vmstate-independent-anchor-pcid.patch);
`00_provision.sh` → `setup_zeroboot.sh` applies it. If you change `vmstate.rs`,
regenerate the patch (see `../zeroboot-pin/README.md`) and keep
`tools/inspect_vmstate.py` in sync — it must mirror the Rust exactly.

**Verify the anchors landed before trusting `bench`.** `tools/inspect_vmstate.py`
is a faithful mirror of the Rust `resolve_offsets`: it derives the same offsets,
decodes RIP/CR3/EFER/GDT/IDT/…, runs the same validations, and **exits non-zero**
if any hard check fails — use it as a pre-`bench` gate:

```bash
python3 tools/inspect_vmstate.py /tmp/zb-workdir-agent/snapshot/vmstate
# "ALL CHECKS PASS" => the Rust parser anchors identically; proceed to 25_bench.sh.
# add --diag for a hexdump around the IOAPIC anchor.
```

> ✅ **Verified on-node (2026-11-19)** against the real 512 MiB snapshot: IOAPIC
> at `0x5a4` (shift +19), vCPU block at EFER `0x2640` (shift −1205); RIP
> `0xffffffff819a220b`, CR3 `0x38de005`. `bench` passes end to end (100/100
> fork+exec, 1000 concurrent forks, isolation PASS).
>
> **PCID gotcha (was the second blocker):** the first version of the EFER anchor
> required CR3 to be 4K-aligned. The guest kernel runs with `CR4.PCIDE` (bit 17)
> set, so CR3's low 12 bits hold the PCID (`0x005` here) and the only real
> candidate was rejected ("cannot locate EFER/vCPU block"). The check now
> requires a nonzero page-table base (bits 12–51) with reserved bits 52–63 clear,
> and allows only PWT/PCD in the low bits when PCIDE is off. The Rust
> `cr3_plausible` and the Python `cr3_plausible` in the inspector are the same check.

### Parser-free fallback: `26_cow_mmap_demo.py`

The Phase 1 **memory-sharing** proof lives at the `mmap(MAP_PRIVATE)` layer and
never needed the parser at all. It stays the quickest proof, and the safety net
if `inspect_vmstate.py` ever fails on a future build:

```bash
python3 26_cow_mmap_demo.py --n 16                 # read-only: Pss & physical ~= one snapshot
python3 26_cow_mmap_demo.py --n 16 --touch-mb 50   # Private_Dirty ~= N x 50 MiB
python3 26_cow_mmap_demo.py --n 1000 --no-read     # 1000 mappings ~ free (Zeroboot's 265 KB/fork)
```

## Node gotchas (all handled by the scripts; listed so you know why)

- **Socket timeout.** Firecracker's Full snapshot (a dev-preview path) writes the
  mem file at ~80 MB/s (256 MB → 3.2 s; 512 MB → ~6.4 s). Upstream Zeroboot's
  socket timeout is hardcoded at **5 s** ([firecracker.rs:9](../../zeroboot/src/vmm/firecracker.rs#L9)),
  so snapshots ≥ ~400 MB fail with `Resource temporarily unavailable (os error 11)`
  — EAGAIN from the timed-out read, not a snapshot failure. Fixed by
  `zeroboot-pin/patches/0002-firecracker-socket-timeout-120s.patch`.
- **Guest kernel isn't in the repo.** `guest/vmlinux-fc` is gitignored upstream;
  `setup_zeroboot.sh` downloads Firecracker's CI 6.1.128 kernel and checks it
  against a pinned sha256.
- **fd limit.** 1000 concurrent forks exceed the default 1024 open files
  (`Too many open files (os error 24)`): each fork holds KVM VM + vCPU fds.
  `25_bench.sh` and `30_serve.sh` raise `ulimit -n` (override with `NOFILE=`).
- **Root-owned build state.** `sudo` on these images keeps `HOME`, so running
  rustup/cargo under sudo leaves root-owned `~/.cargo`, `~/.rustup`,
  `~/zeroboot/target`. `00_provision.sh` now builds as `$SUDO_USER` and
  `chown`s any of those it finds root-owned.
- **Local disk.** Keep `WORKDIR` on local disk (`/tmp`, the default); some
  CloudLab profiles mount homes over NFS, which makes snapshot writes slower.
- **Debugging `template`.** To see guest boot / Firecracker errors, temporarily
  make its child stdio inherit (don't commit this into a patch):
  `sed -i -e 's/\.stdout(Stdio::null())/.stdout(Stdio::inherit())/' -e 's/\.stderr(Stdio::piped())/.stderr(Stdio::inherit())/' $ZB_DIR/src/vmm/firecracker.rs`.
- **Reading `bench`.** "KB/fork" is `total_RSS / count`. Read the *trend* across
  10/100/1000; bench forks never run a vCPU, so RSS stays far below the snapshot.
- **`40_cow_probe.py`** needs root to read the serve process's `smaps_rollup`;
  pick `--spins` high enough to outlast `--hold` (default 2e10 ≈ several s on a Xeon).

## Later phases

Built: see `../ROADMAP.md` and the root `README.md` (Phases 2–4: sandboxes, traces, replay + KSM).
