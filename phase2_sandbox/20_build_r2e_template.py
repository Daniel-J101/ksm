#!/usr/bin/env python3
"""Build a Zeroboot sandbox template for one R2E-Gym task.

Fidelity rule: the sandbox must start in exactly the state R2E-Gym's agent sees
in docker. So we don't re-implement R2E's setup. We run R2E-Gym's OWN code:
  RepoEnv(EnvArgs(ds=row))     -> starts the task container, runs setup_env
                                  (venv symlinks, `uv pip install chardet`,
                                  pyc cleanup, hides run_tests.sh/r2e_tests)
  env.add_commands(tools)      -> installs file_editor/search/execute_bash/finish
                                  into /usr/local/bin, as the r2egym agent config does
and then export THAT container into a Zeroboot rootfs (10_image_to_rootfs.sh),
with PATH = R2E's DOCKER_PATH (R2E runs every command with it) and cwd /testbed.

Run with R2E-Gym's venv, as root (docker + loop-free ext4 + zeroboot template):
  sudo -E $R2E_DIR/.venv/bin/python 20_build_r2e_template.py --repo aiohttp
  sudo -E $R2E_DIR/.venv/bin/python 20_build_r2e_template.py --repo pandas --index 3

Writes $TEMPLATES_DIR/<repo>/<commit12>/: rootfs.ext4, snapshot/{mem,vmstate},
manifest.json (image digest, sizes, mem, timings), task.json (the dataset row).
"""

import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
KSM = os.path.dirname(HERE)
R2E_DIR = os.environ.get("R2E_DIR", "/mnt/data/r2e/R2E-Gym")
TEMPLATES_DIR = os.environ.get("TEMPLATES_DIR", "/mnt/data/templates")
# The r2egym agent config's command_files (src/r2egym/agenthub/config/r2egym/edit_fn_calling.yaml)
TOOLS = ["src/r2egym/agenthub/tools/r2egym/file_editor.py",
         "src/r2egym/agenthub/tools/search.py",
         "src/r2egym/agenthub/tools/r2egym/execute_bash.py",
         "src/r2egym/agenthub/tools/finish.py"]
READY = "ZEROBOOT_TEMPLATE_READY"


def run(cmd, env=None):
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, env={**os.environ, **(env or {})})


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", required=True, help="R2E repo_name, e.g. aiohttp")
    ap.add_argument("--index", type=int, default=0, help="n-th task of that repo in dataset order")
    ap.add_argument("--dataset", default="R2E-Gym/R2E-Gym-Lite")
    ap.add_argument("--split", default="train")
    ap.add_argument("--headroom-mb", type=int, default=1536,
                    help="guest RAM on top of the in-RAM root, for running tests")
    ap.add_argument("--keep-container", action="store_true")
    a = ap.parse_args()
    if os.geteuid() != 0:
        sys.exit("run as root (sudo -E ...)")

    sys.path.insert(0, os.path.join(R2E_DIR, "src"))
    os.chdir(R2E_DIR)                       # R2E's tool paths are relative to its checkout
    from datasets import load_dataset
    from r2egym.agenthub.environment.env import EnvArgs, RepoEnv
    from r2egym.agenthub.runtime.docker import DOCKER_PATH

    t0 = time.time()
    ds = load_dataset(a.dataset, split=a.split)
    idx = [i for i, r in enumerate(ds["repo_name"]) if r == a.repo]
    if a.index >= len(idx):
        sys.exit(f"{a.repo}: only {len(idx)} tasks")
    row = ds[idx[a.index]]
    commit = row["commit_hash"]
    out = os.path.join(TEMPLATES_DIR, a.repo, commit[:12])
    os.makedirs(out, exist_ok=True)
    json.dump({**row, "_dataset": a.dataset, "_split": a.split, "_row": idx[a.index],
               "_repo_index": a.index}, open(os.path.join(out, "task.json"), "w"))
    print(f"task {a.repo}#{a.index} (dataset row {idx[a.index]}) commit {commit[:12]} image {row['docker_image']}")

    # --- R2E-Gym's own environment setup, in docker (with network) ---
    t1 = time.time()
    env = RepoEnv(EnvArgs(ds=row), verbose=False)
    env.add_commands(TOOLS)
    cid = env.runtime.container.id
    setup_s = time.time() - t1
    try:
        run(["bash", os.path.join(HERE, "10_image_to_rootfs.sh"), f"container:{cid}", out],
            env={"ENV_PATH": DOCKER_PATH, "WORKDIR_IN_IMAGE": env.runtime.repo_path})
    finally:
        if not a.keep_container:
            env.close()

    man_path = os.path.join(out, "manifest.json")
    man = json.load(open(man_path))
    mem_mib = (int(man["content_mb"] / 0.9) + a.headroom_mb + 255) // 256 * 256

    # --- snapshot: boot, root -> RAM, wait for the agent's ready marker ---
    t2 = time.time()
    run(["bash", os.path.join(KSM, "phase1_zeroboot_cow", "20_make_template.sh")], env={
        "WORKDIR": out, "ROOTFS": os.path.join(out, "rootfs.ext4"), "INIT_PATH": "/zb-init",
        "MEM_MIB": str(mem_mib), "WAIT_SECS": "900", "ZEROBOOT_READY_MARKER": READY,
        "ZEROBOOT_FC_TIMEOUT_SECS": "900"})
    template_s = time.time() - t2
    boot_copy = os.path.join(out, "rootfs.boot.ext4")
    if os.path.exists(boot_copy):
        os.remove(boot_copy)                # Firecracker's mutated copy; not needed after the snapshot
    run([sys.executable, "-I", os.path.join(KSM, "phase1_zeroboot_cow", "tools", "inspect_vmstate.py"),
         os.path.join(out, "snapshot", "vmstate")])

    man.update({
        "task": {"repo": a.repo, "repo_index": a.index, "dataset": a.dataset, "split": a.split,
                 "dataset_row": idx[a.index], "commit": commit, "docker_image": row["docker_image"]},
        "r2e_commit": subprocess.check_output(["git", "-C", R2E_DIR, "rev-parse", "HEAD"], text=True).strip(),
        "tools": TOOLS, "env_path": DOCKER_PATH, "mem_mib": mem_mib, "headroom_mb": a.headroom_mb,
        "seconds": {"r2e_setup": round(setup_s, 1), "template": round(template_s, 1),
                    "total": round(time.time() - t0, 1)},
    })
    json.dump(man, open(man_path, "w"), indent=2)
    subprocess.run(["chown", "-R", f"{os.environ.get('SUDO_USER', 'root')}:", out], check=False)
    print(f"\nTEMPLATE READY: {out}  (content {man['content_mb']} MB, guest RAM {mem_mib} MiB)")


if __name__ == "__main__":
    main()
