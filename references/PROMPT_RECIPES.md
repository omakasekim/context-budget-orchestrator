# Copy-Paste Prompt Recipes

Use these prompts in **Claude Code**, **Gemini CLI**, or **OpenAI Codex CLI** to get maximum leverage from `context-budget-orchestrator`.

## 1. Large Multi-File Refactor (Auto-Discovery)
> Use `context-budget-orchestrator` to run `auto-plan` on `src/` for migrating our REST handlers to async/await, record any key invariants with `record-decision`, and execute Batch 1 with `gate-check` before each subtask.

## 2. Parallel Multi-Agent Swarm Execution
> Run `plan-schedule` for these 6 modules, inspect `parallel_waves`, and dispatch subagents only for file-disjoint tasks in Wave 1 using `lease --action claim` before editing and `lease --action complete` after verification.

## 3. Safe Pre-Handoff Checkpoint
> Run `verify-handoff --verify-cmd "pytest"`, fix any syntax or test failures, then run `save-worklog` so `.agent_state/context_graph.json` and `.agent_state/WORKLOG.md` are ready for a fresh session.

## 4. Subgraph-Pruned Cold Resume
> Resume worklog: run `resume-context --json`, check `git_drift_detected`, load only `focused_subgraph` + `resume_pointer`, and continue the next pending batch.

## 5. One-Screen Mission Control Check
> Run `budget_engine.py status` and show me current context budget, remaining waves, active leases, and recorded architectural decisions.
