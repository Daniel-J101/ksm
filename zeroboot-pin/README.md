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
| `setup_zeroboot.sh` | clone → checkout pin → apply patches (skip if already applied) → fetch + verify kernel → build with pinned Rust → `make guest` |

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

Edit in `$ZB_DIR`, rebuild, test, then regenerate the patch from the pinned commit:

```bash
cd ~/zeroboot
git diff src/vmm/vmstate.rs CHANGELOG.md > ~/ksm/zeroboot-pin/patches/0001-vmstate-independent-anchor-pcid.patch
git diff src/vmm/firecracker.rs          > ~/ksm/zeroboot-pin/patches/0002-firecracker-socket-timeout-120s.patch
# a new, unrelated change -> a new numbered patch:
git diff src/api/handlers.rs             > ~/ksm/zeroboot-pin/patches/0003-<what>.patch
```

Each patch must apply cleanly to `ZB_COMMIT`, in numbered order. The simplest
check is a throwaway fresh checkout:
`ZB_DIR=/tmp/zb-check SKIP_BUILD=1 bash ~/ksm/zeroboot-pin/setup_zeroboot.sh && rm -rf /tmp/zb-check`.

To move to a newer upstream: bump `ZB_COMMIT`, run `setup_zeroboot.sh` on a fresh
`ZB_DIR`, fix any patches that no longer apply, and re-run the Phase 1 gate
(`inspect_vmstate.py` + `25_bench.sh`).
