#!/usr/bin/env python3
"""Phase 3: record DeepSeek agent trajectories ONCE, for replay in Zeroboot (Phase 4).

Runs R2E-Gym's own agent loop, unmodified (Agent.run with the r2egym
function-calling scaffold, R2E's runner defaults max_steps=40 /
max_steps_absolute=50), against R2E-Gym's own docker runtime. Recording is
done by wrapping, not editing, R2E code:
  * DockerRuntime.run / copy_to_container -> every container command, with
    wall-clock start/end, phase (setup | agent | post) and the agent step it
    belongs to;
  * litellm.completion -> every LLM call: latency, tokens incl. cache hit/miss.
think_time_ms_before for an agent command = time from the end of the previous
container command to its start: the LLM call plus agent bookkeeping, exactly
what replay must wait (AgentZip paper, Section 5.3).

Workloads (paper Section 5.2), one task, one cohort:
  rollout : N trajectories (default 16), same prompt, temperatures spread over
            --temps (default 0.2..1.2)
  gaf     : 4 trajectories, role prompts (minimal patch, failure reproduction,
            test-guided repair, root-cause diagnosis) x temperatures 0.3/0.6/0.9/1.2

  # API key: ~/.config/ksm/deepseek.env (DEEPSEEK_API_KEY=...), never printed
  sudo -E $R2E_DIR/.venv/bin/python record.py --template-dir $TEMPLATES_DIR/aiohttp/f0d74880deec \\
       --mode rollout --n 1 --budget-usd 1          # measure one trajectory first
  ... --mode rollout --n 16 --parallel 16 --budget-usd 4
  ... --mode gaf --parallel 4 --budget-usd 2

Outputs:
  ksm/traces/<repo>/<commit12>/<mode>/<k>.json     compact trace (committed; replay input)
  $DATA_DIR/traces/<repo>/<commit12>/<mode>/<k>.full.json.gz  R2E Trajectory + all
                                                    observations + logs (data disk)
"""

import argparse
import gzip
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
KSM = os.path.dirname(HERE)
R2E_DIR = os.environ.get("R2E_DIR", "/mnt/data/r2e/R2E-Gym")
DATA_DIR = os.environ.get("DATA_DIR", "/mnt/data")
KEY_FILE = os.path.expanduser(os.environ.get("DEEPSEEK_ENV_FILE",
                              f"~{os.environ.get('SUDO_USER', '')}/.config/ksm/deepseek.env"))
MODEL = "deepseek/deepseek-flash"
# R2E-Gym enables native function calling only for model names containing
# gpt/sonnet/o3/o4 (agent.py:330). DeepSeek supports function calling, and in
# R2E's text-XML mode deepseek-flash often answers in its own <|DSML|> tool-call
# markup, which R2E can't parse (27 of 44 steps wasted in the first probe). So R2E
# is given this label, which turns on its fn-calling path unmodified, and the
# litellm wrapper below maps the label back to MODEL and drops the
# Anthropic-only `cache_control` markers R2E adds (DeepSeek caches prefixes
# automatically).
R2E_LLM_LABEL = "gpt-compat/deepseek-flash"
CONFIG = "src/r2egym/agenthub/config/r2egym/edit_fn_calling.yaml"
# Conservative token prices (USD / 1M) for the running estimate; the DeepSeek
# balance delta is the ground truth and is recorded too.
PRICE = {"input_miss": 0.30, "input_hit": 0.006, "output": 1.20}

GAF_ROLES = [
    ("minimal_patch", 0.3,
     "Approach for this attempt: make the smallest possible change to non-test files that "
     "resolves the issue. Do not refactor or touch unrelated code."),
    ("failure_reproduction", 0.6,
     "Approach for this attempt: first write and run a small script that reproduces the "
     "failure described in the issue, then fix the code and confirm the script now succeeds."),
    ("test_guided_repair", 0.9,
     "Approach for this attempt: locate and run the existing tests most related to the issue, "
     "then iterate on a fix guided by their results."),
    ("root_cause_diagnosis", 1.2,
     "Approach for this attempt: trace the relevant code path and identify the root cause "
     "before editing anything; state the root cause, then fix it."),
]


def load_key():
    for line in open(KEY_FILE):
        if line.startswith("DEEPSEEK_API_KEY="):
            return line.split("=", 1)[1].strip()
    sys.exit(f"no DEEPSEEK_API_KEY in {KEY_FILE}")


def balance_usd(key):
    req = urllib.request.Request("https://api.deepseek.com/user/balance",
                                 headers={"Authorization": f"Bearer {key}"})
    d = json.load(urllib.request.urlopen(req, timeout=30))
    return sum(float(b["total_balance"]) for b in d.get("balance_infos", []) if b["currency"] == "USD")


def sha(s):
    return hashlib.sha256(s.encode("utf-8", "replace")).hexdigest()[:16]


# ============================================================== child: one trajectory

def child(spec_path):
    spec = json.load(open(spec_path))
    os.chdir(R2E_DIR)
    sys.path.insert(0, os.path.join(R2E_DIR, "src"))
    import litellm
    import r2egym.agenthub.runtime.docker as dk
    from r2egym.agenthub.agent.agent import Agent, AgentArgs
    from r2egym.agenthub.environment.env import EnvArgs, RepoEnv

    state = {"phase": "setup", "step": None, "agent_started": False}
    cmds, llm = [], []

    orig_run, orig_copy = dk.DockerRuntime.run, dk.DockerRuntime.copy_to_container

    def run(self, code, timeout=dk.CMD_TIMEOUT, args="", workdir=None, type=None):  # noqa: A002
        t0 = time.time()
        out, err = orig_run(self, code, timeout=timeout, args=args, workdir=workdir, type=type)
        cmds.append({"kind": "run", "phase": state["phase"], "step": state["step"],
                     "t_start": t0, "t_end": time.time(), "code": code, "timeout": timeout,
                     "args": args, "workdir": workdir, "error_code": str(err),
                     "output_len": len(out), "output_sha": sha(out), "output": out})
        return out, err

    def copy(self, src_path, dest_path):
        t0 = time.time()
        r = orig_copy(self, src_path, dest_path)
        data = open(src_path, "rb").read() if os.path.isfile(src_path) else b""
        cmds.append({"kind": "copy", "phase": state["phase"], "step": state["step"],
                     "t_start": t0, "t_end": time.time(), "src": os.path.relpath(src_path, R2E_DIR),
                     "dest": dest_path, "bytes": len(data), "sha": hashlib.sha256(data).hexdigest()[:16]})
        return r

    dk.DockerRuntime.run, dk.DockerRuntime.copy_to_container = run, copy

    orig_step = RepoEnv.step

    def step(self, action, timeout=None):
        state["phase"], state["step"] = "agent", len({c["step"] for c in cmds if c["phase"] == "agent"})
        state["agent_started"] = True
        try:
            return orig_step(self, action, timeout=timeout)
        finally:
            state["phase"], state["step"] = "post", None   # anything outside a step after this is post
    RepoEnv.step = step

    orig_completion = litellm.completion

    def strip_cc(x):
        if isinstance(x, dict):
            return {k: strip_cc(v) for k, v in x.items() if k != "cache_control"}
        if isinstance(x, list):
            return [strip_cc(v) for v in x]
        return x

    def completion(*a, **kw):
        if kw.get("model") == R2E_LLM_LABEL:
            kw["model"] = MODEL
        for k in ("messages", "tools"):
            if kw.get(k) is not None:
                kw[k] = strip_cc(kw[k])
        t0 = time.time()
        rec = {"t_start": t0, "model": kw.get("model"), "temperature": kw.get("temperature")}
        try:
            r = orig_completion(*a, **kw)
        except Exception as e:  # noqa: BLE001
            rec.update(t_end=time.time(), error=repr(e)[:500]); llm.append(rec); raise
        u = getattr(r, "usage", None)
        details = getattr(u, "completion_tokens_details", None)
        rec.update(t_end=time.time(), served_model=getattr(r, "model", None),
                   prompt_tokens=getattr(u, "prompt_tokens", 0), completion_tokens=getattr(u, "completion_tokens", 0),
                   cache_hit=getattr(u, "prompt_cache_hit_tokens", 0) or 0,
                   cache_miss=getattr(u, "prompt_cache_miss_tokens", 0) or 0,
                   reasoning_tokens=getattr(details, "reasoning_tokens", 0) if details else 0)
        llm.append(rec)
        return r
    litellm.completion = completion

    # ---- the R2E run itself
    task = json.load(open(spec["task_json"]))
    row = {k: v for k, v in task.items() if not k.startswith("_")}
    args = AgentArgs.from_yaml(CONFIG)
    args.llm_name = R2E_LLM_LABEL
    args.llm_base_url = None                       # default points at localhost:8000
    if spec.get("role_prompt"):
        args.instance_prompt = spec["role_prompt"] + "\n\n" + args.instance_prompt
    env = RepoEnv(EnvArgs(ds=row), verbose=False)
    agent = Agent(name=f"zb-{spec['mode']}-{spec['k']}", args=args)
    t_run = time.time()
    traj = agent.run(env, max_steps=spec["max_steps"], max_steps_absolute=spec["max_steps_absolute"],
                     temperature=spec["temperature"], use_fn_calling=True, scaffold="r2egym")
    state["phase"], state["step"] = "post", None
    reward = None
    try:
        reward = env.compute_reward()
    except Exception as e:  # noqa: BLE001
        print("reward failed:", repr(e))
    env.close()
    t_done = time.time()

    # ---- compact trace for replay
    agent_cmds = [c for c in cmds if c["phase"] == "agent"]
    prev_end = max([c["t_end"] for c in cmds if c["phase"] == "setup"] or [t_run])
    replay = []
    for c in agent_cmds:
        replay.append({"step": c["step"], "think_time_ms_before": round((c["t_start"] - prev_end) * 1000, 1),
                       "code": c["code"], "timeout": c["timeout"], "args": c["args"], "workdir": c["workdir"],
                       "rec_exec_ms": round((c["t_end"] - c["t_start"]) * 1000, 1),
                       "rec_error_code": c["error_code"], "rec_output_len": c["output_len"],
                       "rec_output_sha": c["output_sha"]})
        prev_end = c["t_end"]
    tok = {k: sum(x.get(k, 0) or 0 for x in llm) for k in
           ("prompt_tokens", "completion_tokens", "cache_hit", "cache_miss", "reasoning_tokens")}
    est = (tok["cache_miss"] * PRICE["input_miss"] + tok["cache_hit"] * PRICE["input_hit"]
           + tok["completion_tokens"] * PRICE["output"]) / 1e6
    compact = {
        "format": "ksm-trace-v1",
        "meta": {**{k: spec[k] for k in ("mode", "k", "cohort", "temperature", "role", "max_steps",
                                          "max_steps_absolute")},
                 "model": MODEL, "served_models": sorted({x.get("served_model") or "?" for x in llm}),
                 "scaffold": "r2egym/fn_calling", "r2e_config": CONFIG, "r2e_llm_label": R2E_LLM_LABEL,
                 "r2e_commit": subprocess.check_output(["git", "-C", R2E_DIR, "rev-parse", "HEAD"], text=True).strip(),
                 "task": spec["task"], "template_manifest_sha": spec["template_manifest_sha"],
                 "recorded_utc": datetime.fromtimestamp(t_run, timezone.utc).isoformat(timespec="seconds"),
                 "recorder": "ksm/phase3_traces/record.py"},
        "result": {"exit_reason": traj.exit_reason, "reward": reward, "n_steps": len(traj.trajectory_steps),
                   "n_llm_calls": len(llm), "llm_errors": sum(1 for x in llm if "error" in x),
                   "wall_s": round(t_done - t_run, 1),
                   "llm_s": round(sum(x["t_end"] - x["t_start"] for x in llm), 1),
                   "tokens": tok, "cost_usd_est": round(est, 4),
                   "output_patch_sha": sha(traj.output_patch or ""), "output_patch_len": len(traj.output_patch or "")},
        "setup": [{k: c[k] for k in ("kind", "code", "src", "dest", "error_code") if k in c}
                  for c in cmds if c["phase"] == "setup"],
        "replay": replay,
        "llm_calls": [{k: v for k, v in x.items()} for x in llm],
    }
    os.makedirs(os.path.dirname(spec["out_compact"]), exist_ok=True)
    json.dump(compact, open(spec["out_compact"], "w"), indent=1)
    os.makedirs(os.path.dirname(spec["out_full"]), exist_ok=True)
    with gzip.open(spec["out_full"], "wt") as f:
        json.dump({"spec": spec, "trajectory": json.loads(traj.model_dump_json()), "commands": cmds,
                   "llm_calls": llm, "output_patch": traj.output_patch, "reward": reward}, f)
    print(f"DONE {spec['mode']}#{spec['k']}: {compact['result']}")


# ============================================================== parent: plan + launch

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--child", help=argparse.SUPPRESS)
    ap.add_argument("--template-dir")
    ap.add_argument("--mode", choices=("rollout", "gaf"))
    ap.add_argument("--n", type=int, default=None, help="rollout: trajectories (16); gaf: roles (4)")
    ap.add_argument("--only", default="", help="comma list of k to (re)run, e.g. 0 or 3,7")
    ap.add_argument("--temps", default="0.2,1.2", help="rollout temperature range lo,hi")
    ap.add_argument("--parallel", type=int, default=4)
    ap.add_argument("--budget-usd", type=float, required=False, default=None,
                    help="stop launching when the DeepSeek balance has dropped this much")
    ap.add_argument("--min-balance-usd", type=float, default=1.0)
    ap.add_argument("--max-steps", type=int, default=40)
    ap.add_argument("--max-steps-absolute", type=int, default=50)
    ap.add_argument("--force", action="store_true", help="re-record trajectories that already exist")
    a = ap.parse_args()
    if a.child:
        return child(a.child)
    if not (a.template_dir and a.mode and a.budget_usd is not None):
        ap.error("--template-dir, --mode and --budget-usd are required")
    if os.geteuid() != 0:
        sys.exit("run as root (sudo -E ...): R2E-Gym drives docker")

    key = load_key()
    T = os.path.abspath(a.template_dir)
    man_raw = open(os.path.join(T, "manifest.json"), "rb").read()
    man = json.loads(man_raw)
    task = man["task"]
    cohort = f"{task['repo']}-{task['commit'][:12]}-{a.mode}"
    rel = os.path.join(task["repo"], task["commit"][:12], a.mode)
    if a.mode == "rollout":
        n = a.n or 16
        lo, hi = (float(x) for x in a.temps.split(","))
        specs = [{"k": k, "temperature": round(lo + (hi - lo) * k / max(1, n - 1), 3), "role": None,
                  "role_prompt": None} for k in range(n)]
    else:
        roles = GAF_ROLES[:a.n or 4]
        specs = [{"k": k, "temperature": t, "role": r, "role_prompt": p} for k, (r, t, p) in enumerate(roles)]
    if a.only:
        keep = {int(x) for x in a.only.split(",")}
        specs = [s for s in specs if s["k"] in keep]
    run_dir = os.path.join(DATA_DIR, "traces", rel)
    os.makedirs(run_dir, exist_ok=True)
    for s in specs:
        s.update(mode=a.mode, cohort=cohort, task=task, task_json=os.path.join(T, "task.json"),
                 template_manifest_sha=hashlib.sha256(man_raw).hexdigest()[:16],
                 max_steps=a.max_steps, max_steps_absolute=a.max_steps_absolute,
                 out_compact=os.path.join(KSM, "traces", rel, f"{s['k']:02d}.json"),
                 out_full=os.path.join(run_dir, f"{s['k']:02d}.full.json.gz"))
    todo = [s for s in specs if a.force or not os.path.exists(s["out_compact"])]
    print(f"cohort {cohort}: {len(specs)} planned, {len(todo)} to record "
          f"(temps {[s['temperature'] for s in todo]}), parallel {a.parallel}, budget ${a.budget_usd}")

    b0 = balance_usd(key)
    print(f"DeepSeek balance at start: ${b0:.2f}")
    env = {**os.environ, "DEEPSEEK_API_KEY": key, "LITELLM_LOG": "ERROR"}
    running, launched, stopped = [], 0, None
    while todo or running:
        still = []
        for s, p, f in running:
            if p.poll() is None:
                still.append((s, p, f))
            else:
                f.close()
                print(f"  finished {a.mode}#{s['k']} (exit {p.returncode})", flush=True)
        running = still
        if todo and len(running) < a.parallel and not stopped:
            bal = balance_usd(key)
            if b0 - bal >= a.budget_usd:
                stopped = f"budget reached (spent ${b0 - bal:.2f})"
            elif bal < a.min_balance_usd:
                stopped = f"balance ${bal:.2f} below minimum"
            else:
                s = todo.pop(0)
                sp = os.path.join(run_dir, f"{s['k']:02d}.spec.json")
                json.dump(s, open(sp, "w"))
                log = open(os.path.join(run_dir, f"{s['k']:02d}.log"), "w")
                p = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--child", sp],
                                     stdout=log, stderr=subprocess.STDOUT, env=env)
                running.append((s, p, log)); launched += 1
                print(f"  launched {a.mode}#{s['k']} T={s['temperature']} role={s['role']} (balance ${bal:.2f})", flush=True)
                continue
        if stopped and todo:
            print(f"  NOT launching {len(todo)} more: {stopped}", flush=True)
            todo = []
        time.sleep(5)
    b1 = balance_usd(key)
    done = [s for s in specs if os.path.exists(s["out_compact"])]
    print(f"\nrecorded {len(done)}/{len(specs)}; spent ${b0 - b1:.3f} (balance ${b1:.2f})")
    for s in done:
        r = json.load(open(s["out_compact"]))["result"]
        print(f"  #{s['k']:02d} T={s['temperature']} {s['role'] or '':22} steps={r['n_steps']:2} "
              f"exit={r['exit_reason']:<16} reward={r['reward']} wall={r['wall_s']}s llm={r['llm_s']}s "
              f"est=${r['cost_usd_est']}")
    for s in specs:
        log_path = os.path.join(run_dir, f"{s['k']:02d}.log")
        if s not in done and os.path.exists(log_path):
            print(f"  #{s['k']:02d} FAILED -- see {log_path}")
    if os.environ.get("SUDO_USER"):
        subprocess.run(["chown", "-R", f"{os.environ['SUDO_USER']}:", os.path.join(KSM, "traces"), run_dir],
                       check=False)


if __name__ == "__main__":
    main()
