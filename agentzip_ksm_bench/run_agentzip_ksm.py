#!/usr/bin/env python3
"""Measure KSM memory savings across a cohort of R2E-Gym agent sandboxes.

Runs on the CloudLab host (root, native Docker, Linux >= 5.17; 5.19+ gives the
per-process counters too). Launches N sibling sandboxes from R2E-Gym docker
images, lets them reach a steady memory state (via container_entry.py), then
starts ksmd and records how many identical 4 KiB pages KSM merges across them.

This reproduces the paper's *KSM baseline*: the memory KSM can reclaim is
exactly its byte-identical page sharing. See README.md.

Cohorts (what plays the role of "sibling sandboxes"):
  identical : the SAME task x N        -> upper bound; validates the pipeline
  siblings  : N DIFFERENT tasks, one repo -> realistic; shared template/libs
              but divergent private pages, the regime where KSM underperforms
              (and where AgentZip's delta/dictionary codecs would win).

KSM's merge counters (/sys/kernel/mm/ksm/pages_sharing, pages_shared) are
SYSTEM-WIDE, so run this on an otherwise idle node.
"""

import argparse
import csv
import datetime
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
KSM = Path("/sys/kernel/mm/ksm")
PAGE = 4096


def log(msg):
    print(f"[{datetime.datetime.now():%H:%M:%S}] {msg}", flush=True)


def sh(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, **kw)


class Ksm:
    """Reads/writes /sys/kernel/mm/ksm, restoring original values at exit.
    Same contract as numa_ksm_bench/run_bench.py."""

    KNOBS = ["merge_across_nodes", "smart_scan", "advisor_mode", "pages_to_scan",
             "sleep_millisecs", "use_zero_pages", "run"]

    def __init__(self):
        self.saved = {k: self.read(k) for k in self.KNOBS if self.has(k)}

    def has(self, knob):
        return (KSM / knob).exists()

    def read(self, knob):
        value = (KSM / knob).read_text().strip()
        if "[" in value:  # advisor_mode prints "[none] scan-time"
            value = value[value.index("[") + 1:value.index("]")]
        return value

    def write(self, knob, value):
        (KSM / knob).write_text(f"{value}\n")

    def stat(self, knob):
        return int(self.read(knob))

    def prepare(self, pages_to_scan, sleep_ms):
        self.write("run", 2)  # unmerge everything -> clean slate (system-wide!)
        if self.has("merge_across_nodes"):
            self.write("merge_across_nodes", 1)
        if self.has("advisor_mode"):
            self.write("advisor_mode", "none")
        if self.has("smart_scan"):
            self.write("smart_scan", 0)
        if self.has("use_zero_pages"):
            self.write("use_zero_pages", 1)
        self.write("pages_to_scan", pages_to_scan)
        self.write("sleep_millisecs", sleep_ms)
        self.write("run", 0)

    def restore(self):
        try:
            self.write("run", 2)
        except OSError:
            pass
        for knob in self.KNOBS:
            if knob in self.saved:
                try:
                    self.write(knob, self.saved[knob])
                except OSError as e:
                    log(f"warning: could not restore ksm/{knob}={self.saved[knob]}: {e}")


def container_anon_kb(name):
    """Anonymous RSS of a running container, in KiB, from its cgroup if we can
    reach it, else from `docker stats`. Anon ~= what KSM scans."""
    # cgroup v2 memory.stat 'anon' is the cleanest signal.
    r = sh(["docker", "inspect", "-f", "{{.Id}}", name])
    cid = r.stdout.strip()
    for base in (f"/sys/fs/cgroup/system.slice/docker-{cid}.scope/memory.stat",
                 f"/sys/fs/cgroup/docker/{cid}/memory.stat"):
        p = Path(base)
        if p.exists():
            for line in p.read_text().splitlines():
                if line.startswith("anon "):
                    return int(line.split()[1]) // 1024
    # Fallback: docker stats MEM USAGE (RSS-ish), parse "123.4MiB / ...".
    r = sh(["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}", name])
    try:
        used = r.stdout.split("/")[0].strip()
        num = float("".join(c for c in used if (c.isdigit() or c == ".")))
        unit = "".join(c for c in used if c.isalpha()).lower()
        mult = {"kib": 1, "mib": 1024, "gib": 1024 * 1024}.get(unit, 1024)
        return int(num * mult)
    except Exception:  # noqa: BLE001
        return -1


def launch_cohort(tasks, args, scratch):
    """docker run each sandbox detached; return list of container names."""
    names = []
    for i, t in enumerate(tasks):
        name = f"az_{args.run_id}_{i}"
        sh(["docker", "rm", "-f", name])
        ready = f"/azout/ready_{i}"
        cmd = [
            "docker", "run", "-d", "--name", name,
            "--memory-swappiness=0",
            "-v", f"{HERE}:/az:ro",
            "-v", f"{scratch}:/azout",
            t["docker_image"],
            "python3", "/az/container_entry.py",
            "--pkg", t.get("pkg", ""),
            "--gt-patch", (f"/azout/{t['gt_patch_name']}" if t.get("gt_patch_name") else ""),
            "--test-cmd", args.test_cmd,
            "--ready-file", ready,
            "--hold-secs", str(args.hold_secs),
        ]
        r = sh(cmd)
        if r.returncode != 0:
            log(f"docker run failed for {name}: {r.stderr.strip()}")
            raise SystemExit(1)
        log(f"launched {name}  image={t['docker_image']}  task={t.get('task_id','?')}")
        names.append(name)
    return names


def wait_ready(n, scratch, timeout):
    log(f"waiting for {n} sandboxes to reach steady memory (timeout {timeout}s)...")
    t0 = time.time()
    while time.time() - t0 < timeout:
        ready = sum(1 for i in range(n) if (Path(scratch) / f"ready_{i}").exists())
        if ready == n:
            log(f"all {n} sandboxes ready after {time.time()-t0:.0f}s")
            return True
        time.sleep(3)
    log(f"TIMEOUT: only {ready}/{n} sandboxes became ready")
    return False


def wait_for_merge(ksm, settle_secs, max_secs):
    """Run ksmd until pages_sharing stops growing (plateau) or max_secs."""
    ksm.write("run", 1)
    log("ksmd scanning; polling pages_sharing...")
    t0 = time.time()
    last, stable_since = -1, time.time()
    while time.time() - t0 < max_secs:
        sharing = ksm.stat("pages_sharing")
        if sharing != last:
            last = sharing
            stable_since = time.time()
            log(f"  pages_sharing={sharing}  (+{sharing*PAGE/2**20:.0f} MiB saved)  "
                f"full_scans={ksm.stat('full_scans')}")
        elif time.time() - stable_since >= settle_secs:
            log(f"pages_sharing plateaued at {sharing}")
            break
        time.sleep(settle_secs / 3)
    ksm.write("run", 0)  # stop scanning; merged pages stay merged
    return time.time() - t0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", required=True,
                    help="tasks manifest from fetch_tasks.py (JSON list)")
    ap.add_argument("--cohort", choices=["identical", "siblings"], default="siblings")
    ap.add_argument("-n", "--num", type=int, default=4, help="cohort size (sibling count)")
    ap.add_argument("--test-cmd", default="", help="pytest args passed to each sandbox")
    ap.add_argument("--hold-secs", type=int, default=900)
    ap.add_argument("--ready-timeout", type=int, default=1200)
    ap.add_argument("--pages-to-scan", type=int, default=4000)
    ap.add_argument("--sleep-ms", type=int, default=20)
    ap.add_argument("--merge-settle", type=int, default=30,
                    help="seconds of no pages_sharing growth to call it a plateau")
    ap.add_argument("--merge-max", type=int, default=1800)
    ap.add_argument("--out", default=str(HERE / "results"))
    args = ap.parse_args()
    args.run_id = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")

    # --- preflight ---
    problems = []
    if not KSM.exists():
        problems.append("no KSM in this kernel (/sys/kernel/mm/ksm missing)")
    if sh(["id", "-u"]).stdout.strip() != "0":
        problems.append("must run as root (writes /sys/kernel/mm/ksm)")
    if sh(["docker", "info"]).returncode != 0:
        problems.append("docker not reachable")
    if not Path("/proc/self/ksm_merging_pages").exists():
        log("note: /proc/<pid>/ksm_merging_pages absent (kernel < 5.19); "
            "per-sandbox merge counts unavailable, system-wide counts still work.")
    if problems:
        for p in problems:
            log("PREFLIGHT: " + p)
        raise SystemExit(1)

    manifest = json.loads(Path(args.manifest).read_text())
    by_repo = {}
    for t in manifest:
        by_repo.setdefault(t.get("repo_name", "?"), []).append(t)

    if args.cohort == "identical":
        tasks = [manifest[0]] * args.num
    else:  # siblings: N tasks sharing the biggest repo
        repo = max(by_repo, key=lambda r: len(by_repo[r]))
        pool = by_repo[repo]
        if len(pool) < args.num:
            log(f"warning: repo {repo} has only {len(pool)} tasks; using all of them")
        tasks = (pool * ((args.num // max(len(pool), 1)) + 1))[:args.num]
        log(f"siblings cohort from repo '{repo}' ({len(pool)} distinct tasks)")

    outdir = Path(args.out) / f"{args.run_id}-{args.cohort}"
    scratch = outdir / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    # Stage GT patches onto the shared volume.
    for t in tasks:
        if t.get("gt_patch"):
            name = f"gt_{t['task_id']}.patch"
            (scratch / name).write_text(t["gt_patch"])
            t["gt_patch_name"] = name

    ksm = Ksm()
    names = []
    try:
        ksm.prepare(args.pages_to_scan, args.sleep_ms)
        names = launch_cohort(tasks, args, scratch)
        if not wait_ready(len(tasks), scratch, args.ready_timeout):
            raise SystemExit("sandboxes did not become ready")

        anon_before = {n: container_anon_kb(n) for n in names}
        total_before = sum(v for v in anon_before.values() if v > 0)
        log(f"cohort anon memory before merge: {total_before/1024:.0f} MiB "
            f"across {len(names)} sandboxes")

        secs = wait_for_merge(ksm, args.merge_settle, args.merge_max)

        pages_shared = ksm.stat("pages_shared")      # unique pages kept
        pages_sharing = ksm.stat("pages_sharing")    # redundant copies reclaimed
        saved_mib = pages_sharing * PAGE / 2**20
        anon_after = {n: container_anon_kb(n) for n in names}
        total_after = sum(v for v in anon_after.values() if v > 0)

        summary = {
            "run_id": args.run_id, "cohort": args.cohort, "num": len(names),
            "merge_secs": round(secs, 1),
            "pages_shared": pages_shared, "pages_sharing": pages_sharing,
            "ksm_saved_mib": round(saved_mib, 1),
            "anon_before_mib": round(total_before / 1024, 1),
            "anon_after_mib": round(total_after / 1024, 1),
            "ksm_saving_pct_of_anon": (round(100 * saved_mib / (total_before / 1024), 2)
                                       if total_before > 0 else None),
            "full_scans": ksm.stat("full_scans"),
        }
        (outdir / "summary.json").write_text(json.dumps(summary, indent=2))
        with (outdir / "runs.csv").open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["container", "task_id", "anon_before_kb", "anon_after_kb"])
            for n, t in zip(names, tasks):
                w.writerow([n, t.get("task_id", "?"), anon_before.get(n, -1),
                            anon_after.get(n, -1)])

        log("=" * 60)
        log(f"KSM merged {pages_sharing} pages = {saved_mib:.0f} MiB reclaimed "
            f"({summary['ksm_saving_pct_of_anon']}% of cohort anon)")
        log(f"unique dedup pages kept (pages_shared): {pages_shared}")
        log(f"results in {outdir}")
        log("=" * 60)
    finally:
        for n in names:
            sh(["docker", "rm", "-f", n])
        ksm.restore()
        log("containers removed, KSM settings restored.")


if __name__ == "__main__":
    main()
