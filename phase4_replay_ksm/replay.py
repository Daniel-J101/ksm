#!/usr/bin/env python3
"""Phase 4: replay a recorded cohort into Zeroboot sandboxes and measure memory.

One run = one cohort (e.g. the 16 Rollout traces of a task) x one config:
  nocomp : KSM off (ksmd stopped, everything unmerged)
  ksm    : KSM on with the knobs from tools/ksm_ctl.sh (env overrides)
Fork mappings are MADV_MERGEABLE in BOTH configs (ZEROBOOT_KSM_MERGEABLE=1), so
the only difference between them is whether ksmd runs.

Replay (AgentZip Section 5.3): all trajectories start together, one sandbox each,
forked from the task template. For every recorded agent command: wait until
`think_time_ms_before` after the previous command finished (the sandbox is
idle, as during an LLM call), then run it through ZerobootRuntime.run with the
recorded timeout/args/workdir. A sandbox is deleted when its trajectory ends.
Per command we check the exit code (and output hash) against the recording.

Memory, sampled every --interval s from the serve process (every sandbox lives
in it) and KSM's counters:
  M(t) = sandbox-owned physical memory
       = (Anonymous_kB(t) - Anonymous_kB before any sandbox)   CoW'd private pages
         - pages_sharing(t) * 4 kB                             pages KSM deduplicated
  (the 1b gate showed the serve Pss drop equals pages_sharing). Saving
  S = 1 - M_avg(ksm) / M_avg(nocomp), averaged over the replay's wall time.

  sudo python3 replay.py --template-dir $TEMPLATES_DIR/aiohttp/f0d74880deec \\
       --cohort ../traces/aiohttp/f0d74880deec/rollout --config ksm --rep 0
Writes results/<repo>-<commit12>-<cohort>-<config>-r<rep>-<utc>.json
"""

import argparse
import glob
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
KSM_ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(KSM_ROOT, "phase2_sandbox"))
from zbsandbox import ZerobootRuntime  # noqa: E402

KSMD = "/sys/kernel/mm/ksm"
TICK = os.sysconf("SC_CLK_TCK")


def sh(*cmd, env=None):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, env={**os.environ, **(env or {})})


def read_kv(path):
    out = {}
    try:
        for line in open(path):
            k, _, v = line.partition(":")
            parts = v.split()
            if parts and parts[0].isdigit():
                out[k.strip()] = int(parts[0])
    except OSError:
        pass
    return out


def ksm_counters():
    out = {}
    for f in ("pages_shared", "pages_sharing", "pages_unshared", "pages_volatile", "full_scans", "run"):
        try:
            out[f] = int(open(os.path.join(KSMD, f)).read())
        except (OSError, ValueError):
            pass
    return out


def ksmd_cpu_s():
    try:
        pid = subprocess.check_output(["pgrep", "-x", "ksmd"], text=True).split()[0]
        f = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()
        return (int(f[11]) + int(f[12])) / TICK          # utime + stime
    except Exception:  # noqa: BLE001
        return None


def sample(pid, anon0):
    r = read_kv(f"/proc/{pid}/smaps_rollup")
    k = ksm_counters()
    m = read_kv("/proc/meminfo")
    priv = r.get("Anonymous", 0) - anon0
    saved = k.get("pages_sharing", 0) * 4
    return {"Anonymous_kB": r.get("Anonymous", 0), "Rss_kB": r.get("Rss", 0), "Pss_kB": r.get("Pss", 0),
            "Private_Dirty_kB": r.get("Private_Dirty", 0), "ksm_pages_shared": k.get("pages_shared"),
            "ksm_pages_sharing": k.get("pages_sharing"), "ksm_full_scans": k.get("full_scans"),
            "MemAvailable_kB": m.get("MemAvailable"), "private_kB": priv, "ksm_saved_kB": saved,
            "M_kB": priv - saved}


def replay_one(trace, idx, t0, out):
    meta, steps = trace["meta"], trace["replay"]
    rec = {"k": meta["k"], "temperature": meta["temperature"], "role": meta.get("role"),
           "n_cmds": len(steps), "code_match": 0, "sha_match": 0, "mismatches": [], "exec_ms": []}
    rt = ZerobootRuntime(seed=10_000 + idx)
    rec["t_create"] = round(time.time() - t0, 3)
    prev_end = time.time()
    try:
        for i, s in enumerate(steps):
            delay = prev_end + s["think_time_ms_before"] / 1000 - time.time()
            if delay > 0:
                time.sleep(delay)
            ts = time.time()
            o, c = rt.run(s["code"], timeout=s["timeout"], args=s["args"] or "", workdir=s["workdir"])
            prev_end = time.time()
            rec["exec_ms"].append(round((prev_end - ts) * 1000, 1))
            if c == s["rec_error_code"]:
                rec["code_match"] += 1
            else:
                rec["mismatches"].append({"i": i, "rec": s["rec_error_code"], "got": c, "code": s["code"][:200],
                                          "out": o[:300]})
            if hashlib.sha256(o.encode("utf-8", "replace")).hexdigest()[:16] == s["rec_output_sha"]:
                rec["sha_match"] += 1
            else:   # keep enough to diagnose against the full recording
                rec.setdefault("sha_mismatches", []).append(
                    {"i": i, "rec_len": s["rec_output_len"], "got_len": len(o), "got": o[:4000]})
    except Exception as e:  # noqa: BLE001
        rec["error"] = repr(e)
    finally:
        rt.close()
    rec["t_end"] = round(time.time() - t0, 3)
    out[idx] = rec


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--template-dir", required=True)
    ap.add_argument("--cohort", required=True, help="directory of compact traces (<k>.json)")
    ap.add_argument("--config", choices=("nocomp", "ksm"), required=True)
    ap.add_argument("--rep", type=int, default=0)
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--numa-node", default="0")
    ap.add_argument("--settle", type=float, default=5.0, help="seconds of sampling after the last sandbox ends")
    ap.add_argument("--tag", default="", help="extra label, e.g. for knob sweeps")
    a = ap.parse_args()
    if os.geteuid() != 0:
        sys.exit("run as root: controls KSM, starts serve, reads its smaps")

    T = os.path.abspath(a.template_dir)
    man = json.load(open(os.path.join(T, "manifest.json")))
    traces = [json.load(open(f)) for f in sorted(glob.glob(os.path.join(a.cohort, "*.json")))]
    if not traces:
        sys.exit(f"no traces in {a.cohort}")
    tm = {t["meta"]["template_manifest_sha"] for t in traces}
    cohort = traces[0]["meta"]["cohort"]

    serve = os.path.join(KSM_ROOT, "phase1_zeroboot_cow", "30_serve.sh")
    ksm_ctl = os.path.join(KSM_ROOT, "tools", "ksm_ctl.sh")
    senv = {"WORKDIR": T, "NUMA_NODE": a.numa_node}
    sh("bash", serve, "stop", env=senv)
    time.sleep(1)
    sh("bash", ksm_ctl, "on" if a.config == "ksm" else "off")
    knobs = json.loads(sh("bash", ksm_ctl, "status").stdout)
    sh("bash", serve, env={**senv, "BACKGROUND": "1", "ZEROBOOT_KSM_MERGEABLE": "1"})
    for _ in range(600):
        try:
            urllib.request.urlopen("http://127.0.0.1:8080/v1/health", timeout=2)
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    pid = int(open(os.path.join(T, "serve.pid")).read())
    time.sleep(2)
    anon0 = read_kv(f"/proc/{pid}/smaps_rollup").get("Anonymous", 0)
    cpu0 = ksmd_cpu_s()
    print(f"{cohort} config={a.config} rep={a.rep}: {len(traces)} trajectories, serve pid {pid}, "
          f"baseline Anonymous {anon0/1024:.1f} MiB", flush=True)

    series, stop = [], threading.Event()
    t0 = time.time()

    def sampler():
        while not stop.is_set():
            series.append({"t": round(time.time() - t0, 2), **sample(pid, anon0)})
            stop.wait(a.interval)
    th = threading.Thread(target=sampler, daemon=True)
    th.start()

    results = {}
    workers = [threading.Thread(target=replay_one, args=(tr, i, t0, results)) for i, tr in enumerate(traces)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    wall = time.time() - t0
    time.sleep(a.settle)
    stop.set()
    th.join()
    cpu1 = ksmd_cpu_s()
    sh("bash", serve, "stop", env=senv)
    sh("bash", ksm_ctl, "off")

    live = [s for s in series if s["t"] <= wall]
    m_avg = sum(s["M_kB"] for s in live) / max(1, len(live))
    priv_avg = sum(s["private_kB"] for s in live) / max(1, len(live))
    peak = max(live, key=lambda s: s["M_kB"]) if live else {}
    ncmd = sum(r["n_cmds"] for r in results.values())
    summary = {
        "cohort": cohort, "config": a.config, "rep": a.rep, "tag": a.tag, "n_trajectories": len(traces),
        "wall_s": round(wall, 2), "M_avg_MiB": round(m_avg / 1024, 2), "private_avg_MiB": round(priv_avg / 1024, 2),
        "M_peak_MiB": round(peak.get("M_kB", 0) / 1024, 2),
        "ksm_saved_avg_MiB": round(sum(s["ksm_saved_kB"] for s in live) / max(1, len(live)) / 1024, 2),
        "ksmd_cpu_s": round(cpu1 - cpu0, 2) if cpu0 is not None and cpu1 is not None else None,
        "cmds": ncmd, "code_match": sum(r["code_match"] for r in results.values()),
        "sha_match": sum(r["sha_match"] for r in results.values()),
        "errors": [r.get("error") for r in results.values() if r.get("error")],
    }
    print(json.dumps(summary), flush=True)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    os.makedirs(os.path.join(HERE, "results"), exist_ok=True)
    name = f"{cohort}-{a.config}{('-' + a.tag) if a.tag else ''}-r{a.rep}-{stamp}.json"
    json.dump({"summary": summary, "template": T, "template_manifest_sha": sorted(tm),
               "template_mem_mib": man["mem_mib"], "numa_node": a.numa_node, "interval_s": a.interval,
               "ksm_status": knobs, "uname": os.uname().release, "trajectories": results, "series": series},
              open(os.path.join(HERE, "results", name), "w"), indent=1)
    print(f"wrote results/{name}")


if __name__ == "__main__":
    main()
