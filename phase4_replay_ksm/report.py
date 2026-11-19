#!/usr/bin/env python3
"""Aggregate Phase 4 replay results into a table (+ an M(t) plot if matplotlib exists).

  python3 report.py                       # all results/*.json
  python3 report.py --tag ""              # only untagged (main) runs
  $R2E_DIR/.venv/bin/python report.py     # same, plus results/mt-<cohort>.png

Per cohort:
  M_avg (MiB)  time-averaged sandbox-owned memory, mean +- sd over reps
  S            KSM saving = 1 - M_avg(ksm)/M_avg(nocomp), per rep pair, mean +- sd
  slowdown     wall(ksm)/wall(nocomp), per rep pair
"""

import argparse
import glob
import json
import os
import statistics as st
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))


def ms(v):
    return f"{st.mean(v):.1f} ± {st.stdev(v):.1f}" if len(v) > 1 else f"{v[0]:.1f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default=None, help="only runs with this tag ('' = untagged)")
    a = ap.parse_args()
    runs = [json.load(open(f)) for f in sorted(glob.glob(os.path.join(HERE, "results", "*.json")))]
    runs = [r for r in runs if a.tag is None or r["summary"]["tag"] == a.tag]
    by = defaultdict(lambda: defaultdict(dict))          # cohort -> config -> rep -> run
    for r in runs:
        s = r["summary"]
        by[s["cohort"]][s["config"]][s["rep"]] = r

    lines = ["| cohort | sandboxes | M_avg NoComp (MiB) | M_avg KSM (MiB) | **KSM saving S** | wall NoComp (s) | slowdown | ksmd CPU (s) | exit codes match | reps |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for cohort, cfg in sorted(by.items()):
        reps = sorted(set(cfg.get("nocomp", {})) & set(cfg.get("ksm", {})))
        if not reps:
            continue
        mn = [cfg["nocomp"][k]["summary"]["M_avg_MiB"] for k in reps]
        mk = [cfg["ksm"][k]["summary"]["M_avg_MiB"] for k in reps]
        S = [100 * (1 - b / x) for x, b in zip(mn, mk)]
        wn = [cfg["nocomp"][k]["summary"]["wall_s"] for k in reps]
        sl = [cfg["ksm"][k]["summary"]["wall_s"] / cfg["nocomp"][k]["summary"]["wall_s"] for k in reps]
        cpu = [cfg["ksm"][k]["summary"]["ksmd_cpu_s"] or 0 for k in reps]
        allr = [cfg[c][k]["summary"] for c in ("nocomp", "ksm") for k in reps]
        match = f"{sum(x['code_match'] for x in allr)}/{sum(x['cmds'] for x in allr)}"
        n = allr[0]["n_trajectories"]
        lines.append(f"| {cohort} | {n} | {ms(mn)} | {ms(mk)} | **{ms(S)} %** | {ms(wn)} | "
                     f"{st.mean(sl):.3f}× | {ms(cpu)} | {match} | {len(reps)} |")
    table = "\n".join(lines)
    print(table)
    out = os.path.join(HERE, "results", "summary.md")
    open(out, "w").write(table + "\n")
    print(f"\nwrote {out}")

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("(matplotlib not available: skipping plots)")
        return
    for cohort, cfg in sorted(by.items()):
        if 0 not in cfg.get("nocomp", {}) or 0 not in cfg.get("ksm", {}):
            continue
        fig, ax = plt.subplots(figsize=(7, 3.2))
        for c, color in (("nocomp", "#5b6b7f"), ("ksm", "#d1603d")):
            ser = cfg[c][0]["series"]
            ax.plot([p["t"] for p in ser], [p["M_kB"] / 1024 for p in ser], color=color, lw=1.6,
                    label=f"{'NoComp' if c == 'nocomp' else 'KSM'} (avg {cfg[c][0]['summary']['M_avg_MiB']:.0f} MiB)")
        ax.set_xlabel("time since replay start (s)")
        ax.set_ylabel("sandbox-owned memory M(t), MiB")
        ax.set_title(f"{cohort}, rep 0", fontsize=10)
        ax.grid(alpha=0.25)
        ax.legend(frameon=False, fontsize=8)
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
        fig.tight_layout()
        p = os.path.join(HERE, "results", f"mt-{cohort}.png")
        fig.savefig(p, dpi=130)
        print(f"wrote {p}")


if __name__ == "__main__":
    main()
