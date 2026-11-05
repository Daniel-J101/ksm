#!/usr/bin/env python3
"""Phase 1 deliverable: prove N Zeroboot forks share physical memory via CoW.

This is the scientific core of Phase 1 and is independent of how the template
rootfs was built -- it drives a running `zeroboot serve` over its HTTP API.

Idea
----
Zeroboot runs every fork as an in-process KVM VM inside the single `zeroboot
serve` process, and each fork maps the one snapshot memory file with
mmap(MAP_PRIVATE): reads hit the shared snapshot, writes fault in a private
page (see the paper's sandbox model, and Zeroboot's ARCHITECTURE.md). So the
*whole* CoW story is visible in that one process's /proc/<pid>/smaps_rollup:

  - the snapshot is mapped once at serve start (already in the baseline),
  - each live fork adds only its Private_Dirty CoW pages.

We hold N forks resident at once (N concurrent /v1/exec calls whose guest code
sleeps), sample the serve process's memory while they are up, and compare the
measured growth against the naive no-sharing cost (N x snapshot_MB).

Two regimes (choose with --touch-mb):
  --touch-mb 0   each fork only reads   -> growth ~ 0        (pure sharing)
  --touch-mb M   each fork dirties M MB -> growth ~ N x M    (CoW isolates writes;
                 private grows with *touched* bytes, never with snapshot size)

Run ON THE CLOUDLAB NODE, as root (smaps_rollup of the root-owned serve process):

  sudo python3 40_cow_probe.py --sweep 1,2,4,8,16 --hold 20 --touch-mb 0
  sudo python3 40_cow_probe.py --sweep 1,2,4,8,16 --hold 20 --touch-mb 50

Only the Python standard library is used, so nothing needs installing on-node.
"""

import argparse
import csv
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone


# --------------------------------------------------------------------------- #
# Reading host memory                                                         #
# --------------------------------------------------------------------------- #
def read_meminfo():
    """Return /proc/meminfo as {key: kB_int}."""
    out = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, _, rest = line.partition(":")
            out[k.strip()] = int(rest.strip().split()[0])  # kB
    return out


def read_smaps_rollup(pid):
    """Return the serve process's rolled-up smaps as {key: kB_int}.

    Falls back to `sudo cat` if the process is root-owned and we are not.
    """
    path = f"/proc/{pid}/smaps_rollup"
    try:
        with open(path) as f:
            text = f.read()
    except PermissionError:
        text = subprocess.check_output(["sudo", "cat", path], text=True)
    out = {}
    for line in text.splitlines():
        if ":" in line and "kB" in line:
            k, _, rest = line.partition(":")
            try:
                out[k.strip()] = int(rest.strip().split()[0])
            except (ValueError, IndexError):
                pass
    return out


def find_serve_pid(explicit):
    if explicit:
        return explicit
    try:
        out = subprocess.check_output(
            ["pgrep", "-f", "zeroboot serve"], text=True).split()
        # pgrep -f also matches this probe if someone named it oddly; take the
        # lowest PID, which is the long-lived server.
        pids = sorted(int(p) for p in out)
        if pids:
            return pids[0]
    except subprocess.CalledProcessError:
        pass
    sys.exit("could not find a 'zeroboot serve' process; pass --serve-pid")


# --------------------------------------------------------------------------- #
# Driving the Zeroboot HTTP API                                               #
# --------------------------------------------------------------------------- #
def http_post(base, path, body, api_key, timeout):
    data = json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def http_get(base, path, api_key, timeout=10):
    req = urllib.request.Request(base + path, method="GET")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


HOLD_CODE = r"""
import time
mb = {touch}
_keep = None
if mb > 0:
    b = bytearray(mb * 1024 * 1024)
    # touch every page so the write actually faults in a private CoW page
    for i in range(0, len(b), 4096):
        b[i] = 0xAB
    _keep = b
time.sleep({hold})
print("held", mb)
"""


def hold_fork(base, api_key, hold, touch, timeout, results, idx, barrier):
    """One concurrent fork: fire a single /v1/exec that sleeps `hold` seconds."""
    code = HOLD_CODE.format(touch=touch, hold=hold)
    body = {"code": code, "language": "python", "timeout_seconds": hold + 20}
    barrier.wait()  # release all forks together
    t0 = time.time()
    try:
        resp = http_post(base, "/v1/exec", body, api_key, timeout)
        results[idx] = {"ok": resp.get("exit_code") == 0,
                        "fork_time_ms": resp.get("fork_time_ms"),
                        "wall_s": time.time() - t0}
    except Exception as e:  # noqa: BLE001
        results[idx] = {"ok": False, "error": repr(e), "wall_s": time.time() - t0}


# --------------------------------------------------------------------------- #
# One measurement point: hold N forks, sample memory at steady state          #
# --------------------------------------------------------------------------- #
def measure_point(base, api_key, pid, n, hold, touch, ramp, settle):
    """Hold `n` forks, sample serve smaps + meminfo, return the steady peak."""
    results = [None] * n
    barrier = threading.Barrier(n + 1)
    threads = []
    for i in range(n):
        t = threading.Thread(target=hold_fork,
                             args=(base, api_key, hold, touch, hold + 30,
                                   results, i, barrier))
        t.start()
        threads.append(t)

    barrier.wait()                      # all forks fire now
    time.sleep(ramp)                    # let every fork boot + touch its memory

    # Sample repeatedly over the remaining hold window; keep the peak Pss, which
    # is steady state once all forks are resident.
    peak = None
    deadline = time.time() + max(0.0, hold - ramp - settle)
    samples = 0
    while time.time() < deadline:
        roll = read_smaps_rollup(pid)
        mi = read_meminfo()
        pss = roll.get("Pss", 0)
        if peak is None or pss > peak["serve_Pss_kB"]:
            peak = {
                "serve_Rss_kB": roll.get("Rss", 0),
                "serve_Pss_kB": pss,
                "serve_Private_Dirty_kB": roll.get("Private_Dirty", 0),
                "serve_Private_Clean_kB": roll.get("Private_Clean", 0),
                "serve_Shared_Clean_kB": roll.get("Shared_Clean", 0),
                "serve_Shared_Dirty_kB": roll.get("Shared_Dirty", 0),
                "MemAvailable_kB": mi.get("MemAvailable", 0),
                "MemFree_kB": mi.get("MemFree", 0),
            }
        samples += 1
        time.sleep(0.5)

    for t in threads:
        t.join()
    time.sleep(settle)                  # let destroyed forks release pages

    fork_ms = [r.get("fork_time_ms") for r in results
               if r and r.get("fork_time_ms") is not None]
    return {
        "n": n,
        "samples": samples,
        "forks_ok": sum(1 for r in results if r and r.get("ok")),
        "forks_failed": sum(1 for r in results if not (r and r.get("ok"))),
        "fork_time_ms_mean": round(sum(fork_ms) / len(fork_ms), 3) if fork_ms else None,
        **(peak or {}),
        "fork_errors": [r.get("error") for r in results
                        if r and r.get("error")][:3],
    }


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=os.environ.get("ZEROBOOT_BASE",
                                                      "http://127.0.0.1:8080"))
    ap.add_argument("--api-key", default=os.environ.get("ZEROBOOT_API_KEY", ""))
    ap.add_argument("--serve-pid", type=int, default=0,
                    help="PID of `zeroboot serve` (auto-detected if omitted)")
    ap.add_argument("--sweep", default="1,2,4,8,16",
                    help="comma list of fork counts to measure")
    ap.add_argument("--hold", type=float, default=20.0,
                    help="seconds each fork stays resident (keep < exec timeout)")
    ap.add_argument("--touch-mb", type=int, default=0,
                    help="MB each fork dirties (0 = pure read / sharing regime)")
    ap.add_argument("--ramp", type=float, default=3.0,
                    help="seconds to wait after firing before sampling")
    ap.add_argument("--settle", type=float, default=2.0)
    ap.add_argument("--outdir", default="results")
    args = ap.parse_args()

    sweep = [int(x) for x in args.sweep.split(",") if x.strip()]
    pid = find_serve_pid(args.serve_pid)

    # Snapshot size from the health endpoint -> the no-sharing cost per fork.
    health = http_get(args.base, "/v1/health", args.api_key)
    templates = health.get("templates", {})
    snap_mb = None
    for t in templates.values():
        if isinstance(t, dict) and t.get("memory_mb"):
            snap_mb = t["memory_mb"]
            break
    if not snap_mb:
        snap_mb = int(os.environ.get("ZEROBOOT_MEM_MIB", "512"))
        print(f"[warn] health gave no memory_mb; assuming snapshot = {snap_mb} MB")

    print(f"serve pid={pid}  snapshot={snap_mb} MB  base={args.base}")
    print(f"health status={health.get('status')}  templates={list(templates)}")

    # Baseline: serve process with no forks up.
    base_roll = read_smaps_rollup(pid)
    base_mi = read_meminfo()
    base_pss = base_roll.get("Pss", 0)
    base_avail = base_mi.get("MemAvailable", 0)
    print(f"baseline serve Pss={base_pss/1024:.1f} MB  "
          f"MemAvailable={base_avail/1024/1024:.2f} GB\n")

    rows = []
    for n in sweep:
        pt = measure_point(args.base, args.api_key, pid, n,
                           args.hold, args.touch_mb, args.ramp, args.settle)
        # Growth attributable to the N live forks.
        serve_added_mb = (pt.get("serve_Pss_kB", 0) - base_pss) / 1024.0
        avail_drop_mb = (base_avail - pt.get("MemAvailable_kB", 0)) / 1024.0
        naive_mb = n * snap_mb
        pt["serve_added_MB"] = round(serve_added_mb, 2)
        pt["mem_used_delta_MB"] = round(avail_drop_mb, 2)
        pt["naive_nocow_MB"] = naive_mb
        pt["per_fork_private_MB"] = round(serve_added_mb / n, 3) if n else 0
        # How much of the naive cost CoW eliminated (serve-process view).
        pt["cow_sharing_pct"] = round(100 * (1 - serve_added_mb / naive_mb), 2) \
            if naive_mb else None
        rows.append(pt)
        print(f"N={n:>3}  serve+={serve_added_mb:8.1f} MB  "
              f"used+={avail_drop_mb:8.1f} MB  naive={naive_mb:8.0f} MB  "
              f"per-fork={pt['per_fork_private_MB']:7.3f} MB  "
              f"CoW-saved={pt['cow_sharing_pct']}%  "
              f"ok={pt['forks_ok']}/{n}")

    # Persist.
    os.makedirs(args.outdir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    tag = f"touch{args.touch_mb}"
    summary = {
        "when_utc": stamp,
        "base": args.base,
        "serve_pid": pid,
        "snapshot_mb": snap_mb,
        "hold_s": args.hold,
        "touch_mb": args.touch_mb,
        "baseline_serve_Pss_MB": round(base_pss / 1024, 2),
        "baseline_MemAvailable_GB": round(base_avail / 1024 / 1024, 2),
        "points": rows,
    }
    jpath = os.path.join(args.outdir, f"cow-{stamp}-{tag}.json")
    cpath = os.path.join(args.outdir, f"cow-{stamp}-{tag}.csv")
    with open(jpath, "w") as f:
        json.dump(summary, f, indent=2)
    with open(cpath, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["n", "forks_ok", "fork_time_ms_mean", "serve_added_MB",
                    "mem_used_delta_MB", "naive_nocow_MB", "per_fork_private_MB",
                    "cow_sharing_pct"])
        for r in rows:
            w.writerow([r["n"], r["forks_ok"], r.get("fork_time_ms_mean"),
                        r["serve_added_MB"], r["mem_used_delta_MB"],
                        r["naive_nocow_MB"], r["per_fork_private_MB"],
                        r["cow_sharing_pct"]])

    # Headline.
    if rows:
        top = rows[-1]
        print(f"\n== Phase 1 result ==")
        print(f"{top['n']} forks of a {snap_mb} MB snapshot cost "
              f"{top['serve_added_MB']:.1f} MB of private memory "
              f"(naive no-CoW would be {top['naive_nocow_MB']:.0f} MB) "
              f"-> CoW eliminated {top['cow_sharing_pct']}%.")
        if args.touch_mb == 0:
            print("Touch-free regime: near-zero growth proves forks SHARE the "
                  "snapshot. Re-run with --touch-mb 50 to see CoW isolate writes.")
        else:
            print(f"Each fork dirtied ~{args.touch_mb} MB; measured per-fork "
                  f"private = {top['per_fork_private_MB']:.1f} MB. Private memory "
                  f"tracks *touched* bytes, not snapshot size -- that is CoW.")
    print(f"\nwrote {jpath}\n      {cpath}")


if __name__ == "__main__":
    main()
