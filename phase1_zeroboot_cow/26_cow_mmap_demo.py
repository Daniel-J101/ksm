#!/usr/bin/env python3
"""Phase 1 CoW proof at the mmap layer -- the mechanism, on the REAL snapshot.

Zeroboot's vmstate parser can't anchor on this Firecracker build (sections
shifted independently -- see tools/inspect_vmstate.py), so `bench` can't restore
CPU state and run VMs. But the memory *sharing* the paper and Zeroboot rely on is
not the CPU restore -- it is `mmap(MAP_PRIVATE)` of the one snapshot memory file
(kvm.rs:66). This demonstrates that directly on the actual snapshot/mem file: map
it N times copy-on-write, exactly as N forks would, and measure real memory.

What N forks cost is NOT VmRSS (which counts every shared page once per mapping,
so it looks like N x snapshot even when physical is shared). The truths are:
  * Pss (proportional set size): shared pages divided among mappers -> ~1 snapshot
  * Private_Dirty: only pages a mapping WROTE -> ~0 until you touch, then ~N x touch
  * system physical (MemAvailable drop): ~1 snapshot + the private dirtied pages

Run on the node (snapshot already built by 20_make_template.sh):
  python3 26_cow_mmap_demo.py --n 16                      # 16 forks, read-only
  python3 26_cow_mmap_demo.py --n 1000                    # 1000 forks share one snapshot
  python3 26_cow_mmap_demo.py --n 16 --touch-mb 50        # each fork dirties 50 MiB

Stdlib only. No root, no KVM, no Firecracker.
"""
import argparse
import mmap
import os
import resource


def smaps_rollup():
    out = {}
    try:
        with open("/proc/self/smaps_rollup") as f:
            for line in f:
                if ":" in line and "kB" in line:
                    k, _, rest = line.partition(":")
                    out[k.strip()] = int(rest.strip().split()[0])
    except OSError:
        pass
    return out


def meminfo():
    out = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, _, rest = line.partition(":")
            out[k.strip()] = int(rest.strip().split()[0])
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mem-file", default="/tmp/zb-workdir-agent/snapshot/mem")
    ap.add_argument("--n", type=int, default=16, help="number of CoW mappings (forks)")
    ap.add_argument("--touch-mb", type=int, default=0,
                    help="MiB each mapping writes (dirties private CoW pages)")
    ap.add_argument("--no-read", action="store_true",
                    help="skip faulting in shared pages (mimics an unrun fork)")
    args = ap.parse_args()

    size = os.path.getsize(args.mem_file)
    size_mb = size / 1024 / 1024
    fd = os.open(args.mem_file, os.O_RDONLY)

    base_roll = smaps_rollup()
    base_avail = meminfo().get("MemAvailable", 0)
    print(f"snapshot mem file: {args.mem_file}  ({size_mb:.0f} MiB)")
    print(f"mapping {args.n} copies MAP_PRIVATE  read={'no' if args.no_read else 'yes'}  "
          f"touch={args.touch_mb} MiB/each")
    print(f"naive no-CoW cost = {args.n} x {size_mb:.0f} = {args.n*size_mb:.0f} MiB\n")

    maps = []
    touch_bytes = args.touch_mb * 1024 * 1024
    for _ in range(args.n):
        mm = mmap.mmap(fd, size, flags=mmap.MAP_PRIVATE,
                       prot=mmap.PROT_READ | mmap.PROT_WRITE)
        if not args.no_read:
            # fault in every page: shared with the file's page cache (not copied)
            for off in range(0, size, 4096):
                _ = mm[off]
        for off in range(0, min(touch_bytes, size), 4096):
            mm[off] = 0xAB  # write -> private CoW page for THIS mapping
        maps.append(mm)

    roll = smaps_rollup()
    avail_drop_mb = (base_avail - meminfo().get("MemAvailable", 0)) / 1024.0
    rss_mb = roll.get("Rss", 0) / 1024.0
    pss_mb = roll.get("Pss", 0) / 1024.0
    priv_mb = roll.get("Private_Dirty", 0) / 1024.0
    shared_mb = roll.get("Shared_Clean", 0) / 1024.0
    maxrss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0

    print(f"after mapping {args.n} copies:")
    print(f"  VmRSS         {rss_mb:9.1f} MiB   (counts shared pages once PER mapping)")
    print(f"  Pss           {pss_mb:9.1f} MiB   <- proportional: ~1 snapshot = SHARING")
    print(f"  Shared_Clean  {shared_mb:9.1f} MiB")
    print(f"  Private_Dirty {priv_mb:9.1f} MiB   <- only what mappings WROTE")
    print(f"  system used + {avail_drop_mb:9.1f} MiB   (MemAvailable drop = true physical)")
    print(f"  ru_maxrss     {maxrss_mb:9.1f} MiB\n")

    naive = args.n * size_mb
    # Physical cost is best read from the MemAvailable drop.
    saved = 100 * (1 - avail_drop_mb / naive) if naive else 0
    print(f"== CoW result ==")
    print(f"{args.n} CoW mappings of a {size_mb:.0f} MiB snapshot used ~{avail_drop_mb:.0f} MiB "
          f"physical (naive no-CoW = {naive:.0f} MiB) -> CoW eliminated {saved:.1f}%.")
    if args.touch_mb == 0:
        print("Read-only: physical ~= one snapshot regardless of N -> forks SHARE it.")
    else:
        per = priv_mb / args.n if args.n else 0
        print(f"Private_Dirty/mapping ~ {per:.1f} MiB ~= the {args.touch_mb} MiB each wrote: "
              f"CoW privatizes only written pages, independent of snapshot size.")

    for mm in maps:
        mm.close()
    os.close(fd)


if __name__ == "__main__":
    main()
