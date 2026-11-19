# zeroboot-pin — the exact Zeroboot every ksm node builds

We don't vendor Zeroboot into ksm. We pin an upstream commit and keep our changes
as patches here. `setup_zeroboot.sh` turns that into a built checkout at
`$ZB_DIR` (default `~/zeroboot`, local disk, outside ksm).

Why not vendor it: ksm is shared between nodes, and a vendored tree would put
`target/` (Rust build output) and the 40 MB guest kernel in the shared directory,
where nodes would overwrite each other's builds. Patches keep our changes to
Zeroboot small enough to review and diff against upstream.

| File | What |
|---|---|
| `UPSTREAM` | repo URL + pinned commit, guest-kernel URL + sha256, Firecracker version, Rust toolchain |
| `patches/0001-vmstate-independent-anchor-pcid.patch` | `parse_vmstate` anchors the IOAPIC and vCPU blocks independently by content, with a PCID-aware CR3 check (+ CHANGELOG entry). Without it, `bench`/`serve` fail to load the snapshot. |
| `patches/0002-firecracker-socket-timeout-120s.patch` | Firecracker API socket timeout 5 s → 120 s, so ≥ 400 MB snapshots don't fail with `os error 11`. |
| `patches/0003-ksm-mergeable-forks.patch` | `ZEROBOOT_KSM_MERGEABLE=1` → `madvise(MADV_MERGEABLE)` on every fork mapping. Off by default (upstream behaviour). Without it ksmd never scans fork memory. |
| `patches/0004-restore-guest-clock.patch` | Forks continue the guest clock from the snapshot: restore `IA32_TSC` + `TSC_DEADLINE` (parsed from Firecracker's MSR chunks by their exact layout) and `KVM_SET_CLOCK` to the guest's kvmclock time at snapshot. Without it every fork's TSC restarts at 0, so the **first sleep/timeout in a fork stalls for (boot → snapshot) seconds** (= template `WAIT_SECS`). Also logs if `KVM_SET_MSRS` stops early. |
| `new_patch.sh` | write the changes in `$ZB_DIR` not yet covered by the series as the next numbered patch |
| `setup_zeroboot.sh` | clone → checkout pin → apply patches (already-applied prefix detected by exact git-tree match) → fetch + verify kernel → build with pinned Rust → `make guest` |

## Use

Normally you don't run this directly: `sudo bash phase1_zeroboot_cow/00_provision.sh`
installs system packages and then runs it as your user. On its own:

```bash
bash zeroboot-pin/setup_zeroboot.sh                 # -> ~/zeroboot
ZB_DIR=/local/zb bash zeroboot-pin/setup_zeroboot.sh
SKIP_BUILD=1 bash zeroboot-pin/setup_zeroboot.sh    # checkout + patches + kernel only
```

Run it as a normal user, never root. It's safe to re-run. It stops with an
error if `$ZB_DIR` is on a different commit with local edits, or if a patch
neither applies nor is already applied.

## Changing Zeroboot

Patches **stack**: later ones edit code that earlier ones added. So never
regenerate an old patch with a plain `git diff` against the pin. Instead:

```bash
# edit + build + test in ~/zeroboot, then:
bash ~/ksm/zeroboot-pin/new_patch.sh --dry-run          # what's not in the series yet
bash ~/ksm/zeroboot-pin/new_patch.sh <short-name>       # -> patches/000N-<short-name>.patch
ZB_DIR=/tmp/zb-check SKIP_BUILD=1 bash ~/ksm/zeroboot-pin/setup_zeroboot.sh && rm -rf /tmp/zb-check
```

`setup_zeroboot.sh` finds which prefix of the series a checkout already has
by comparing git trees exactly, so re-running it is always safe. It refuses a
tree that matches no prefix.

To move to a newer upstream: bump `ZB_COMMIT`, run `setup_zeroboot.sh` on a fresh
`ZB_DIR`, fix any patches that no longer apply, and re-run the Phase 1 gate
(`inspect_vmstate.py` + `25_bench.sh`).
