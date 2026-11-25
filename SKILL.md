---
name: context-budget-orchestrator
description: >
  Orchestrates context-window budgets across long-running, multi-file, and multi-step engineering tasks.
  Use when checking session token utilization, decomposing large work into token-safe batches,
  preventing mid-task context truncation or degradation, enforcing pre-task budget gates,
  saving condensed handoff worklogs to .agent_state/WORKLOG.md, or resuming work cleanly in a fresh session.
---

# Context Budget Orchestrator

Prevent silent context-window degradation, mid-edit truncation, and lost progress across **Claude Code**, **Gemini CLI**, and **OpenAI Codex CLI** by treating context capacity as a hard engineering budget.

All commands use the bundled zero-dependency Python 3.9+ engine located at `scripts/budget_engine.py` relative to this skill directory (resolve `SKILL_DIR` from the directory containing this `SKILL.md`).

```bash
ENGINE="<SKILL_DIR>/scripts/budget_engine.py"
```

## Non-Negotiable Operating Rules

### 1. Triage & Schedule Before Editing
On any task that touches **more than 2 files**, spans **multiple sequential steps**, or explores a **large codebase**:
1. Inspect the active session budget first:
   ```bash
   python3 "$ENGINE" inspect-session --model "<active-model>" --json
   ```
2. Decompose the request into explicit atomic subtasks (`id`, `title`, `files_to_read`, `expected_output_tokens`, `dependencies`) and schedule them into token-safe session batches:
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

### 3. Clean Atomic Handoffs & Worklog Checkpoints
Trigger a handoff whenever:
- `inspect-session` returns `status == "CRITICAL_HANDOFF"` (`> 68%` utilization or `< 25,000` tokens remaining),
- `gate-check` returns `decision == "HALT_AND_HANDOFF"`, or
- You finish **Batch 1** while **Batch 2+** remains pending.

Before handing off:
1. **Guarantee repository integrity:** Never leave syntax-broken code, half-applied diffs, or unverified edits on disk. Either complete the current atomic step cleanly or revert partial changes before writing the worklog.
2. Persist the checkpoint using `save-worklog` (which writes `.agent_state/WORKLOG.md` following `references/WORKLOG_TEMPLATE.md` and archives a timestamped snapshot in `.agent_state/archive/`):
   ```bash
   python3 "$ENGINE" save-worklog \
     --objective "<immutable-goal>" \
     --constraints-json '["<constraint-1>","<constraint-2>"]' \
     --completed-json '[{"task_id":"t1","summary":"...","files_touched":["src/a.py"],"verification":"pytest passed"}]' \
     --failed-json '["Avoided approach X because ..."]' \
     --resume-pointer "Start task t2 in src/b.py:45 (implement validator and run unit tests)" \
     --model "<active-model>" \
     --used-tokens "<current_used_tokens>" \
     --workspace "." \
     --json
   ```
3. Reply to the user with a concise **3–5 bullet summary** (completed work, verification status, remaining batches, exact resume pointer) and instruct them to open a fresh session and say: **`resume worklog`**.

### 4. Token-Lean Cold-Start Resumption
When the user says `"resume worklog"` or you start a session in a workspace containing `.agent_state/WORKLOG.md`:
1. Run `resume-context` first:
   ```bash
   python3 "$ENGINE" resume-context --workspace "." --json
   ```
2. Check `git_drift_detected`. If external commits or uncommitted edits occurred since the last handoff, inspect only the changed files and inform the user before overwriting anything.
3. Read **ONLY** `.agent_state/WORKLOG.md` and the specific file(s)/line range(s) named in `resume_pointer` plus the files required for the next pending task. Do **NOT** re-scan or re-read the entire repository.
4. Continue executing the next pending batch using Rule 2 (`gate-check`) before every subtask.

## Reference Artifacts
- Engine CLI: `scripts/budget_engine.py`
- Unit Test Suite: `scripts/test_budget_engine.py`
- Worklog Schema Template: `references/WORKLOG_TEMPLATE.md`
