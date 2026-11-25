---
name: context-budget-orchestrator
description: >
  Universal zero-dependency AI orchestrator skill for long-horizon projects (minutes to hours) across
  Claude Code, Gemini CLI, OpenAI Codex CLI, GitHub Copilot CLI, OpenClaw, OpenCode, DeepSeek Harness, and Grok CLI.
  Use to research topics and organize findings into persistent graph memory, plan dependency-ordered batches and file-disjoint parallel waves,
  write and execute verified code with auto-compressed logs, route to installed companion skills without context bloat,
  delegate scoped subagent briefs with atomic file leases, enforce 68% context budget gates, and perform subgraph-pruned cold resumes.
license: MIT
---

# Context Budget Orchestrator (v3.4 Universal)

Manage complex multi-file and multi-hour engineering projects cleanly across **Claude Code, Gemini CLI, Codex CLI, Copilot CLI, OpenClaw, OpenCode, DeepSeek Harness (`dsh`), and Grok CLI** without context degradation, edit collisions, or linear handoff bloat.

Resolve `SKILL_DIR` from the folder containing this `SKILL.md`:
```bash
ENGINE="<SKILL_DIR>/scripts/budget_engine.py"
```

## Six-Pillar Operating Loop

### 0. Automatic Interactive Feature Picker (First Run / On Demand)
- Before starting a new workspace task (or when asked to pick/change features), check `python3 "$ENGINE" configure --show --workspace "." --json`.
- If `configured` is `false`, automatically prompt the user with tick boxes (`↑`/`↓` move, `Space` toggle, `Enter` confirm):
  - In a live terminal TTY, run `python3 "$ENGINE" configure --workspace "."` to launch the zero-dependency ANSI checkbox TUI.
  - In a chat-based CLI without raw TTY, present `prompt_schema` using the host's native multi-select checkbox prompt, then save their choices via `python3 "$ENGINE" configure --enable "<csv>" --disable "<csv>" --workspace "." --json`.
- Honor `.agent_state/features.json` throughout the session (skip disabled modules).

### 1. Inspect Budget, Route Skills & Organize Research
- Check active context utilization & context-rot alerts first:
  ```bash
  python3 "$ENGINE" inspect-session --model "<active-model>" --json
  ```
- Before loading extra skills, route via metadata-only discovery (prevents loading unused skill bodies):
  ```bash
  python3 "$ENGINE" discover-skills --query "<topic-or-domain>" --workspace "." --json
  ```
- When researching APIs, docs, or codebases, store structured findings in the canonical graph so they persist across sessions:
  ```bash
  python3 "$ENGINE" memory --action add --kind research --title "<topic>" --content "<concise-findings>" --source "<url-or-file>" --tags "<tag1,tag2>" --tasks "<task_id>" --workspace "." --json
  ```
- Lock architectural invariants with `record-decision --id ADR-01 --decision "<rule>" --rationale "<why>"`.

### 2. Plan Dependency Batches & File-Disjoint Parallel Waves
- Auto-discover local Python/JS/TS import graphs into topological batches + parallel waves:
  ```bash
  python3 "$ENGINE" auto-plan --path "src" --action "Refactor" --objective "<goal>" --model "<active-model>" --used-tokens "<used_tokens>" --workspace "." --json
  ```
- Or supply an explicit task DAG (`id`, `title`, `role`, `files_to_read`, `expected_output_tokens`, `dependencies`) via `plan-schedule --tasks-json '<json>'` (auto-splits oversized tasks into `__part1`, `__part2`, ...).

### 3. Delegate to Specialized Subagents Safely
For tasks in a file-disjoint `parallel_wave` (or distinct roles: `researcher`, `architect`, `implementer`, `reviewer`, `verifier`):
```bash
python3 "$ENGINE" delegate --task-id "<task_id>" --role "<role>" --harness "<claude|gemini|codex|opencode|openclaw|copilot|deepseek|grok>" --workspace "." --json
```
This atomically claims a file-conflict `lease`, prunes `.agent_state/context_graph.json` down to a tight `--token-budget` brief, recommends up to 2 companion skills, writes `.agent_state/delegations/<task>_<role>.md`, and returns the exact cross-harness dispatch command.

### 4. Write & Execute Code Behind Budget Gates
Before every subtask, run:
```bash
python3 "$ENGINE" gate-check --task-id "<task_id>" --model "<active-model>" --used-tokens "<used_tokens>" --workspace "." --json
```
- On `HALT_AND_HANDOFF`: **STOP immediately** and execute Pillar 5.
- On `PROCEED`: edit only scoped files, then run builds/tests via `exec-task` (automatically compresses verbose logs by 70–95% and records an execution receipt in the graph):
  ```bash
  python3 "$ENGINE" exec-task --task-id "<task_id>" --cmd "<build-or-test-cmd>" --workspace "." --json
  ```
- Complete the task lease when verified: `lease --action complete --task-id "<task_id>" --agent-id "<agent_id>"`.
- For security/bug audits, use `audit` (`init-scope` / `--scope-nl`, 30-CWE `scan` + `--rules-file` + `--retest-new` + `--formats json,sarif,html,junit,csv,md`, `openapi` contract audit, `diff`, 7-Question `triage`, and `redact`; see `references/SECURITY_AUDIT.md`).
- Never trust self-reported "done" claims: verify with `verify` (`shipped` checks git ancestry exit 0/1, `commit-audit` catches hollow tests & claim-vs-diff lies, `liveness` catches spin loops, `arbitrate` enforces file/dependency locks, `escalate` queues human decisions; see `references/TRUTH_VERIFICATION.md`).

### 5. Pre-Flight Verify & Graph-First Atomic Handoff (Multi-Hour Continuity)
Whenever `status == "CRITICAL_HANDOFF"` (>68% used or <25,000 remaining), `gate-check` halts, or Batch 1 finishes while Batch 2+ remains:
1. Run `verify-handoff --workspace "." --verify-cmd "<targeted-test-cmd>" --json` — never hand off syntax-broken code or conflict markers.
2. Persist epoch-tracked `.agent_state/context_graph.json`, `.agent_state/WORKLOG.md` (with Mermaid DAG), and timestamped archives via `save-worklog`.
3. Give the user a 3–5 bullet summary and instruct them to open a fresh session and say **`resume worklog`**.

### 6. Subgraph-Pruned Cold Resume
On `"resume worklog"` (or whenever `.agent_state/context_graph.json` exists):
1. Run `python3 "$ENGINE" resume-context --workspace "." --json` (or `query-graph --task-id <id> --token-budget 3000 --json`).
2. Check `git_drift_detected` and `active_leases`.
3. Bootstrap **strictly** from `focused_subgraph` (objective, constraints, ADRs, linked research/memory, dead ends, direct ancestors) + `resume_pointer` without re-scanning the entire repository.

## References
- Engine: `scripts/budget_engine.py` · Hooks: `scripts/hook_adapter.py` · Tests: `scripts/test_budget_engine.py`
- Docs: `references/ARCHITECTURE.md`, `references/HOOKS.md`, `references/PROMPT_RECIPES.md`, `references/SECURITY_AUDIT.md`, `references/TRUTH_VERIFICATION.md`, `references/WORKLOG_TEMPLATE.md`
