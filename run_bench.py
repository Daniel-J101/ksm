#!/usr/bin/env python3
"""Driver for the KSM cross-node penalty benchmark. See README.md.

Each *session* launches two ksm_bench instances (A on node 0, B on node 1),
fills identical buffers, optionally lets KSM merge them, records where every
page ended up, and then runs a list of timed measurements against them.
"""

import argparse
import csv
import datetime
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import analyze

HERE = Path(__file__).resolve().parent
BIN = HERE / "ksm_bench"
KSM = Path("/sys/kernel/mm/ksm")
NUMA_BALANCING = Path("/proc/sys/kernel/numa_balancing")

# Placement of each instance as (cpu node, memory node).
CONFIGS = {
    # Baseline: both local, no merging.
    "ksm_off":        dict(ksm=False, merge_across_nodes=None, A=(0, 0), B=(1, 1)),
    # Stock KSM: one copy per page, wherever ksmd happens to keep it.
    "ksm_merge_all":  dict(ksm=True,  merge_across_nodes=1,    A=(0, 0), B=(1, 1)),
    # No KSM, but B's memory is forced onto node 0: the pure remote penalty.
    "remote_control": dict(ksm=False, merge_across_nodes=None, A=(0, 0), B=(1, 0)),
    # KSM that only merges within a node: no remote penalty, no cross-node saving.
    "ksm_per_node":   dict(ksm=True,  merge_across_nodes=0,    A=(0, 0), B=(1, 1)),
}
MODES = {"solo_a": ["A"], "solo_b": ["B"], "concurrent": ["A", "B"]}

RUN_FIELDS = [
    "session", "plan", "config", "size", "shared_frac", "pattern", "mode", "write_frac",
    "rep", "instance", "cpu", "cpu_node", "mem_node", "pages", "remote_frac",
    "remote_frac_after", "merging_before", "merging_after", "ops", "secs", "ns_per_op",
    "gbps", "writes",
]
SESSION_FIELDS = [
    "session", "plan", "config", "size", "shared_frac", "fill_secs_A", "fill_secs_B",
    "shared_pages", "merge_secs", "merge_full_scans", "pages_shared", "pages_sharing",
    "A_n0", "A_n1", "A_merging", "A_remote_frac", "B_n0", "B_n1", "B_merging", "B_remote_frac",
]


def log(msg):
    print(f"[{datetime.datetime.now():%H:%M:%S}] {msg}", flush=True)


def parse_size(s):
    s = s.strip()
    mult = {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30}.get(s[-1].upper(), 1)
    return int(float(s[:-1] if mult > 1 else s) * mult)


def parse_cpulist(text):
    cpus = []
    for part in text.strip().split(","):
        lo, _, hi = part.partition("-")
        cpus.extend(range(int(lo), int(hi or lo) + 1))
    return cpus


def node_free_bytes(node):
    for line in Path(f"/sys/devices/system/node/node{node}/meminfo").read_text().splitlines():
        if "MemFree:" in line:
            return int(line.split()[-2]) * 1024
    raise RuntimeError(f"no MemFree for node {node}")


def sh(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout
    except OSError as e:
        return str(e)


class Ksm:
    """Reads and writes /sys/kernel/mm/ksm, restoring the original values at exit."""

    KNOBS = ["merge_across_nodes", "smart_scan", "advisor_mode", "pages_to_scan",
             "sleep_millisecs", "run"]

    def __init__(self):
        self.saved = {k: self.read(k) for k in self.KNOBS if self.has(k)}

    def has(self, knob):
        return (KSM / knob).exists()

    def read(self, knob):
        value = (KSM / knob).read_text().strip()
        if "[" in value:  # e.g. advisor_mode prints "[none] scan-time"
            value = value[value.index("[") + 1:value.index("]")]
        return value

    def write(self, knob, value):
        (KSM / knob).write_text(f"{value}\n")

    def stat(self, knob):
        return int(self.read(knob))

    def prepare(self, merge_across_nodes, pages_to_scan, sleep_ms):
        # run=2 unmerges every KSM page on the system. That is required
        # before merge_across_nodes can change, and gives each session a
        # clean slate. ksmd stays stopped until the buffers are filled.
        self.write("run", 2)
        if merge_across_nodes is not None:
            self.write("merge_across_nodes", merge_across_nodes)
        if self.has("advisor_mode"):
            self.write("advisor_mode", "none")  # fixed scan rate, no auto-tuning
        if self.has("smart_scan"):
            self.write("smart_scan", 0)         # don't skip pages that failed to merge
        self.write("pages_to_scan", pages_to_scan)
        self.write("sleep_millisecs", sleep_ms)
        self.write("run", 0)

    def restore(self):
        self.write("run", 2)
        for knob in self.KNOBS:
            if knob in self.saved:
                try:
                    self.write(knob, self.saved[knob])
                except OSError as e:
                    log(f"warning: could not restore ksm/{knob}={self.saved[knob]}: {e}")


class Worker:
    """One ksm_bench process pinned with numactl, driven over its stdin/stdout."""

    def __init__(self, name, cpu, mem_node, size, seed, shared_frac, logfile):
        self.name = name
        cmd = ["numactl", f"--physcpubind={cpu}", f"--membind={mem_node}", str(BIN),
               f"--size={size}", f"--seed={seed}", f"--id={0 if name == 'A' else 1}",
               f"--shared-frac={shared_frac}"]
        logfile.write(f"$ {' '.join(cmd)}\n")
        logfile.flush()
        # numactl execs the binary, so this pid is the benchmark itself.
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=logfile, text=True, bufsize=1)

    def send(self, line):
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()

    def recv(self):
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError(f"worker {self.name} exited (status {self.proc.poll()})")
        parts = line.split()
        if parts[0] != "OK":
            raise RuntimeError(f"worker {self.name}: {line.strip()}")
        return dict(kv.split("=", 1) for kv in parts[1:])

    def call(self, line):
        self.send(line)
        return self.recv()

    def merging(self):
        """Pages of this process currently mapped to a KSM page."""
        return int(Path(f"/proc/{self.proc.pid}/ksm_merging_pages").read_text())

    def close(self):
        try:
            self.send("quit")
            self.proc.wait(timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            self.proc.kill()
            self.proc.wait()


class Session:
    def __init__(self, plan, config, size, shared_frac, measurements):
        self.plan = plan
        self.config = config
        self.size = size
        self.shared_frac = shared_frac
        # (pattern, mode, write_frac, rep), run in this order.
        self.measurements = measurements


def build_sessions(args):
    sizes = [parse_size(s) for s in args.sizes.split(",")]
    big = max(sizes)
    patterns = args.patterns.split(",")
    modes = args.modes.split(",")
    sessions = []
    plans = ["core", "write", "shared"] if args.plan == "all" else [args.plan]

    for plan in plans:
        if plan == "quick":
            meas = [("chase", m, 0.0, 0) for m in ("solo_a", "solo_b")]
            sessions += [Session("quick", c, parse_size("1G"), 1.0, meas)
                         for c in ("ksm_off", "ksm_merge_all")]
        elif plan == "core":
            meas = [(p, m, 0.0, r) for p in patterns for m in modes for r in range(args.reps)]
            sessions += [Session("core", c, size, 1.0, meas)
                         for size in sizes for c in args.configs.split(",")]
        elif plan == "write":
            fracs = [float(w) for w in args.write_fracs.split(",")]
            # Without KSM, writes change nothing about placement: one session covers all.
            sessions.append(Session("write", "ksm_off", big, 1.0,
                                    [("chase", "concurrent", w, r)
                                     for w in fracs for r in range(args.write_reps)]))
            # With KSM every write run unmerges pages, so each one gets a fresh merge.
            sessions += [Session("write", "ksm_merge_all", big, 1.0, [("chase", "concurrent", w, r)])
                         for w in fracs for r in range(args.write_reps)]
        elif plan == "shared":
            meas = [("chase", m, 0.0, r) for m in ("solo_a", "solo_b") for r in range(args.reps)]
            sessions.append(Session("shared", "ksm_off", big, 1.0, meas))
            sessions += [Session("shared", "ksm_merge_all", big, float(f), meas)
                         for f in args.shared_fracs.split(",")]
    return sessions


def wait_for_merge(workers, expected, ksm, args, logf):
    """Run ksmd until both instances have merged `expected` pages, or merging plateaus."""
    ksm.write("run", 1)
    t0 = time.time()
    scans0 = last_scans = ksm.stat("full_scans")
    last_m = None
    npages = max(expected, 1)
    while True:
        time.sleep(2)
        m = min(w.merging() for w in workers.values())
        scans = ksm.stat("full_scans")
        elapsed = time.time() - t0
        logf.write(f"merge t={elapsed:.0f}s full_scans={scans - scans0} merging={m}/{expected}\n")
        logf.flush()
        if expected and m >= 0.995 * expected:
            break
        if scans != last_scans:
            # KSM needs two full passes before anything merges, so wait for three.
            if last_m is not None and scans - scans0 >= 3 and m - last_m <= max(1, 0.001 * npages):
                break
            last_m, last_scans = m, scans
        if elapsed > args.merge_timeout:
            log(f"warning: merge timed out after {elapsed:.0f}s at {m}/{expected} pages")
            break
    return time.time() - t0, ksm.stat("full_scans") - scans0


def remote_frac(stat, cpu_node):
    pages = int(stat["pages"])
    return (pages - int(stat.get(f"n{cpu_node}", 0))) / pages


def run_session(sid, s, args, ksm, cpus, out, run_writer, session_writer):
    cfg = CONFIGS[s.config]
    need = {}
    for name in ("A", "B"):
        need[cfg[name][1]] = need.get(cfg[name][1], 0) + s.size * 1.05 + (256 << 20)
    for node, nbytes in need.items():
        if node_free_bytes(node) < nbytes:
            raise RuntimeError(f"node {node} has {node_free_bytes(node) >> 20} MiB free, "
                               f"session needs {int(nbytes) >> 20} MiB")

    log(f"session {sid}: {s.plan} {s.config} size={analyze.fmt_size(s.size)} "
        f"shared={s.shared_frac} ({len(s.measurements)} measurements)")
    logf = open(out / "logs" / f"session_{sid:03d}_{s.config}.log", "w")
    ksm.prepare(cfg["merge_across_nodes"] if cfg["ksm"] else None,
                args.pages_to_scan, args.sleep_ms)
    workers = {}
    try:
        fill = {}
        # A fills first, as VM-0 started first in the paper.
        for name in ("A", "B"):
            cpu_node, mem_node = cfg[name]
            workers[name] = Worker(name, cpus[cpu_node], mem_node, s.size, args.seed,
                                   s.shared_frac, logf)
            fill[name] = workers[name].call("fill")
            logf.write(f"fill {name}: {fill[name]}\n")
            if int(fill[name]["node"]) != cpu_node:
                raise RuntimeError(f"worker {name} runs on node {fill[name]['node']}, "
                                   f"expected {cpu_node}")
        expected = int(fill["A"]["shared_pages"])

        merge_secs, merge_scans = 0.0, 0
        if cfg["ksm"]:
            merge_secs, merge_scans = wait_for_merge(workers, expected, ksm, args, logf)
            log(f"  merged {min(w.merging() for w in workers.values())}/{expected} pages "
                f"in {merge_secs:.0f}s ({merge_scans} full scans)")
            if not args.keep_ksmd:
                ksm.write("run", 0)  # stop scanning; merged pages stay merged

        placement = {n: w.call("stat") for n, w in workers.items()}
        for n, w in workers.items():
            logf.write(f"stat {n}: {placement[n]}\n")
            logf.write(sh(f"numastat -p {w.proc.pid}"))
        logf.flush()
        rfrac = {n: remote_frac(placement[n], cfg[n][0]) for n in workers}
        log("  placement: " + ", ".join(
            f"{n} {100 * rfrac[n]:.1f}% remote" for n in workers))

        session_writer.writerow({
            "session": sid, "plan": s.plan, "config": s.config, "size": s.size,
            "shared_frac": s.shared_frac, "fill_secs_A": fill["A"]["secs"],
            "fill_secs_B": fill["B"]["secs"], "shared_pages": expected,
            "merge_secs": f"{merge_secs:.1f}", "merge_full_scans": merge_scans,
            "pages_shared": ksm.stat("pages_shared"), "pages_sharing": ksm.stat("pages_sharing"),
            **{f"{n}_{k}": placement[n].get(k, "") for n in ("A", "B")
               for k in ("n0", "n1", "merging")},
            **{f"{n}_remote_frac": f"{rfrac[n]:.4f}" for n in ("A", "B")},
        })

        for pattern in dict.fromkeys(m[0] for m in s.measurements):
            if args.warmup > 0:
                for w in workers.values():
                    w.send(f"run {pattern} {args.warmup} 0")
                for w in workers.values():
                    w.recv()

        for pattern, mode, wfrac, rep in s.measurements:
            active = MODES[mode]
            before = {n: workers[n].merging() for n in active}
            # Concurrent runs start within a millisecond of each other and are
            # time-bounded, so they overlap for the whole measurement.
            for n in active:
                workers[n].send(f"run {pattern} {args.duration} {wfrac}")
            results = {n: workers[n].recv() for n in active}
            after_frac = dict(rfrac)
            if wfrac > 0:
                for n in active:
                    after_frac[n] = remote_frac(workers[n].call("stat"), cfg[n][0])
            for n in active:
                r = results[n]
                run_writer.writerow({
                    "session": sid, "plan": s.plan, "config": s.config, "size": s.size,
                    "shared_frac": s.shared_frac, "pattern": pattern, "mode": mode,
                    "write_frac": wfrac, "rep": rep, "instance": n, "cpu": cpus[cfg[n][0]],
                    "cpu_node": cfg[n][0], "mem_node": cfg[n][1],
                    "pages": placement[n]["pages"], "remote_frac": f"{rfrac[n]:.4f}",
                    "remote_frac_after": f"{after_frac[n]:.4f}",
                    "merging_before": before[n], "merging_after": workers[n].merging(),
                    "ops": r["ops"], "secs": r["secs"], "ns_per_op": r["ns_per_op"],
                    "gbps": r["gbps"], "writes": r["writes"],
                })
            logf.write(f"run {pattern} {mode} w={wfrac} rep={rep}: {results}\n")
            logf.flush()
            summary = ", ".join(f"{n} {float(results[n]['ns_per_op']):.1f} ns/op" for n in active)
            log(f"  {pattern:5s} {mode:10s} w={wfrac:<5} rep={rep}: {summary}")
    finally:
        for w in workers.values():
            w.close()
        logf.close()


def preflight(args):
    problems = []
    if os.geteuid() != 0:
        problems.append("must run as root (writes /sys/kernel/mm/ksm)")
    if not BIN.exists():
        problems.append(f"{BIN} not found: run `make` first")
    for tool in ("numactl", "numastat"):
        if not shutil.which(tool):
            problems.append(f"{tool} not installed (apt install numactl)")
    if not Path("/sys/devices/system/node/node1").exists():
        problems.append("need at least two NUMA nodes")
    if not (KSM / "run").exists():
        problems.append("kernel has no KSM (/sys/kernel/mm/ksm missing)")
    if not Path("/proc/self/ksm_merging_pages").exists():
        problems.append("/proc/<pid>/ksm_merging_pages missing (needs Linux 5.19+)")
    if problems:
        sys.exit("cannot run:\n  " + "\n  ".join(problems))
    swaps = Path("/proc/swaps").read_text().strip().splitlines()
    if len(swaps) > 1:
        log("warning: swap is enabled; swapped-out pages would distort placement (swapoff -a)")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("plan", choices=["quick", "core", "write", "shared", "all"])
    ap.add_argument("--sizes", default="16M,4G", help="working-set sizes (default 16M,4G)")
    ap.add_argument("--configs", default=",".join(CONFIGS), help="configs for the core plan")
    ap.add_argument("--patterns", default="chase,seq")
    ap.add_argument("--modes", default="solo_a,solo_b,concurrent")
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--duration", type=float, default=10, help="seconds per timed run")
    ap.add_argument("--warmup", type=float, default=2, help="untimed seconds per pattern")
    ap.add_argument("--write-fracs", default="0,0.01,0.1")
    ap.add_argument("--write-reps", type=int, default=2)
    ap.add_argument("--shared-fracs", default="0,0.25,0.5,0.75,1")
    ap.add_argument("--pages-to-scan", type=int, default=10000)
    ap.add_argument("--sleep-ms", type=int, default=10)
    ap.add_argument("--paper-rate", action="store_true",
                    help="scan at the paper's 1000 pages / 100 ms (slower merges)")
    ap.add_argument("--merge-timeout", type=float, default=1800)
    ap.add_argument("--keep-ksmd", action="store_true",
                    help="leave ksmd scanning during timed runs (default: stop it after merging)")
    ap.add_argument("--keep-numa-balancing", action="store_true",
                    help="leave AutoNUMA as is (default: disable it for the run)")
    ap.add_argument("--cpus", help="CPU for node 0 and node 1, e.g. 2,30 "
                                   "(default: second CPU of each node)")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out", type=Path, help="results directory (default results/<time>-<plan>)")
    args = ap.parse_args()
    if args.paper_rate:
        args.pages_to_scan, args.sleep_ms = 1000, 100
    for c in args.configs.split(","):
        if c not in CONFIGS:
            ap.error(f"unknown config {c}; choose from {', '.join(CONFIGS)}")
    for m in args.modes.split(","):
        if m not in MODES:
            ap.error(f"unknown mode {m}; choose from {', '.join(MODES)}")

    preflight(args)
    if args.cpus:
        cpus = dict(enumerate(int(c) for c in args.cpus.split(",")))
    else:
        cpus = {}
        for n in (0, 1):
            node_list = parse_cpulist(Path(f"/sys/devices/system/node/node{n}/cpulist").read_text())
            cpus[n] = node_list[1 if len(node_list) > 1 else 0]

    sessions = build_sessions(args)
    timed = sum(len(s.measurements) for s in sessions) * args.duration
    log(f"{len(sessions)} sessions, ~{timed / 60:.0f} min of timed runs plus fills and merges; "
        f"CPUs: node0={cpus[0]} node1={cpus[1]}")

    out = args.out or HERE / "results" / f"{datetime.datetime.now():%Y%m%d-%H%M%S}-{args.plan}"
    (out / "logs").mkdir(parents=True, exist_ok=True)

    ksm = Ksm()
    numa_balancing = NUMA_BALANCING.read_text().strip() if NUMA_BALANCING.exists() else None
    (out / "meta.json").write_text(json.dumps({
        "argv": sys.argv, "args": {k: str(v) for k, v in vars(args).items()}, "cpus": cpus,
        "uname": sh("uname -a").strip(), "ksm_saved": ksm.saved,
        "numa_balancing": numa_balancing, "lscpu": sh("lscpu"), "numactl_H": sh("numactl -H"),
        "governor": sh("cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor | sort | uniq -c"),
        "thp": sh("cat /sys/kernel/mm/transparent_hugepage/enabled"),
    }, indent=2))

    def interrupt(*_):
        raise KeyboardInterrupt

    # SIGTERM / SIGHUP take the same cleanup path as Ctrl-C.
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, interrupt)

    try:
        if numa_balancing is not None and not args.keep_numa_balancing:
            # AutoNUMA could migrate pages mid-run and undo the placement being measured.
            NUMA_BALANCING.write_text("0\n")
        with open(out / "runs.csv", "w", newline="") as rf, \
             open(out / "sessions.csv", "w", newline="") as sf:
            run_writer = csv.DictWriter(rf, RUN_FIELDS)
            session_writer = csv.DictWriter(sf, SESSION_FIELDS)
            run_writer.writeheader()
            session_writer.writeheader()
            for sid, s in enumerate(sessions):
                run_session(sid, s, args, ksm, cpus, out, run_writer, session_writer)
                rf.flush()
                sf.flush()
    except KeyboardInterrupt:
        log("interrupted; partial results kept")
    finally:
        log("restoring KSM settings (unmerging everything)")
        ksm.restore()
        if numa_balancing is not None:
            NUMA_BALANCING.write_text(numa_balancing + "\n")

    log(f"results in {out}")
    analyze.summarize(out)


if __name__ == "__main__":
    main()
