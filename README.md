# context-budget-orchestrator

[![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B%20Stdlib%20Only-3B82F6?style=for-the-badge)](scripts/budget_engine.py)
[![Cross-CLI Skill](https://img.shields.io/badge/Agent%20Skill-Claude%20%7C%20Gemini%20%7C%20Codex-10B981?style=for-the-badge)](SKILL.md)
[![License: MIT](https://img.shields.io/badge/License-MIT-8B5CF6?style=for-the-badge)](LICENSE)

A zero-dependency, cross-platform **Agent Skill** that prevents AI coding agents from hitting mid-task context-window truncation, silent quality degradation, or linear handoff bloat across **Claude Code**, **Gemini CLI**, and **OpenAI Codex CLI**.

## Why Context Graph + Budget Gates Beat Flat Worklogs Alone

Long coding sessions fail in two ways:
1. **Mid-task context exhaustion**: Agents degrade past ~68% utilization and risk leaving broken partial edits when forced to compact or restart.
2. **Linear handoff bloat**: Flat Markdown worklogs grow linearly over 3–6 handoffs, forcing each new session to re-read unrelated completed branches.

`context-budget-orchestrator` solves both:
- Enforces a **68% safe ceiling** and **25,000-token floor** with topological batching, automatic oversized-task splitting, and pre-task gate checks.
- Persists a **canonical typed Context Graph** (`.agent_state/context_graph.json`) linking `objective`, `constraint`, `task`, `file`, `dead_end`, and `checkpoint` nodes (`DEPENDS_ON`, `READS`, `TOUCHES`, `WARNS_*`) so `resume-context` and `query-graph` load **only the pruned ancestor subgraph** needed for the next task.
- Projects a compact human-readable `.agent_state/WORKLOG.md` containing a live **Mermaid dependency graph** and git drift anchor.

## 1-Command Installation

```bash
git clone https://github.com/Aetherx86/context-budget-orchestrator.git
cd context-budget-orchestrator
bash install.sh
```

`install.sh` installs the canonical skill payload to `${XDG_DATA_HOME:-$HOME/.local/share}/agent-skills/context-budget-orchestrator` and creates symlinks for:
- **Claude Code:** `~/.claude/skills/context-budget-orchestrator`
- **Gemini CLI:** `~/.gemini/skills/context-budget-orchestrator`
- **OpenAI Codex CLI:** `~/.agents/skills/context-budget-orchestrator`

### Alternative Modes
- **Copy mode** (for filesystems/Windows environments where symlinks are restricted):
  ```bash
  bash install.sh --copy
  ```
- **Uninstall cleanly:**
  ```bash
  bash install.sh --uninstall
  ```

### Verify Locally
```bash
python3 scripts/test_budget_engine.py
```

## How to Use in Your CLI Agent

Once installed, the skill is discovered automatically via `SKILL.md`:

- **Start a large multi-file task** (Claude Code, Gemini CLI, or Codex CLI):
  > *"Use context-budget-orchestrator to plan and execute the auth middleware migration across `src/auth/` and `tests/`."*
- **Resume cleanly in a fresh session after a handoff:**
  > *"resume worklog"*

## CLI Engine Reference (`scripts/budget_engine.py`)

Zero external dependencies (Python 3.9+ stdlib only; automatically upgrades to `tiktoken` `o200k_base`/`cl100k_base` if installed). Every subcommand supports `--json`.

| Subcommand | Purpose |
| --- | --- |
| `inspect-session` | Auto-detects active Claude/Gemini/Codex session transcripts, calculates utilization %, safe ceiling (68%), and status (`GREEN` / `YELLOW` / `CRITICAL_HANDOFF`). |
| `plan-schedule` | Measures live file/directory footprints (skipping binaries, `.git`, `node_modules`, `dist`, etc.), validates DAG dependencies, auto-splits oversized tasks (`__part1`, `__part2`), packs sequential session batches, and initializes `.agent_state/schedule.json` + `.agent_state/context_graph.json`. |
| `gate-check` | Re-measures live file footprint before a subtask and outputs `PROCEED` or `HALT_AND_HANDOFF`. |
| `save-worklog` | Captures git anchor (`branch`, `HEAD`, `status`), preserves immutable `objective` and `constraints`, marks completed tasks, updates `.agent_state/context_graph.json` + `.agent_state/WORKLOG.md` (with Mermaid DAG), and archives timestamped snapshots. |
| `query-graph` | Extracts a token-pruned upstream subgraph (`--task-id`, `--depth`, `--mermaid`) containing only the ancestor tasks, files, and dead-end warnings relevant to the target task, reporting exact token savings. |
| `resume-context` | Loads the graph-pruned `focused_subgraph`, compares live git `HEAD`/`status` against the saved anchor to flag drift, and returns the exact next pending batch and `resume_pointer`. |

## Example Workflow Output

```text
$ python3 scripts/budget_engine.py inspect-session --model claude-sonnet-4-6 --extra-tokens 42000
[inspect-session] status=GREEN | used=42,000/200,000 (21.0%) | remaining=158,000 | safe_ceiling=136,000 | estimator=heuristic | source=none

$ python3 scripts/budget_engine.py query-graph --task-id auth_b2 --depth 2 --workspace .
[query-graph] targets=['auth_b2'] | pruned=418/1,120 tok (-62.7%)
  relevant_files: src/auth/jwt.py, src/auth/middleware.py
  relevant_dead_ends: 1

$ python3 scripts/budget_engine.py query-graph --task-id auth_b2 --mermaid --workspace .
flowchart LR
  task_auth_b1["auth_b1 [DONE]: Extract JWT verifier"]
  task_auth_b2["auth_b2 [B2]: Wire auth middleware"]
  task_auth_b1 --> task_auth_b2
```

## How `.agent_state/` Works

All runtime state is kept inside `<workspace>/.agent_state/` (gitignored automatically if your global `core.excludesfile` is configured):
- `context_graph.json` — Canonical typed knowledge & execution graph for surgical subgraph-pruned resumes.
- `WORKLOG.md` — Compact human-readable projection with Mermaid DAG, telemetry, git anchor, and exact `resume_pointer`.
- `schedule.json` — Active batch schedule and task completion ledger.
- `archive/` — Timestamped snapshots of every handoff graph and worklog.

## License

[MIT](LICENSE)
