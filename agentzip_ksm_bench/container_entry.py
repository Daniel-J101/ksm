#!/usr/bin/env python3
"""In-container entrypoint for the AgentZip/KSM experiment.

Runs *inside* each R2E-Gym sandbox container. It makes the process's
anonymous memory eligible for KSM, runs a deterministic, LLM-free workload
(optionally apply the ground-truth patch, then exercise the repo), and then
holds the resulting memory resident so the host driver can run ksmd and read
the merge counters.

Why prctl here and not in a wrapper: PR_SET_MEMORY_MERGE sets a flag on the
*current* mm that is CLEARED on execve(). A wrapper that prctl()s and then
exec()s the real workload would lose it. So we set it in this process, after
Python has done its last exec, and keep the heavy workload in this same
process tree (import + pytest.main run in-process; fork children inherit the
flag, exec children do not).

Needs Linux >= 5.17 for PR_SET_MEMORY_MERGE. The host enables KSM globally.
"""

import argparse
import ctypes
import ctypes.util
import os
import subprocess
import sys
import time
from pathlib import Path

PR_SET_MEMORY_MERGE = 67  # <linux/prctl.h>, since 5.17


def enable_memory_merge():
    """prctl(PR_SET_MEMORY_MERGE, 1): all current and future anon VMAs of this
    process become KSM-mergeable. Returns True on success."""
    libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
    rc = libc.prctl(PR_SET_MEMORY_MERGE, 1, 0, 0, 0)
    if rc != 0:
        err = ctypes.get_errno()
        print(f"[entry] prctl(PR_SET_MEMORY_MERGE) failed: errno={err} "
              f"({os.strerror(err)}). Kernel may be < 5.17; KSM will not scan "
              f"this process's memory.", flush=True)
        return False
    print("[entry] PR_SET_MEMORY_MERGE enabled: anon memory is KSM-mergeable.", flush=True)
    return True


def apply_gt_patch(repo_dir, patch_file):
    patch = Path(patch_file)
    if not patch.exists() or patch.stat().st_size == 0:
        print(f"[entry] no GT patch at {patch_file}; running on clean tree.", flush=True)
        return
    for args in (["git", "-C", repo_dir, "apply", "--whitespace=nowarn", str(patch)],
                 ["patch", "-p1", "-d", repo_dir, "-i", str(patch)]):
        r = subprocess.run(args, capture_output=True, text=True)
        if r.returncode == 0:
            print(f"[entry] applied GT patch with: {' '.join(args[:2])}", flush=True)
            return
    print(f"[entry] WARNING: could not apply GT patch (tried git apply and patch). "
          f"Continuing on clean tree. git stderr:\n{r.stderr}", flush=True)


def run_workload(repo_dir, pkg, test_cmd):
    """Exercise the repo so it faults in a realistic set of private pages.

    Kept in-process where possible so the memory lands in this (mergeable) mm.
    We do NOT care whether tests pass -- we care that the package and its test
    collection are resident. Errors are swallowed on purpose.
    """
    os.chdir(repo_dir)
    if pkg:
        try:
            __import__(pkg)
            print(f"[entry] imported {pkg}", flush=True)
        except Exception as e:  # noqa: BLE001  -- realistic workloads do fail
            print(f"[entry] import {pkg} raised {type(e).__name__}: {e}", flush=True)
    # Prefer in-process pytest so its memory stays in this mm.
    try:
        import pytest  # noqa: F401
        argv = test_cmd.split() if test_cmd else ["-q", "-x", "--no-header", "-p", "no:cacheprovider"]
        print(f"[entry] pytest.main({argv})", flush=True)
        try:
            pytest.main(argv)
        except SystemExit:
            pass
    except Exception as e:  # noqa: BLE001
        print(f"[entry] pytest unavailable or failed ({e}); import-only workload.", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo-dir", default="/testbed")
    ap.add_argument("--pkg", default="", help="top-level import name to warm (e.g. scrapy)")
    ap.add_argument("--gt-patch", default="", help="path to ground-truth patch to apply first")
    ap.add_argument("--test-cmd", default="", help="pytest args; empty = sensible default")
    ap.add_argument("--ready-file", required=True,
                    help="path (on a mounted volume) to touch once memory is resident")
    ap.add_argument("--hold-secs", type=int, default=600,
                    help="seconds to keep memory resident for the host to merge + measure")
    args = ap.parse_args()

    enable_memory_merge()
    if args.gt_patch:
        apply_gt_patch(args.repo_dir, args.gt_patch)
    run_workload(args.repo_dir, args.pkg, args.test_cmd)

    # Signal the host that this sandbox has reached steady-state memory.
    rf = Path(args.ready_file)
    rf.parent.mkdir(parents=True, exist_ok=True)
    rf.write_text(f"{os.getpid()} {int(time.time())}\n")
    print(f"[entry] ready; holding memory resident for {args.hold_secs}s "
          f"(pid {os.getpid()}).", flush=True)

    # Touch pages periodically so nothing gets swapped/dropped while we wait.
    deadline = time.time() + args.hold_secs
    while time.time() < deadline:
        time.sleep(5)


if __name__ == "__main__":
    main()
