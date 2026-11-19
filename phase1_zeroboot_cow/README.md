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
python3   tools/inspect_vmstate.py /tmp/zb-workdir-agent/snapshot/vmstate  # gate: anchors OK?
sudo      bash 25_bench.sh         # zeroboot bench -> the CoW proof
```

> `bench`/`serve` restore guest CPU state via `parse_vmstate`. On this node that
> needed the independent-anchor fix — see "vmstate parser" below; run
> `inspect_vmstate.py` once after step 20 to confirm it anchors before `bench`.

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
   adjacency, cross-checked against a plausible CR0 (PE|PG set) and CR3 (nonzero,
   4K-aligned) just before it. The CR check disambiguates the real EFER from the
   same value sitting in the MSR list.

`LAPIC..XSAVE` is one *rigid* kvm-vcpu struct, so once EFER is located the
remaining fields follow at fixed, build-stable deltas — no global shift. The old
`REF_*` table is kept only to compute those deltas, never as absolute positions.

This is already applied to `src/vmm/vmstate.rs` in the checkout here. Because
`zeroboot/` is a **separate checkout on the node** (and gitignored from this
repo), apply the same change there before rebuilding:

```bash
cd ~/zeroboot
git apply /path/to/phase1_zeroboot_cow/tools/vmstate_independent_anchor.patch
source $HOME/.cargo/env && cargo build --release
```

**Verify the anchors landed before trusting `bench`.** `tools/inspect_vmstate.py`
is a faithful mirror of the Rust `resolve_offsets`: it derives the same offsets,
decodes RIP/CR3/EFER/GDT/IDT/…, runs the same validations, and **exits non-zero**
if any hard check fails — use it as a pre-`bench` gate:

```bash
python3 tools/inspect_vmstate.py /tmp/zb-workdir-agent/snapshot/vmstate
# "ALL CHECKS PASS" => the Rust parser anchors identically; proceed to 25_bench.sh.
# add --diag for a hexdump around the IOAPIC anchor.
```

> ⚠️ Developed on this Mac, where the Rust can't be compiled (`kvm-bindings` is
> Linux-only) and there's no real `vmstate`. The algorithm was validated against
> a synthetic vmstate reproducing this node's independent shift; the
> `inspect_vmstate.py` gate is the on-node confirmation that it anchors correctly
> on the *real* snapshot. Run it first.

### Parser-free fallback: `26_cow_mmap_demo.py`

The Phase 1 **memory-sharing** proof lives at the `mmap(MAP_PRIVATE)` layer and
never needed the parser at all. It stays the quickest proof, and the safety net
if `inspect_vmstate.py` ever fails on a future build:

```bash
python3 26_cow_mmap_demo.py --n 16                 # read-only: Pss & physical ~= one snapshot
python3 26_cow_mmap_demo.py --n 16 --touch-mb 50   # Private_Dirty ~= N x 50 MiB
python3 26_cow_mmap_demo.py --n 1000 --no-read     # 1000 mappings ~ free (Zeroboot's 265 KB/fork)
```

## Known issue: raise Zeroboot's socket timeout before step 20

Firecracker's **Full snapshot is a dev-preview path** that writes the mem file at
~80 MB/s (measured on CloudLab: 256 MB → 3.2 s; 512 MB → ~6.4 s). Zeroboot's
socket timeout is hardcoded at **5 s** ([firecracker.rs:9](../../zeroboot/src/vmm/firecracker.rs#L9)),
so any snapshot ≥ ~400 MB fails with `Error: Resource temporarily unavailable
(os error 11)` — an EAGAIN from the timed-out `stream.read`, not a snapshot
failure. Fix once in the source and rebuild:

```bash
sudo sed -i 's/from_secs(5)/from_secs(120)/g' $ZB_DIR/src/vmm/firecracker.rs
sudo bash -lc 'source $HOME/.cargo/env; cd $ZB_DIR && cargo build --release'
```

Two more node gotchas: the repo may be **root-owned** (edit/build with `sudo`),
and CloudLab **home dirs are NFS** — keep `WORKDIR` on local disk (`/tmp`), the
default here, or snapshot writes are even slower. To see guest boot / Firecracker
errors during `template`, temporarily make its child stdio inherit:
`sed -i -e 's/\.stdout(Stdio::null())/.stdout(Stdio::inherit())/' -e 's/\.stderr(Stdio::piped())/.stderr(Stdio::inherit())/' $ZB_DIR/src/vmm/firecracker.rs`.

## Steps I could not test from the WSL dev box (verify on-node)

- `00`: `make guest` needs `musl-tools`; `make build` needs the Rust toolchain on PATH.
- `10`: loop-mounting ext4 needs root; `debugfs` listing is best-effort.
- `20`: if `template` fails, re-run with `ZEROBOOT_DEBUG=1`; confirm `snapshot/{mem,vmstate}` appear.
- `25`: `bench` needs the vmstate fix applied + rebuilt (`git apply` the patch,
  `cargo build --release`) and `inspect_vmstate.py` reporting "ALL CHECKS PASS"
  first. Its "KB/fork" is `total_RSS / count` — read the *trend* across
  10/100/1000, not the absolute, as the sharing signal.
- `40`: needs root to read the serve process's `smaps_rollup`; pick `--spins` high
  enough to outlast `--hold` on this CPU (default 2e10 ≈ several s on a Xeon).

## Next phases (not built)

- **Phase 2 — trace record:** a Python-capable agent + DeepSeek, recording tool
  actions + `think_time_ms_before` (§5.3).
- **Phase 3 — replay + KSM baseline:** deterministic replay, `M(t)` 1/s, KSM dedup
  (builds on `../../numa_ksm_bench/agentzip_ksm_bench/`).
- **Phase 4 — AgentZip codecs:** template-delta + cohort-dictionary + RLE.
