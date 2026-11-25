---
name: context-budget-orchestrator
description: >
  Orchestrates context-window budgets and a queryable Context Graph across long-running, multi-file, and multi-step engineering tasks.
  Use when checking session token utilization, decomposing large work into token-safe batches,
  preventing mid-task context truncation or degradation, enforcing pre-task budget gates,
  saving graph-backed handoff checkpoints (.agent_state/context_graph.json + WORKLOG.md), or resuming work cleanly in a fresh session.
---

# Context Budget Orchestrator

Prevent silent context-window degradation, mid-edit truncation, and linear handoff bloat across **Claude Code**, **Gemini CLI**, and **OpenAI Codex CLI** by combining a **68% context budget gate** with a **canonical typed Context Graph** (`.agent_state/context_graph.json`) and a slim human-readable projection (`.agent_state/WORKLOG.md`).

All commands use the bundled zero-dependency Python 3.9+ engine located at `scripts/budget_engine.py` relative to this skill directory (resolve `SKILL_DIR` from the directory containing this `SKILL.md`).

```bash
ENGINE="<SKILL_DIR>/scripts/budget_engine.py"
```

## Why Graph-First Handoffs Beat Flat Markdown Alone
A flat `WORKLOG.md` grows linearly across handoffs, forcing later sessions to re-read unrelated completed branches. `budget_engine.py` stores a typed graph (`objective`, `constraint`, `task`, `file`, `dead_end`, `checkpoint` nodes linked by `DEPENDS_ON`, `READS`, `TOUCHES`, and `WARNS_*` edges) in `.agent_state/context_graph.json` so resuming agents pull **only** the pruned ancestor subgraph for their next task while humans still get a visual Mermaid DAG inside `.agent_state/WORKLOG.md`.

## Non-Negotiable Operating Rules

### 1. Triage & Schedule Before Editing
On any task that touches **more than 2 files**, spans **multiple sequential steps**, or explores a **large codebase**:
1. Inspect the active session budget first:
   ```bash
   python3 "$ENGINE" inspect-session --model "<active-model>" --json
   ```
2. Decompose the request into explicit atomic subtasks (`id`, `title`, `files_to_read`, `expected_output_tokens`, `dependencies`) and schedule them into token-safe session batches (this also initializes `.agent_state/context_graph.json`):
   ```bash
   python3 "$ENGINE" plan-schedule \
     --model "<active-model>" \
     --used-tokens "<used_tokens>" \
     --workspace "." \
     --tasks-json '<json-array-of-tasks>' \
     --json
   ```
3. Present the resulting batch breakdown (including any automatically split tasks and warnings) to the user before modifying code.

### 2. Enforce Pre-Task Gate Checks
Before starting **every** subtask (especially destructive edits, refactors, or long test runs), run:
```bash
python3 "$ENGINE" gate-check \
  --task-id "<task_id>" \
  --model "<active-model>" \
  --used-tokens "<current_used_tokens>" \
  --workspace "." \
  --json
```
- If `decision == "PROCEED"`: execute the subtask and run targeted verification.
- If `decision == "HALT_AND_HANDOFF"`: **STOP IMMEDIATELY**. Do NOT begin editing files for that subtask. Proceed directly to Rule 3.

### 3. Clean Atomic Handoffs & Graph Checkpoints
Trigger a handoff whenever:
- `inspect-session` returns `status == "CRITICAL_HANDOFF"` (`> 68%` utilization or `< 25,000` tokens remaining),
- `gate-check` returns `decision == "HALT_AND_HANDOFF"`, or
- You finish **Batch 1** while **Batch 2+** remains pending.

Before handing off:
1. **Guarantee repository integrity:** Never leave syntax-broken code, half-applied diffs, or unverified edits on disk. Either complete the current atomic step cleanly or revert partial changes before writing the checkpoint.
2. Persist the graph + worklog checkpoint using `save-worklog` (writes `.agent_state/context_graph.json`, `.agent_state/WORKLOG.md` with Mermaid DAG, and timestamped snapshots in `.agent_state/archive/`):
   ```bash
   python3 "$ENGINE" save-worklog \
     --objective "<immutable-goal>" \
     --constraints-json '["<constraint-1>","<constraint-2>"]' \
     --completed-json '[{"task_id":"t1","summary":"...","files_touched":["src/a.py"],"verification":"pytest passed"}]' \
     --failed-json '["Avoided approach X for t2 because ..."]' \
     --resume-pointer "Start task t2 in src/b.py:45 (implement validator and run unit tests)" \
     --model "<active-model>" \
     --used-tokens "<current_used_tokens>" \
     --workspace "." \
     --json
   ```
3. Reply to the user with a concise **3–5 bullet summary** (completed work, verification status, remaining batches, exact resume pointer) and instruct them to open a fresh session and say: **`resume worklog`**.

### 4. Subgraph-Pruned Cold-Start Resumption
When the user says `"resume worklog"` or you start a session in a workspace containing `.agent_state/context_graph.json` or `.agent_state/WORKLOG.md`:
1. Run `resume-context` first (which returns `focused_subgraph` pruned strictly to the next pending batch's upstream dependencies, touched files, and linked dead ends):
   ```bash
   python3 "$ENGINE" resume-context --workspace "." --json
   ```
2. For surgical lookup on a specific downstream task, query the graph directly:
   ```bash
   python3 "$ENGINE" query-graph --task-id "<task_id>" --depth 2 --workspace "." --json
   ```
3. Check `git_drift_detected`. If external commits or uncommitted edits occurred since the last handoff, inspect only the changed files and inform the user before overwriting anything.
4. Bootstrap **ONLY** from `focused_subgraph` + the specific file(s)/line range(s) named in `resume_pointer` and `relevant_files`. Do **NOT** re-scan the entire repository or re-read unrelated completed history.
5. Continue executing the next pending batch using Rule 2 (`gate-check`) before every subtask.

## Reference Artifacts
- Engine CLI: `scripts/budget_engine.py`
- Unit Test Suite: `scripts/test_budget_engine.py`
- Worklog Projection Template: `references/WORKLOG_TEMPLATE.md`
