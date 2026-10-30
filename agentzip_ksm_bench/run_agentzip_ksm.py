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
              but divergent private pages, the regime where KSM underperforms.

DIAGNOSTICS: the summary reports the full KSM counter breakdown so a near-zero
result can be explained. KSM tracks mergeable pages as, roughly,
    candidate ~= pages_shared + pages_sharing + pages_unshared + pages_volatile
  - pages_sharing  : redundant copies KSM reclaimed  (the saving)
  - pages_shared   : unique pages kept as merge targets
  - pages_unshared : scanned but found UNIQUE -> content isn't byte-identical
  - pages_volatile : changing too fast to merge
If `candidate` ~ the cohort's anon page count, KSM scanned the memory and it
simply wasn't identical (expected for pointer-heavy heaps / ASLR, and the whole
reason the paper moves beyond KSM). If `candidate` ~ 0, the memory was never
scanned -> plumbing (prctl/ASLR); try --disable-aslr and check docker logs.

KSM's counters are SYSTEM-WIDE, so run this on an otherwise idle node.
"""

import argparse
import csv
import datetime
import json
import subprocess
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
KSM = Path("/sys/kernel/mm/ksm")
ASLR = Path("/proc/sys/kernel/randomize_va_space")
PAGE = 4096


def log(msg):
    print(f"[{datetime.datetime.now():%H:%M:%S}] {msg}", flush=True)


def sh(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, **kw)


class Ksm:
    """Reads/writes /sys/kernel/mm/ksm, restoring original values at exit."""

    KNOBS = ["merge_across_nodes", "smart_scan", "advisor_mode", "pages_to_scan",
             "sleep_millisecs", "use_zero_pages", "run"]
    COUNTERS = ["pages_shared", "pages_sharing", "pages_unshared", "pages_volatile",
                "full_scans"]

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

    def counters(self):
        return {k: self.stat(k) for k in self.COUNTERS if self.has(k)}

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


class Aslr:
    """Optionally disable ASLR for the run (global knob; affects new execs,
    including in containers). Many more heap/stack pages become byte-identical
    across identical processes with ASLR off -- useful to separate 'not
    identical because of ASLR' from 'not identical at all'."""

    def __init__(self, disable):
        self.disable = disable and ASLR.exists()
        self.saved = ASLR.read_text().strip() if self.disable else None

    def apply(self):
        if self.disable:
            ASLR.write_text("0\n")
            log("ASLR disabled for this run (randomize_va_space=0)")

    def restore(self):
        if self.disable and self.saved is not None:
            try:
                ASLR.write_text(self.saved + "\n")
                log(f"ASLR restored (randomize_va_space={self.saved})")
            except OSError:
                pass


def container_host_pid(name):
    r = sh(["docker", "inspect", "-f", "{{.State.Pid}}", name])
    try:
        return int(r.stdout.strip())
    except ValueError:
        return -1


def proc_merging_pages(hostpid):
    """Per-process pages KSM merged (Linux 5.19+). -1 if unavailable."""
    p = Path(f"/proc/{hostpid}/ksm_merging_pages")
    try:
        return int(p.read_text().strip())
    except (OSError, ValueError):
        return -1


def container_anon_kb(name):
    """Anonymous RSS of a running container, in KiB (cgroup v2 memory.stat
    'anon', else `docker stats`). Anon ~= what KSM scans."""
    cid = sh(["docker", "inspect", "-f", "{{.Id}}", name]).stdout.strip()
    for base in (f"/sys/fs/cgroup/system.slice/docker-{cid}.scope/memory.stat",
                 f"/sys/fs/cgroup/docker/{cid}/memory.stat"):
        p = Path(base)
        if p.exists():
            for line in p.read_text().splitlines():
                if line.startswith("anon "):
                    return int(line.split()[1]) // 1024
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
    names = []
    for i, t in enumerate(tasks):
        name = f"az_{args.run_id}_{i}"
        sh(["docker", "rm", "-f", name])
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
            "--ready-file", f"/azout/ready_{i}",
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
    ready = 0
    while time.time() - t0 < timeout:
        ready = sum(1 for i in range(n) if (Path(scratch) / f"ready_{i}").exists())
        if ready == n:
            log(f"all {n} sandboxes ready after {time.time()-t0:.0f}s")
            return True
        time.sleep(3)
    log(f"TIMEOUT: only {ready}/{n} sandboxes became ready")
    return False


def wait_for_merge(ksm, settle_secs, max_secs):
    ksm.write("run", 1)
    log("ksmd scanning; polling pages_sharing...")
    t0 = time.time()
    last, stable_since = -1, time.time()
    while time.time() - t0 < max_secs:
        sharing = ksm.stat("pages_sharing")
        if sharing != last:
            last = sharing
            stable_since = time.time()
            log(f"  pages_sharing={sharing} (+{sharing*PAGE/2**20:.0f} MiB)  "
                f"unshared={ksm.stat('pages_unshared')}  full_scans={ksm.stat('full_scans')}")
        elif time.time() - stable_since >= settle_secs:
            log(f"pages_sharing plateaued at {sharing}")
            break
        time.sleep(settle_secs / 3)
    ksm.write("run", 0)
    return time.time() - t0


def diagnose(c, anon_pages):
    """Human-readable read of the counter breakdown."""
    candidate = (c.get("pages_shared", 0) + c.get("pages_sharing", 0)
                 + c.get("pages_unshared", 0) + c.get("pages_volatile", 0))
    if anon_pages > 0 and candidate < 0.05 * anon_pages:
        return (f"candidate pages scanned ({candidate}) << cohort anon pages "
                f"(~{anon_pages}): KSM barely SAW the memory -> likely plumbing "
                f"(PR_SET_MEMORY_MERGE not applied, or memory in exec'd children). "
                f"Check docker logs for the prctl line; try --disable-aslr.")
    if candidate > 0 and c.get("pages_sharing", 0) < 0.05 * candidate:
        return (f"KSM scanned ~{candidate} candidate pages but merged only "
                f"{c.get('pages_sharing',0)} ({c.get('pages_unshared',0)} were unique): "
                f"the anon pages are NOT byte-identical. Expected for pointer-heavy "
                f"heaps/ASLR; this is exactly why the paper moves past KSM to "
                f"approximate (delta/dictionary) compression. Try --disable-aslr to "
                f"see how much is ASLR vs genuinely divergent.")
    return "KSM found and merged a substantial share of candidate pages."


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--cohort", choices=["identical", "siblings"], default="siblings")
    ap.add_argument("-n", "--num", type=int, default=4)
    ap.add_argument("--test-cmd", default="")
    ap.add_argument("--hold-secs", type=int, default=900)
    ap.add_argument("--ready-timeout", type=int, default=1200)
    ap.add_argument("--pages-to-scan", type=int, default=4000)
    ap.add_argument("--sleep-ms", type=int, default=20)
    ap.add_argument("--merge-settle", type=int, default=30)
    ap.add_argument("--merge-max", type=int, default=1800)
    ap.add_argument("--disable-aslr", action="store_true",
                    help="set randomize_va_space=0 during the run (restored after)")
    ap.add_argument("--keep-containers", action="store_true",
                    help="do not remove containers at the end (for manual inspection)")
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
    per_proc = Path("/proc/self/ksm_merging_pages").exists()
    if not per_proc:
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
    else:
        repo = max(by_repo, key=lambda r: len(by_repo[r]))
        pool = by_repo[repo]
        if len(pool) < args.num:
            log(f"warning: repo {repo} has only {len(pool)} tasks; using all of them")
        tasks = (pool * ((args.num // max(len(pool), 1)) + 1))[:args.num]
        log(f"siblings cohort from repo '{repo}' ({len(pool)} distinct tasks)")

    outdir = Path(args.out) / f"{args.run_id}-{args.cohort}"
    scratch = outdir / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    for t in tasks:
        if t.get("gt_patch"):
            name = f"gt_{t['task_id']}.patch"
            (scratch / name).write_text(t["gt_patch"])
            t["gt_patch_name"] = name

    ksm = Ksm()
    aslr = Aslr(args.disable_aslr)
    names = []
    try:
        aslr.apply()
        ksm.prepare(args.pages_to_scan, args.sleep_ms)
        names = launch_cohort(tasks, args, scratch)
        if not wait_ready(len(tasks), scratch, args.ready_timeout):
            raise SystemExit("sandboxes did not become ready")

        anon_before = {n: container_anon_kb(n) for n in names}
        total_before = sum(v for v in anon_before.values() if v > 0)
        anon_pages = (total_before * 1024) // PAGE
        log(f"cohort anon memory before merge: {total_before/1024:.0f} MiB "
            f"(~{anon_pages} pages) across {len(names)} sandboxes")

        secs = wait_for_merge(ksm, args.merge_settle, args.merge_max)

        c = ksm.counters()
        saved_mib = c.get("pages_sharing", 0) * PAGE / 2**20
        anon_after = {n: container_anon_kb(n) for n in names}
        total_after = sum(v for v in anon_after.values() if v > 0)
        merging = {n: proc_merging_pages(container_host_pid(n)) for n in names} if per_proc else {}

        # Capture container stdout (shows the prctl line) before teardown.
        logs_dir = outdir / "container_logs"
        logs_dir.mkdir(exist_ok=True)
        for n in names:
            (logs_dir / f"{n}.log").write_text(sh(["docker", "logs", n]).stdout)

        summary = {
            "run_id": args.run_id, "cohort": args.cohort, "num": len(names),
            "disable_aslr": bool(args.disable_aslr),
            "merge_secs": round(secs, 1),
            "ksm_counters": c,
            "candidate_pages": (c.get("pages_shared", 0) + c.get("pages_sharing", 0)
                                + c.get("pages_unshared", 0) + c.get("pages_volatile", 0)),
            "cohort_anon_pages": anon_pages,
            "ksm_saved_mib": round(saved_mib, 1),
            "anon_before_mib": round(total_before / 1024, 1),
            "anon_after_mib": round(total_after / 1024, 1),
            "ksm_saving_pct_of_anon": (round(100 * saved_mib / (total_before / 1024), 2)
                                       if total_before > 0 else None),
            "per_container_merging_pages": merging,
            "diagnosis": diagnose(c, anon_pages),
        }
        (outdir / "summary.json").write_text(json.dumps(summary, indent=2))
        with (outdir / "runs.csv").open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["container", "task_id", "anon_before_kb", "anon_after_kb", "merging_pages"])
            for n, t in zip(names, tasks):
                w.writerow([n, t.get("task_id", "?"), anon_before.get(n, -1),
                            anon_after.get(n, -1), merging.get(n, -1)])

        log("=" * 66)
        log(f"KSM merged {c.get('pages_sharing',0)} pages = {saved_mib:.1f} MiB "
            f"({summary['ksm_saving_pct_of_anon']}% of cohort anon)")
        log(f"counters: {c}")
        log(f"candidate pages scanned ~ {summary['candidate_pages']} "
            f"vs cohort anon ~ {anon_pages} pages")
        if merging:
            log(f"per-container merging_pages: {merging}")
        log(f"DIAGNOSIS: {summary['diagnosis']}")
        log(f"results in {outdir}")
        log("=" * 66)
    finally:
        if not args.keep_containers:
            for n in names:
                sh(["docker", "rm", "-f", n])
            log("containers removed.")
        else:
            log(f"containers kept: {names}")
        ksm.restore()
        aslr.restore()
        log("KSM settings restored.")


if __name__ == "__main__":
    main()
