# Notes for AI agents working in ksm

Read [`README.md`](README.md) first: status, runbook, what lives where, and the
"Findings and decisions" section. Then [`ROADMAP.md`](ROADMAP.md) → "Immediate
next step". Each phase directory's README has the details.

Rules the user has set:
- **Reproducible on fresh CloudLab nodes.** Every change to `~/zeroboot` becomes a
  numbered patch (`zeroboot-pin/new_patch.sh <name>`). Every node setting becomes a
  script plus a row in README "What provisioning changes". Verify a patch applies
  on a fresh clone: `ZB_DIR=/tmp/x SKIP_BUILD=1 bash zeroboot-pin/setup_zeroboot.sh`.
- **Don't commit**; the user reviews and commits.
- **DeepSeek key:** `~/.config/ksm/deepseek.env`. Never print it, never copy it into
  `ksm/`, never send it anywhere except api.deepseek.com. Recording spends real
  money: always pass `--budget-usd`, and do a 1-trajectory probe before a cohort.

Practical:
- Leave the node as you found it: `tools/ksm_ctl.sh off`, `phase1_zeroboot_cow/30_serve.sh stop`
  (with `WORKDIR=<template dir>`), no stray docker containers.
- Long runs (gate, recording, replay matrix) take 5–30 min: run them in the
  background and record results in the phase README.
