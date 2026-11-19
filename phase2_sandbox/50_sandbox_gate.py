#!/usr/bin/env python3
"""Phase 2 gate: do Zeroboot sandboxes behave like R2E-Gym's docker containers?

For one task template (20_build_r2e_template.py):
  1. REFERENCE: run a fixed step list in R2E-Gym's own docker runtime.
  2. SINGLE: run the same steps in one Zeroboot sandbox (ZerobootRuntime).
  3. CONCURRENT: N sandboxes (default 16), each held >= --hold-min minutes,
     repeating the steps with random idle "LLM think" gaps; every step must
     match the reference. Serve-process memory is sampled throughout.

Steps (each is what an agent's tool call does in R2E):
  tests_before  bash /root/run_tests.sh          -> reward must equal docker's (0)
  view          file_editor view <relevant file>
  search        search --search_term 'def ' --path <its dir>
  put_patch     copy_to_container(ground-truth patch)
  apply         git apply /tmp/zb_gt.patch       (the edit)
  create        file_editor create /testbed/zb_probe.py
  run_probe     python /testbed/zb_probe.py
  tests_after   bash /root/run_tests.sh          -> reward must equal docker's (1)
  diffstat      git diff --stat
Equality: exit-code string always; parsed test map + reward for test steps;
output text for the rest (after masking durations).

  sudo -E $R2E_DIR/.venv/bin/python 50_sandbox_gate.py /mnt/data/templates/aiohttp/f0d74880deec
  ... --n 4 --hold-min 2          # quick run
Writes results/gate-<repo>-<utc>.json; exits non-zero on failure.
"""

import argparse
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
KSM = os.path.dirname(HERE)
R2E_DIR = os.environ.get("R2E_DIR", "/mnt/data/r2e/R2E-Gym")
sys.path.insert(0, HERE)
from zbsandbox import ZerobootRuntime  # noqa: E402

TOOLS = ["src/r2egym/agenthub/tools/r2egym/file_editor.py", "src/r2egym/agenthub/tools/search.py",
         "src/r2egym/agenthub/tools/r2egym/execute_bash.py", "src/r2egym/agenthub/tools/finish.py"]
# Steps whose output lists directory entries: compared as a sorted multiset of
# lines, because readdir order differs between docker's overlayfs-on-ext4 and
# the sandbox's tmpfs (documented environment difference; same content).
UNORDERED = {"search"}
_DUR = re.compile(r"\d+(\.\d+)?\s*(s|sec|seconds|ms)\b|\d+\.\d+s")


def steps_for(task, patch_path):
    rf = task.get("relevant_files") or []
    if isinstance(rf, str):
        rf = json.loads(rf.replace("'", '"'))
    rel = rf[0] if rf else ""
    d = os.path.dirname(rel) or "."
    return [
        ("tests_before", "tests", None),
        ("view", "run", f"file_editor view --path /testbed/{rel}"),
        ("search", "run", f"search --search_term 'def ' --path /testbed/{d}"),
        ("put_patch", "put", (patch_path, "/tmp/zb_gt.patch")),
        ("apply", "run", "git apply /tmp/zb_gt.patch"),
        ("create", "run", "file_editor create --path /testbed/zb_probe.py --file_text 'print(6*7)'"),
        ("run_probe", "run", "python /testbed/zb_probe.py"),
        ("tests_after", "tests", None),
        ("diffstat", "run", "git diff --stat"),
    ]


def run_steps(rt, steps, scorer, gaps=None):
    out = []
    for i, (name, kind, arg) in enumerate(steps):
        if gaps:
            time.sleep(gaps[i])
        t0 = time.time()
        if kind == "tests":
            o, c = rt.run_tests()
            rec = {"code": c, "tests": scorer.parse(o), "reward": scorer.reward(o)}
        elif kind == "put":
            rt.copy_to_container(*arg)
            o, c, rec = "", "0", {"code": "0"}
        else:
            o, c = rt.run(arg)
            t = _DUR.sub("<dur>", o)
            if name in UNORDERED:
                t = "\n".join(sorted(t.splitlines()))
            rec = {"code": c, "text": t}
        rec.update(step=name, secs=round(time.time() - t0, 3), out_len=len(o))
        out.append(rec)
    return out


class Scorer:
    """R2E-Gym's _calculate_reward_r2e, applied to a given test output."""
    def __init__(self, docker_rt, task):
        from r2egym.repo_analysis.execution_log_parser import decolor_dict_keys
        self.rt, self.decolor = docker_rt, decolor_dict_keys
        self.expected = {k.split(" - ")[0]: v for k, v in
                         sorted(decolor_dict_keys(json.loads(task["expected_output_json"])).items())}

    def parse(self, output):
        p = self.decolor(self.rt.parse_logs(output))
        return {k.split(" - ")[0]: p[k] for k in sorted(p)}

    def reward(self, output):
        p = self.parse(output)
        if len(p) != len(self.expected):
            return 0.0
        return 1.0 if all((not k) or (k in self.expected and p[k] == self.expected[k]) for k in p) else 0.0


def compare(ref, got):
    diffs = []
    for r, g in zip(ref, got):
        for k in ("code", "tests", "reward", "text"):
            if k in r and r.get(k) != g.get(k):
                diffs.append({"step": r["step"], "field": k,
                              "docker": str(r.get(k))[:300], "sandbox": str(g.get(k))[:300]})
    return diffs


def serve_mem(pid):
    try:
        t = open(f"/proc/{pid}/smaps_rollup").read()
    except OSError:
        return {}
    return {l.split(":")[0]: int(l.split()[1]) for l in t.splitlines()
            if l.split(":")[0] in ("Rss", "Pss", "Anonymous", "Private_Dirty")}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("template_dir")
    ap.add_argument("--n", type=int, default=16)
    ap.add_argument("--hold-min", type=float, default=10.0)
    ap.add_argument("--gap", default="5,40", help="min,max seconds of idle between steps")
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()
    if os.geteuid() != 0:
        sys.exit("run as root (sudo -E ...)")
    T = os.path.abspath(a.template_dir)
    task = json.load(open(os.path.join(T, "task.json")))
    man = json.load(open(os.path.join(T, "manifest.json")))
    os.chdir(R2E_DIR)
    sys.path.insert(0, os.path.join(R2E_DIR, "src"))
    from r2egym.agenthub.environment.env import EnvArgs, RepoEnv
    from r2egym.commit_models.diff_classes import ParsedCommit

    row = {k: v for k, v in task.items() if not k.startswith("_")}
    patch = ParsedCommit(**json.loads(row["parsed_commit_content"])).get_patch(test_file=False, non_test_file=True)
    patch_path = os.path.join(T, "gt_nontest.patch")
    open(patch_path, "w").write(patch)
    steps = steps_for(row, patch_path)

    # 1. reference, in docker
    print("== 1. docker reference ==", flush=True)
    env = RepoEnv(EnvArgs(ds=row), verbose=False)
    env.add_commands(TOOLS)
    scorer = Scorer(env.runtime, row)
    ref = run_steps(env.runtime, steps, scorer)
    env.close()
    for r in ref:
        print(f"   {r['step']:13} code={r['code']!s:20} reward={r.get('reward', '-')} {r['secs']}s len={r['out_len']}")

    # serve the template
    sh = lambda *c, **e: subprocess.run(["bash", os.path.join(KSM, "phase1_zeroboot_cow", "30_serve.sh"), *c],
                                        env={**os.environ, "WORKDIR": T, **e}, check=False, capture_output=True, text=True)
    sh("stop"); time.sleep(1)
    t0 = time.time()
    sh(BACKGROUND="1")
    for _ in range(600):
        try:
            urllib.request.urlopen("http://127.0.0.1:8080/v1/health", timeout=2); break
        except Exception:  # noqa: BLE001
            time.sleep(1)
    load_s = time.time() - t0
    pid = int(open(os.path.join(T, "serve.pid")).read())
    print(f"   serve up in {load_s:.0f}s (pid {pid})", flush=True)

    # 2. one sandbox
    print("== 2. single sandbox ==", flush=True)
    rt = ZerobootRuntime(seed=1)
    single = run_steps(rt, steps, scorer)
    rt.close()
    d_single = compare(ref, single)
    for r in single:
        print(f"   {r['step']:13} code={r['code']!s:20} reward={r.get('reward', '-')} {r['secs']}s len={r['out_len']}")
    print(f"   diffs vs docker: {d_single or 'none'}", flush=True)

    # 3. concurrent hold
    print(f"== 3. {a.n} sandboxes, >= {a.hold_min} min each ==", flush=True)
    gmin, gmax = (float(x) for x in a.gap.split(","))
    rng = random.Random(a.seed)
    mem_series, stop = [], threading.Event()
    def sampler():
        t0 = time.time()
        while not stop.is_set():
            mem_series.append({"t": round(time.time() - t0, 1), **serve_mem(pid)})
            stop.wait(5)
    results, lock = {}, threading.Lock()
    def worker(i, gaps_seed):
        r = random.Random(gaps_seed)
        rt = ZerobootRuntime(seed=1000 + i)
        rounds, diffs, t_end = 0, [], time.time() + a.hold_min * 60
        try:
            while True:
                # undo the previous round's edit so every round starts from the task state
                # (reverse the patch; NOT `git checkout .`, which would resurrect the
                # run_tests.sh / r2e_tests that R2E's setup moved out of /testbed)
                if rounds:
                    o, c = rt.run("git apply -R /tmp/zb_gt.patch && rm -f /testbed/zb_probe.py")
                    if c != "0":
                        diffs.append({"step": "reset", "field": "code", "docker": "0", "sandbox": c, "round": rounds})
                got = run_steps(rt, steps, scorer, gaps=[r.uniform(gmin, gmax) for _ in steps])
                diffs += [dict(d, round=rounds) for d in compare(ref, got)]
                rounds += 1
                if time.time() >= t_end:
                    break
        except Exception as e:  # noqa: BLE001
            diffs.append({"error": repr(e), "round": rounds})
        finally:
            rt.close()
        with lock:
            results[i] = {"rounds": rounds, "diffs": diffs}
    th = threading.Thread(target=sampler); th.start()
    ws = [threading.Thread(target=worker, args=(i, rng.random())) for i in range(a.n)]
    t0 = time.time(); [w.start() for w in ws]; [w.join() for w in ws]
    held_s = time.time() - t0
    stop.set(); th.join()
    sh("stop")

    n_diffs = sum(len(r["diffs"]) for r in results.values())
    peak = max(mem_series, key=lambda m: m.get("Pss", 0), default={})
    checks = [
        ("docker reference: tests fail before fix, pass after",
         ref[0].get("reward") == 0.0 and ref[7].get("reward") == 1.0,
         f"{ref[0].get('reward')} -> {ref[7].get('reward')}"),
        ("single sandbox == docker on every step", not d_single, f"{len(d_single)} diffs"),
        (f"{a.n} concurrent sandboxes == docker on every step of every round", n_diffs == 0,
         f"{n_diffs} diffs; rounds/sandbox {sorted(r['rounds'] for r in results.values())}"),
        (f"each sandbox held >= {a.hold_min} min", held_s >= a.hold_min * 60, f"{held_s/60:.1f} min"),
    ]
    print("\n== Phase 2 gate ==")
    ok = True
    for name, c, d in checks:
        ok &= bool(c)
        print(f"  [{'PASS' if c else 'FAIL'}] {name}  {d}")
    print(f"  serve peak: Pss {peak.get('Pss', 0)/1024:.0f} MiB, Anonymous {peak.get('Anonymous', 0)/1024:.0f} MiB "
          f"(template {man['mem_mib']} MiB guest RAM)")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    os.makedirs(os.path.join(HERE, "results"), exist_ok=True)
    out = os.path.join(HERE, "results", f"gate-{man['task']['repo']}-{stamp}.json")
    json.dump({"when_utc": stamp, "template": T, "task": man["task"], "mem_mib": man["mem_mib"],
               "n": a.n, "hold_min": a.hold_min, "gap_s": [gmin, gmax], "serve_load_s": round(load_s, 1),
               "pass": ok, "checks": [{"name": n, "pass": bool(c), "detail": d} for n, c, d in checks],
               "reference": ref, "single": single, "single_diffs": d_single,
               "concurrent": results, "serve_mem_kB": mem_series}, open(out, "w"), indent=1)
    print(f"wrote {out}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
