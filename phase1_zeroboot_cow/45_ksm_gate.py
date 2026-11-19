#!/usr/bin/env python3
"""Phase 1b KSM feasibility gate: can KSM merge pages that Zeroboot forks dirty?

Before measuring "how much does KSM save for N agents" we must show KSM can
merge fork-private pages at all. Fork memory is mmap(MAP_PRIVATE) of the
snapshot memfd; CoW'd pages there are anonymous, but ksmd only scans VMAs
marked MADV_MERGEABLE, which upstream Zeroboot never sets (patch 0003 adds
ZEROBOOT_KSM_MERGEABLE=1).

Four conditions, each with N forks dirtying TOUCH MiB and held resident:

  A  KSM on,  mergeable=0, same content    -> expect ~0 saved   (patch is required)
  B  KSM on,  mergeable=1, same content    -> expect ~(N-1)*TOUCH saved
  C  KSM on,  mergeable=1, unique content  -> touched pages NOT merged
  D  KSM off, mergeable=1, same content    -> expect 0 saved   (KSM is the cause)

"saved" = /sys/kernel/mm/ksm/pages_sharing at steady state (each sharing page
is one page of physical memory not needed). Also reported: the serve process's
Pss drop vs. condition D, an independent measure of the same saving.

Requires the Phase 1b template (init_agentzip with the `work` seed option):
  sudo python3 45_ksm_gate.py                    # WORKDIR from ../env.sh default
  sudo WORKDIR=/tmp/zb-workdir-1b python3 45_ksm_gate.py --n 8 --touch-mb 64
Writes results/ksm-gate-<utc>.json and exits non-zero if the gate fails.
"""

import argparse
import glob
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
KSM_ROOT = os.path.dirname(HERE)
KSM_CTL = os.path.join(KSM_ROOT, "tools", "ksm_ctl.sh")
SPINS_PER_SEC = 3.3e8   # measured on E5-2683 v3: 1e9 spins ~ 3.0 s; scaled with margin below


def sh(cmd, env=None, check=True):
    e = dict(os.environ, **(env or {}))
    return subprocess.run(cmd, env=e, check=check, text=True, capture_output=True)


def serve(mergeable, workdir):
    env = {"WORKDIR": workdir}
    sh(["bash", os.path.join(HERE, "30_serve.sh"), "stop"], env)
    time.sleep(1)
    env.update(BACKGROUND="1", ZEROBOOT_KSM_MERGEABLE="1" if mergeable else "0")
    out = sh(["bash", os.path.join(HERE, "30_serve.sh")], env).stdout
    if '"ok"' not in out:
        sys.exit(f"serve did not come up:\n{out}")


def run_condition(name, ksm_on, mergeable, content, a, workdir):
    print(f"\n== {name}: KSM {'on' if ksm_on else 'off'}, mergeable={int(mergeable)}, "
          f"content={content} ==", flush=True)
    sh(["bash", KSM_CTL, "on" if ksm_on else "off"])
    serve(mergeable, workdir)
    spins = int((a.hold + a.ramp + 8) * SPINS_PER_SEC)
    before = set(glob.glob(os.path.join(HERE, "results", "cow-*.json")))
    p = sh([sys.executable, os.path.join(HERE, "40_cow_probe.py"), "--sweep", str(a.n),
            "--touch-mb", str(a.touch_mb), "--hold", str(a.hold), "--ramp", str(a.ramp),
            "--spins", str(spins), "--content", content, "--tag", f"gate{name}",
            "--outdir", os.path.join(HERE, "results")], check=False)
    print("\n".join(l for l in p.stdout.splitlines() if l.startswith("N=")))
    if p.returncode != 0:
        sys.exit(f"probe failed:\n{p.stdout}\n{p.stderr}")
    new = sorted(set(glob.glob(os.path.join(HERE, "results", "cow-*.json"))) - before)
    pt = json.load(open(new[-1]))["points"][0]
    return {
        "condition": name, "ksm_on": ksm_on, "mergeable": mergeable, "content": content,
        "forks_ok": pt["forks_ok"], "ksm_saved_MB": pt["ksm_saved_MB"],
        "ksm_pages_shared": pt.get("steady_ksm_pages_shared"),
        "serve_ksm_merging_pages": pt.get("steady_serve_ksm_merging_pages"),
        "steady_Pss_MB": round((pt.get("steady_Pss_kB") or 0) / 1024, 1),
        "serve_added_MB": pt["serve_added_MB"], "result_file": os.path.relpath(new[-1], HERE),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=8)
    ap.add_argument("--touch-mb", type=int, default=64)
    ap.add_argument("--hold", type=float, default=30.0)
    ap.add_argument("--ramp", type=float, default=3.0)
    a = ap.parse_args()
    if os.geteuid() != 0:
        sys.exit("run with sudo")
    workdir = os.environ.get("WORKDIR", "/tmp/zb-workdir-agent")
    ideal = (a.n - 1) * a.touch_mb

    rows = {}
    for name, ksm_on, merge, content in (("D", False, True, "same"), ("A", True, False, "same"),
                                         ("B", True, True, "same"), ("C", True, True, "unique")):
        rows[name] = run_condition(name, ksm_on, merge, content, a, workdir)
    sh(["bash", KSM_CTL, "off"])
    sh(["bash", os.path.join(HERE, "30_serve.sh"), "stop"], {"WORKDIR": workdir})

    A, B, C, D = (rows[k]["ksm_saved_MB"] for k in "ABCD")
    pss_drop_B = rows["D"]["steady_Pss_MB"] - rows["B"]["steady_Pss_MB"]
    checks = [
        ("all forks completed", all(r["forks_ok"] == a.n for r in rows.values()), ""),
        ("D: KSM off merges nothing", D == 0, f"{D} MiB"),
        ("A: without MADV_MERGEABLE nothing merges", A < 0.02 * ideal, f"{A} MiB"),
        ("B: identical pages merge ~(N-1)*touch", B >= 0.9 * ideal, f"{B} MiB vs ideal {ideal} MiB"),
        ("B: serve Pss drops by about the same amount", pss_drop_B >= 0.8 * ideal,
         f"Pss D-B = {pss_drop_B:.1f} MiB"),
        ("C: unique pages don't merge (touched region)", C < B - 0.8 * ideal,
         f"{C} MiB (B={B})"),
    ]
    print("\n== KSM gate ==")
    ok = True
    for name, cond, detail in checks:
        ok &= bool(cond)
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}  {detail}")
    print("GATE PASS: KSM can merge Zeroboot fork pages (with patch 0003)." if ok else "GATE FAIL")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = os.path.join(HERE, "results", f"ksm-gate-{stamp}.json")
    knobs = json.load(open(os.path.join(HERE, rows["B"]["result_file"])))["ksm_knobs"]
    with open(out, "w") as f:
        json.dump({"when_utc": stamp, "n": a.n, "touch_mb": a.touch_mb, "hold_s": a.hold,
                   "ideal_saving_MB": ideal, "pass": ok, "conditions": rows,
                   "checks": [{"name": n, "pass": bool(c), "detail": d} for n, c, d in checks],
                   "ksm_knobs_used": knobs, "uname": os.uname().release}, f, indent=2)
    print(f"wrote {out}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
