#!/usr/bin/env python3
"""Summarize a run_bench.py results directory.

usage: python3 analyze.py results/<dir>

Prints where each session's pages ended up, then the median time per access
for every condition and its slowdown against the same instance with KSM off.
Writes the second table to <dir>/summary.csv.
"""

import csv
import statistics
import sys
from collections import defaultdict
from pathlib import Path

# An instance counts as the session's "victim" if at least this fraction of its pages is remote.
VICTIM_MIN_REMOTE = 0.01


def fmt_size(n):
    n = int(n)
    for unit, shift in (("G", 30), ("M", 20), ("K", 10)):
        if n >= 1 << shift and n % (1 << shift) == 0:
            return f"{n >> shift}{unit}"
    return str(n)


def read_csv(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def print_table(header, rows):
    widths = [max(len(str(x)) for x in col) for col in zip(header, *rows)]
    for row in [header, ["-" * w for w in widths], *rows]:
        print("  ".join(str(x).rjust(w) for x, w in zip(row, widths)))


def summarize(out):
    out = Path(out)
    runs = read_csv(out / "runs.csv")
    sessions = read_csv(out / "sessions.csv")
    if not runs:
        print("no completed measurements")
        return

    # Which instance (if any) lost its copies in each session.
    victim = {}
    for row in runs:
        sid, frac = row["session"], float(row["remote_frac"])
        if frac >= VICTIM_MIN_REMOTE and frac > victim.get(sid, ("", 0.0))[1]:
            victim[sid] = (row["instance"], frac)

    print("\nPage placement after merging (remote = pages not on the instance's CPU node)")
    table = [[s["session"], s["plan"], s["config"], fmt_size(s["size"]), s["shared_frac"],
              s["merge_secs"], s["pages_sharing"], f"{100 * float(s['A_remote_frac']):.1f}%",
              f"{100 * float(s['B_remote_frac']):.1f}%"] for s in sessions]
    print_table(["sess", "plan", "config", "size", "shared", "merge_s", "pages_sharing",
                 "A_remote", "B_remote"], table)

    # Baseline: the same instance doing the same thing with KSM off.
    base = defaultdict(list)
    for row in runs:
        if row["config"] == "ksm_off":
            key = (row["size"], row["pattern"], row["mode"], row["write_frac"], row["instance"])
            base[key].append(float(row["ns_per_op"]))

    groups = defaultdict(list)
    for row in runs:
        is_victim = victim.get(row["session"], ("",))[0] == row["instance"]
        key = (row["config"], row["size"], row["shared_frac"], row["pattern"], row["mode"],
               row["write_frac"], row["instance"] + ("*" if is_victim else ""))
        groups[key].append(row)

    print("\nMedian time per access (* = instance whose pages were moved to the other node)")
    header = ["config", "size", "shared", "pattern", "mode", "write", "inst", "remote",
              "merged_before", "merged_after", "n", "ns_per_op", "GB/s", "slowdown"]
    table, summary = [], []
    for key in sorted(groups):
        config, size, shared, pattern, mode, wfrac, inst = key
        rows = groups[key]
        pages = int(rows[0]["pages"])
        ns = statistics.median(float(r["ns_per_op"]) for r in rows)
        gbps = statistics.median(float(r["gbps"]) for r in rows)
        remote = statistics.median(float(r["remote_frac"]) for r in rows)
        merged_b = statistics.median(int(r["merging_before"]) for r in rows) / pages
        merged_a = statistics.median(int(r["merging_after"]) for r in rows) / pages
        b = base.get((size, pattern, mode, wfrac, inst.rstrip("*")))
        slowdown = ns / statistics.median(b) if b else None
        values = [config, fmt_size(size), shared, pattern, mode, wfrac, inst,
                  f"{100 * remote:.1f}%", f"{100 * merged_b:.1f}%", f"{100 * merged_a:.1f}%",
                  len(rows), f"{ns:.2f}", f"{gbps:.2f}",
                  f"{slowdown:.3f}x" if slowdown else "-"]
        table.append(values)
        summary.append(dict(zip(header, values)))
    print_table(header, table)

    with open(out / "summary.csv", "w", newline="") as f:
        w = csv.DictWriter(f, header)
        w.writeheader()
        w.writerows(summary)

    merged = [s for s in sessions if s["config"] == "ksm_merge_all" and float(s["shared_frac"]) == 1]
    if merged:
        worst = max(victim.get(s["session"], ("", 0.0))[1] for s in merged)
        if worst >= 0.9:
            print(f"\nOK: cross-node merge left one instance with {100 * worst:.1f}% remote pages.")
        else:
            print(f"\nWARNING: after merging, no instance had more than {100 * worst:.1f}% remote "
                  "pages. Check the session logs: did merging finish?")
    print(f"\nwrote {out / 'summary.csv'}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    summarize(sys.argv[1])
