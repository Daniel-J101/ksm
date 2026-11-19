# Phase 3 — record DeepSeek agent traces once, replay many times

A real coding agent (R2E-Gym's own agent loop, DeepSeek Flash) solves each task
**once** in R2E-Gym's own docker runtime. Every container command is recorded
with the wall-clock gap before it. Phase 4 replays those commands into Zeroboot
sandboxes as often as needed, without calling the LLM again (AgentZip §5.3).

## Run

```bash
# one-time per node: DeepSeek key in a private file, outside ksm (never committed/printed)
mkdir -p ~/.config/ksm && ( umask 077; read -rsp 'DeepSeek API key: ' k && printf 'DEEPSEEK_API_KEY=%s\n' "$k" > ~/.config/ksm/deepseek.env; echo; unset k )

source ../env.sh
T=$TEMPLATES_DIR/aiohttp/f0d74880deec            # from phase2_sandbox/20_build_r2e_template.py
sudo -E $R2E_DIR/.venv/bin/python record.py --template-dir $T --mode rollout --n 16 --parallel 16 --budget-usd 3
sudo -E $R2E_DIR/.venv/bin/python record.py --template-dir $T --mode gaf --parallel 4 --budget-usd 2
```

Already-recorded trajectories are skipped (`--force` re-records; `--only 3,7` picks
some). The budget is enforced against the **live DeepSeek balance**, checked
before each launch. Each child also logs its own token-based cost estimate.

## What gets recorded

| Workload | Trajectories / task | Varies |
|---|---|---|
| `rollout` (paper's LLM-training pattern) | 16, run concurrently | temperature 0.2 → 1.2, evenly spaced |
| `gaf` (generate-and-filter, inference) | 4, run concurrently | role prompt + temperature: minimal patch 0.3, failure reproduction 0.6, test-guided repair 0.9, root-cause diagnosis 1.2 |

Agent settings are R2E-Gym's runner defaults: r2egym scaffold
(`edit_fn_calling.yaml`: file_editor, search, execute_bash, finish),
`max_steps=40`, `max_steps_absolute=50`, 64k-token context limit.

R2E code is **wrapped, not edited**:
- **`DockerRuntime.run` / `copy_to_container`:** every container command, with
  start and end time, phase (`setup` = R2E's reset and tool install, already
  baked into the template; `agent` = inside a step; `post` = final diff and
  grading), and its step index.
- **`litellm.completion`:** every LLM call, with latency and tokens (including
  DeepSeek cache hit/miss).

`think_time_ms_before` = end of the previous container command → start of this one.
That covers the LLM call plus agent bookkeeping, which is what replay must wait.

### Function calling (why the model label)

R2E-Gym turns on native function calling only for model names containing
`gpt`/`sonnet`/`o3`/`o4`. In its text/XML mode, deepseek-flash often answers in its
own `<|DSML|>` tool-call markup, which R2E can't parse. In the first probe, **27 of 44
steps were wasted** on "You forgot to use a function call". So R2E sees the label
`gpt-compat/deepseek-flash`, which selects its fn-calling path unchanged. The
recorder's litellm wrapper maps the label to `deepseek/deepseek-flash` and drops the
Anthropic-only `cache_control` markers. Result: 30/30 steps produce a command.
The superseded probe is kept in `$DATA_DIR/traces/_superseded/`.

## Files

| Path | What |
|---|---|
| `ksm/traces/<repo>/<commit12>/<mode>/<k>.json` | **compact trace** (committed; Phase 4's input): `meta` (model, temperature, role, task, template manifest hash, R2E commit, UTC time), `result` (exit reason, reward, steps, tokens, cost estimate), `setup` (what the template already contains), `replay` (per command: `think_time_ms_before`, `code`, `timeout`, `workdir`, recorded exit code, output length and hash), `llm_calls` |
| `$DATA_DIR/traces/.../<k>.full.json.gz` | full R2E `Trajectory` (thoughts, actions, observations), every command's output, LLM calls |
| `$DATA_DIR/traces/.../<k>.log` | the child's R2E log |

Replay uses `ZerobootRuntime.run(code, timeout, args, workdir)` from
`phase2_sandbox/zbsandbox.py`, the same wrapping as R2E's `DockerRuntime.run`. A
recorded exit code and output hash give a per-step fidelity check.

## Results

**Pilot recorded 2026-11-19** (aiohttp#0, `f0d74880deec`, deepseek-flash). Traces are in `traces/aiohttp/f0d74880deec/`.

| | Rollout | GAF |
|---|---|---|
| trajectories | 16 (T 0.2→1.2), all concurrent | 4 roles, concurrent |
| solved (R2E reward 1.0) | **16/16** | **4/4** |
| exit | all `agent` (finished themselves) | all `agent` |
| steps per trajectory | 21–35 (median 30) | 22–37 |
| replayable commands | 463 (every step runs one) | 120 |
| wall time per trajectory | 85–124 s | 80–111 s |
| think time before a command | median 1.6 s, p90 3.2 s | median 1.6 s, p90 3.4 s |
| largest command output | 25.6 KB | 30.0 KB |
| distinct final patches | 8/16 | 3/4 |

- **Diversity:** paths diverge from the first command (median pair differs at
  command 0), no two sequences are identical, and a typical pair shares only ~5% of
  its commands. That matches the paper's "same start, diverging trajectories".
- **Cost:** DeepSeek balance $9.99 → $9.88 for all 20 (the balance updates with a
  lag). The recorder's conservative token-price estimate is $0.28 in total, about
  $0.014 per trajectory, with ~95% of prompt tokens served from DeepSeek's cache.
- **Outputs** are ≤ 30 KB, about 0.3 s over the sandbox serial link, so no
  transport change is needed yet.
