---
name: context-budget-orchestrator
description: >
  Flagship context-budget, typed Context Graph, and multi-agent wave orchestrator for Claude Code, Gemini CLI, and OpenAI Codex CLI.
  Use when inspecting session token budgets or provider telemetry, detecting context rot or edit thrash loops,
  auto-discovering import dependency schedules (auto-plan) or packing manual task DAGs into token-safe batches and parallel waves,
  enforcing pre-task budget gates and multi-agent file leases, compressing noisy test/build logs, recording immutable architectural decisions,
  verifying handoff integrity, persisting graph-backed checkpoints (.agent_state/context_graph.json + WORKLOG.md), or executing subgraph-pruned cold resumes.
---

# Context Budget Orchestrator (v2.0 Flagship)

Eliminate mid-task context truncation, silent attention degradation, edit thrash loops, multi-agent file collisions, and linear handoff bloat across **Claude Code**, **Gemini CLI**, and **OpenAI Codex CLI** using a zero-dependency Python 3.9+ standard-library control plane.

Resolve `SKILL_DIR` from the directory containing this `SKILL.md`:
```bash
ENGINE="<SKILL_DIR>/scripts/budget_engine.py"
```

## Core Operating Protocol

### 1. Inspect Budget & Rot Signals First
Before starting any multi-file (>2 files), multi-step, or large-codebase task:
```bash
python3 "$ENGINE" inspect-session --model "<active-model>" --json
```
- Uses exact provider-reported `usage` tokens from Claude/Gemini/Codex JSONL transcripts when available, falling back to `tiktoken` or a calibrated code/JSON/prose estimator.
- Inspect `rot_alerts`: if noisy tool output share is high (>45%), pipe subsequent test/build outputs through `compress-output`. If an edit thrash loop is flagged, stop and pivot.

### 2. Discover & Schedule Token-Safe Batches + Parallel Waves
- **Automatic static discovery** (scans Python AST + JS/TS imports to build a real dependency DAG, batch schedule, and file-disjoint `parallel_waves`):
  ```bash
  python3 "$ENGINE" auto-plan --path "src" --action "Refactor" --objective "<goal>" --model "<active-model>" --used-tokens "<used_tokens>" --workspace "." --json
  ```
- **Explicit task scheduling** (validates DAG cycles, measures live disk footprints, auto-splits oversized tasks into `<id>__part1`, `<id>__part2`, computes batches + parallel waves, and writes `.agent_state/schedule.json` + `.agent_state/context_graph.json`):
  ```bash
  python3 "$ENGINE" plan-schedule --model "<active-model>" --used-tokens "<used_tokens>" --workspace "." --tasks-json '<tasks-json>' --json
  ```
- Show the batch + parallel wave breakdown (and any split warnings) to the user before editing code.

### 3. Enforce Pre-Task Gate Checks & Multi-Agent Leases
Before **every** subtask:
1. Run `gate-check`:
   ```bash
   python3 "$ENGINE" gate-check --task-id "<task_id>" --agent-id "<agent_id>" --model "<active-model>" --used-tokens "<used_tokens>" --workspace "." --json
   ```
   - On `HALT_AND_HANDOFF`: **STOP IMMEDIATELY**. Do not start editing. Proceed directly to Rule 5.
2. When dispatching parallel subagents across tasks in the same `parallel_wave`, claim exclusive task + file locks before editing and mark complete after verification:
   ```bash
   python3 "$ENGINE" lease --action claim --task-id "<task_id>" --agent-id "worker-1" --workspace "." --json
   # ... edit and verify ...
   python3 "$ENGINE" lease --action complete --task-id "<task_id>" --agent-id "worker-1" --workspace "." --json
   ```

### 4. Protect Context Hygiene (Decisions + Output Compression)
- **Lock architectural invariants** immediately so later sessions never contradict them:
  ```bash
  python3 "$ENGINE" record-decision --id "ADR-01" --decision "<invariant>" --rationale "<why>" --tasks "<t1,t2>" --files "<path>" --workspace "." --json
  ```
- **Compress verbose logs/diffs** before letting them pollute context:
  ```bash
  <test-or-build-command> 2>&1 | python3 "$ENGINE" compress-output --max-lines 60
  ```

### 5. Pre-Flight Verify & Graph-First Atomic Handoff
Trigger a handoff whenever `inspect-session` returns `CRITICAL_HANDOFF` (>68% used or <25,000 remaining), `gate-check` returns `HALT_AND_HANDOFF`, or Batch 1 completes while Batch 2+ remains:
1. **Verify repository integrity** (checks Python AST syntax, JSON validity, merge conflict markers, and optional test command):
   ```bash
   python3 "$ENGINE" verify-handoff --workspace "." --verify-cmd "<targeted-test-cmd>" --json
   ```
   Fix or revert any broken partial edits until `handoff_ready == true`.
2. **Persist canonical Context Graph + WORKLOG projection + archives**:
   ```bash
   python3 "$ENGINE" save-worklog \
     --objective "<immutable-goal>" \
     --constraints-json '["<constraint>"]' \
     --completed-json '[{"task_id":"t1","summary":"...","files_touched":["src/a.py"],"verification":"tests green"}]' \
     --failed-json '["Avoided X for t2 because ..."]' \
     --resume-pointer "Start task t2 in src/b.py:42" \
     --model "<active-model>" \
     --used-tokens "<used_tokens>" \
     --workspace "." \
     --json
   ```
3. Give the user a 3–5 bullet status summary and tell them to open a fresh session and say: **`resume worklog`**.

### 6. Subgraph-Pruned Cold-Start Resumption
When resuming in a workspace containing `.agent_state/context_graph.json` or `.agent_state/WORKLOG.md`:
1. Run `resume-context` (optionally with `--token-budget 3500` for ultra-tight bootstrap):
   ```bash
   python3 "$ENGINE" resume-context --workspace "." --json
   ```
2. For targeted inspection of a specific downstream task (or Mermaid/DOT visualization):
   ```bash
   python3 "$ENGINE" query-graph --task-id "<task_id>" --depth 2 --token-budget 3000 --workspace "." --json
   ```
3. Check `git_drift_detected` and `active_leases`. Inspect only drifted files if any changed out-of-band.
4. Bootstrap strictly from `focused_subgraph` (objective, constraints, ADRs, direct ancestors, linked dead ends) + `resume_pointer` + `relevant_files`. Never re-scan the whole repository.

## Bundled References & Tools
- Engine CLI (12 subcommands): `scripts/budget_engine.py`
- Cross-CLI Hook Adapter (Claude Code & Gemini CLI): `scripts/hook_adapter.py`
- Unit Test Suite: `scripts/test_budget_engine.py`
- Architecture & Graph Schema: `references/ARCHITECTURE.md`
- Native CLI Hooks Setup: `references/HOOKS.md`
- Copy-Paste Prompt Recipes: `references/PROMPT_RECIPES.md`
- Worklog Projection Template: `references/WORKLOG_TEMPLATE.md`
