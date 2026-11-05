#!/usr/bin/env python3
"""Phase 1b (optional) -- CoW under execution: forks that actually dirty memory.

`25_bench.sh` already proves sharing + isolation with Zeroboot's own benchmark
(forks that never run). This probe goes one step further and matches the paper's
regime: hold N forks resident *while they dirty private pages*, and show that the
serve process's memory grows with *touched bytes*, not with N x snapshot.

REQUIRES the Phase 1b agent (init_agentzip.c) in the snapshot, which adds:
    work <mb> <spins>   # dirty <mb> MiB private, then busy-spin <spins> to hold
Build that agent, rebuild the rootfs with AGENT=.../init_agentzip, re-snapshot,
then `zeroboot serve`. (The stock echo/cat agent cannot hold a fork or dirty
memory, so this probe is meaningless against it -- use 25_bench.sh there.)

Idea: each /v1/exec holds a fork for its request duration (handlers.rs drops the
VM when the response returns). We fire N concurrent `work` requests whose busy
spin outlasts our sampling window, so N forks are resident at once. Meanwhile we
sample the serve process's RSS (it runs every fork in-process) and /proc/meminfo.

  --touch-mb 0    forks only spin           -> growth ~ 0        (pure sharing)
  --touch-mb 50   each fork dirties 50 MiB  -> growth ~ N x 50   (private CoW)

Run ON THE NODE as root (reads the root-owned serve process's smaps):
  sudo python3 40_cow_probe.py --sweep 1,2,4,8,16 --hold 15 --touch-mb 0
  sudo python3 40_cow_probe.py --sweep 1,2,4,8,16 --hold 15 --touch-mb 50

Stdlib only.
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


def read_meminfo():
    out = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, _, rest = line.partition(":")
            out[k.strip()] = int(rest.strip().split()[0])  # kB
    return out


def read_smaps_rollup(pid):
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
        out = subprocess.check_output(["pgrep", "-f", "zeroboot serve"], text=True).split()
        pids = sorted(int(p) for p in out)
        if pids:
            return pids[0]
    except subprocess.CalledProcessError:
        pass
    sys.exit("could not find a 'zeroboot serve' process; pass --serve-pid")


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


def hold_fork(base, api_key, touch, spins, timeout, results, idx, barrier):
    """One concurrent fork held by a `work <mb> <spins>` serial command."""
    body = {"code": f"work {touch} {spins}", "language": "python",
            "timeout_seconds": timeout}
    barrier.wait()
    t0 = time.time()
    try:
        resp = http_post(base, "/v1/exec", body, api_key, timeout + 15)
        results[idx] = {"ok": resp.get("exit_code") == 0,
                        "fork_time_ms": resp.get("fork_time_ms"),
                        "wall_s": time.time() - t0}
    except Exception as e:  # noqa: BLE001
        results[idx] = {"ok": False, "error": repr(e), "wall_s": time.time() - t0}


def measure_point(base, api_key, pid, n, touch, spins, hold, ramp, settle):
    results = [None] * n
    barrier = threading.Barrier(n + 1)
    threads = []
    # exec timeout must outlast our sampling window so forks stay resident.
    exec_timeout = int(hold + ramp + 10)
    for i in range(n):
        t = threading.Thread(target=hold_fork,
                             args=(base, api_key, touch, spins, exec_timeout,
                                   results, i, barrier))
        t.start()
        threads.append(t)

    barrier.wait()
    time.sleep(ramp)  # let every fork boot, dirty its memory, enter the spin

    peak = None
    deadline = time.time() + hold
    samples = 0
    while time.time() < deadline:
        roll = read_smaps_rollup(pid)
        mi = read_meminfo()
        rss = roll.get("Rss", 0)
        if peak is None or rss > peak["serve_Rss_kB"]:
            peak = {
                "serve_Rss_kB": rss,
                "serve_Pss_kB": roll.get("Pss", 0),
                "serve_Private_Dirty_kB": roll.get("Private_Dirty", 0),
                "serve_Shared_Clean_kB": roll.get("Shared_Clean", 0),
                "MemAvailable_kB": mi.get("MemAvailable", 0),
            }
        samples += 1
        time.sleep(0.5)

    for t in threads:
        t.join()
    time.sleep(settle)

    fork_ms = [r.get("fork_time_ms") for r in results if r and r.get("fork_time_ms") is not None]
    return {
        "n": n, "samples": samples,
        "forks_ok": sum(1 for r in results if r and r.get("ok")),
        "fork_time_ms_mean": round(sum(fork_ms) / len(fork_ms), 3) if fork_ms else None,
        **(peak or {}),
        "fork_errors": [r.get("error") for r in results if r and r.get("error")][:3],
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=os.environ.get("ZEROBOOT_BASE", "http://127.0.0.1:8080"))
    ap.add_argument("--api-key", default=os.environ.get("ZEROBOOT_API_KEY", ""))
    ap.add_argument("--serve-pid", type=int, default=0)
    ap.add_argument("--sweep", default="1,2,4,8,16")
    ap.add_argument("--touch-mb", type=int, default=0, help="MiB each fork dirties")
    ap.add_argument("--spins", type=int, default=20_000_000_000,
                    help="busy-spin iterations to hold the fork (must outlast --hold)")
    ap.add_argument("--hold", type=float, default=15.0, help="seconds to sample at steady state")
    ap.add_argument("--ramp", type=float, default=3.0)
    ap.add_argument("--settle", type=float, default=2.0)
    ap.add_argument("--snapshot-mb", type=int, default=0,
                    help="snapshot MiB for the naive baseline (else read from mem file guess)")
    ap.add_argument("--outdir", default="results")
    args = ap.parse_args()

    sweep = [int(x) for x in args.sweep.split(",") if x.strip()]
    pid = find_serve_pid(args.serve_pid)

    health = http_get(args.base, "/v1/health", args.api_key)
    snap_mb = args.snapshot_mb or int(os.environ.get("ZEROBOOT_MEM_MIB", "512"))
    print(f"serve pid={pid}  snapshot~{snap_mb} MiB  base={args.base}  "
          f"health={health.get('status')} templates={list(health.get('templates', {}))}")

    base_roll = read_smaps_rollup(pid)
    base_mi = read_meminfo()
    base_rss = base_roll.get("Rss", 0)
    base_avail = base_mi.get("MemAvailable", 0)
    print(f"baseline serve RSS={base_rss/1024:.1f} MiB  "
          f"MemAvailable={base_avail/1024/1024:.2f} GiB\n")

    rows = []
    for n in sweep:
        pt = measure_point(args.base, args.api_key, pid, n, args.touch_mb,
                           args.spins, args.hold, args.ramp, args.settle)
        serve_added_mb = (pt.get("serve_Rss_kB", 0) - base_rss) / 1024.0
        avail_drop_mb = (base_avail - pt.get("MemAvailable_kB", 0)) / 1024.0
        naive_mb = n * snap_mb
        pt["serve_added_MB"] = round(serve_added_mb, 2)
        pt["mem_used_delta_MB"] = round(avail_drop_mb, 2)
        pt["naive_nocow_MB"] = naive_mb
        pt["per_fork_private_MB"] = round(serve_added_mb / n, 3) if n else 0
        pt["cow_sharing_pct"] = round(100 * (1 - serve_added_mb / naive_mb), 2) if naive_mb else None
        rows.append(pt)
        print(f"N={n:>3}  serve+={serve_added_mb:8.1f} MiB  used+={avail_drop_mb:8.1f} MiB  "
              f"naive={naive_mb:8.0f} MiB  per-fork={pt['per_fork_private_MB']:7.3f} MiB  "
              f"CoW-saved={pt['cow_sharing_pct']}%  ok={pt['forks_ok']}/{n}")

    os.makedirs(args.outdir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    tag = f"touch{args.touch_mb}"
    summary = {"when_utc": stamp, "base": args.base, "serve_pid": pid,
               "snapshot_mb": snap_mb, "touch_mb": args.touch_mb, "spins": args.spins,
               "hold_s": args.hold, "baseline_serve_Rss_MB": round(base_rss / 1024, 2),
               "points": rows}
    jpath = os.path.join(args.outdir, f"cow-{stamp}-{tag}.json")
    cpath = os.path.join(args.outdir, f"cow-{stamp}-{tag}.csv")
    with open(jpath, "w") as f:
        json.dump(summary, f, indent=2)
    with open(cpath, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["n", "forks_ok", "fork_time_ms_mean", "serve_added_MB",
                    "mem_used_delta_MB", "naive_nocow_MB", "per_fork_private_MB", "cow_sharing_pct"])
        for r in rows:
            w.writerow([r["n"], r["forks_ok"], r.get("fork_time_ms_mean"), r["serve_added_MB"],
                        r["mem_used_delta_MB"], r["naive_nocow_MB"], r["per_fork_private_MB"],
                        r["cow_sharing_pct"]])

    if rows:
        top = rows[-1]
        print(f"\n== Phase 1b result ==")
        print(f"{top['n']} forks (each dirtying {args.touch_mb} MiB) added "
              f"{top['serve_added_MB']:.1f} MiB to the serve process "
              f"(naive no-CoW would be {top['naive_nocow_MB']:.0f} MiB) -> "
              f"CoW eliminated {top['cow_sharing_pct']}%.")
        if args.touch_mb == 0:
            print("Touch-free: near-zero growth proves forks SHARE the snapshot.")
        else:
            print(f"per-fork private = {top['per_fork_private_MB']:.1f} MiB ~ the {args.touch_mb} "
                  f"MiB each fork touched: private memory tracks touched bytes, not snapshot size.")
    print(f"\nwrote {jpath}\n      {cpath}")


if __name__ == "__main__":
    main()
