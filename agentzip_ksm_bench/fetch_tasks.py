#!/usr/bin/env python3
"""Build a task manifest from R2E-Gym-Lite for the KSM experiment.

Each R2E-Gym-Lite row carries everything a sandbox needs:
  repo_name, docker_image, commit_hash, problem_statement,
  parsed_commit_content (the ground-truth change), expected_output_json (tests).

This selects tasks (optionally all from one repo, so they form a realistic
"siblings" cohort: shared template, divergent private state) and writes a JSON
manifest consumed by run_agentzip_ksm.py. It also extracts a best-effort
ground-truth patch per task.

Run on the CloudLab node (needs `pip install datasets`). The docker images are
pulled lazily by `docker run`; each is ~300-500 MB, so mind disk space.

  python3 fetch_tasks.py --repo scrapy --num 8 --out tasks_scrapy.json
  python3 fetch_tasks.py --list-repos        # just show repo -> task counts
"""

import argparse
import json
import re
import sys

# Common top-level import names, keyed by repo. Used to "warm" the package in
# each sandbox. Repos not listed fall back to the repo name lowercased.
PKG_HINT = {
    "scrapy": "scrapy", "numpy": "numpy", "pandas": "pandas", "pillow": "PIL",
    "aiohttp": "aiohttp", "tornado": "tornado", "pyramid": "pyramid",
    "orange3": "Orange", "datalad": "datalad", "coveragepy": "coverage",
    "coverage.py": "coverage",
}


def extract_patch(parsed_commit_content):
    """Best-effort unified diff from the row's parsed_commit_content field.

    The exact shape of this field is not documented, so we try a few common
    layouts and otherwise return "" (the sandbox then runs on a clean tree and
    still produces realistic memory -- verify the shape on-node and adjust).
    """
    if not parsed_commit_content:
        return ""
    obj = parsed_commit_content
    if isinstance(obj, str):
        # Already a diff?
        if obj.lstrip().startswith(("diff --git", "--- ", "Index: ")):
            return obj
        try:
            obj = json.loads(obj)
        except Exception:  # noqa: BLE001
            return ""
    diffs = []
    if isinstance(obj, dict):
        candidates = obj.get("file_diffs") or obj.get("diffs") or []
        for d in candidates if isinstance(candidates, list) else []:
            if isinstance(d, dict):
                for k in ("diff", "patch", "unified_diff", "content"):
                    if d.get(k):
                        diffs.append(d[k])
                        break
            elif isinstance(d, str):
                diffs.append(d)
    return "\n".join(diffs)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", default="R2E-Gym/R2E-Gym-Lite")
    ap.add_argument("--split", default="train")
    ap.add_argument("--repo", default="", help="restrict to one repo_name (for a siblings cohort)")
    ap.add_argument("--num", type=int, default=8, help="max tasks to emit")
    ap.add_argument("--out", default="tasks.json")
    ap.add_argument("--list-repos", action="store_true")
    args = ap.parse_args()

    try:
        from datasets import load_dataset
    except ImportError:
        sys.exit("needs `pip install datasets`")

    ds = load_dataset(args.dataset, split=args.split)

    if args.list_repos:
        counts = {}
        for row in ds:
            counts[row["repo_name"]] = counts.get(row["repo_name"], 0) + 1
        for repo, c in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"{c:5d}  {repo}")
        return

    manifest, seen = [], 0
    for row in ds:
        if args.repo and row.get("repo_name") != args.repo:
            continue
        repo = row.get("repo_name", "?")
        pkg = PKG_HINT.get(repo.lower(), repo.lower())
        task_id = f"{repo}_{str(row.get('commit_hash',''))[:10]}_{seen}"
        manifest.append({
            "task_id": task_id,
            "repo_name": repo,
            "docker_image": row["docker_image"],
            "commit_hash": row.get("commit_hash", ""),
            "pkg": pkg,
            "gt_patch": extract_patch(row.get("parsed_commit_content")),
            "problem_statement": (row.get("problem_statement") or "")[:2000],
        })
        seen += 1
        if seen >= args.num:
            break

    if not manifest:
        sys.exit(f"no tasks matched (repo={args.repo!r}). Try --list-repos.")
    with open(args.out, "w") as f:
        json.dump(manifest, f, indent=2)
    with_patch = sum(1 for t in manifest if t["gt_patch"])
    print(f"wrote {len(manifest)} tasks -> {args.out}  "
          f"({with_patch} with an extracted GT patch)")
    if with_patch < len(manifest):
        print("note: some tasks have no extracted patch; inspect parsed_commit_content "
              "shape and adjust extract_patch() if you want GT edits applied.")


if __name__ == "__main__":
    main()
