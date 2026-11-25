# context-budget-orchestrator

[![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B%20Stdlib%20Only-3B82F6?style=for-the-badge)](scripts/budget_engine.py)
[![Cross-CLI Skill](https://img.shields.io/badge/Agent%20Skill-Claude%20%7C%20Gemini%20%7C%20Codex-10B981?style=for-the-badge)](SKILL.md)
[![License: MIT](https://img.shields.io/badge/License-MIT-8B5CF6?style=for-the-badge)](LICENSE)

A zero-dependency, cross-platform **Agent Skill** that prevents AI coding agents from hitting mid-task context-window truncation, silent quality degradation, or broken partial edits across **Claude Code**, **Gemini CLI**, and **OpenAI Codex CLI**.

## Why This Exists

Long coding sessions degrade well before hitting 100% context utilization:
- Agents lose track of early architectural constraints, re-read files repeatedly, or run out of context mid-refactor leaving half-edited files on disk.
- Restarting a fresh session manually forces the next agent to waste thousands of tokens re-discovering repository state.

`context-budget-orchestrator` enforces a **68% safe context ceiling** and **25,000-token floor** using deterministic local token accounting, topological batch scheduling, automatic oversized-task splitting, pre-task budget gates, and `.agent_state/WORKLOG.md` handoff checkpoints with git drift detection.

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
| `plan-schedule` | Measures live file/directory footprints (skipping binaries, `.git`, `node_modules`, `dist`, etc.), validates DAG dependencies, auto-splits oversized tasks (`__part1`, `__part2`), packs sequential session batches, and writes `.agent_state/schedule.json`. |
| `gate-check` | Re-measures live file footprint before a subtask and outputs `PROCEED` or `HALT_AND_HANDOFF`. |
| `save-worklog` | Captures git anchor (`branch`, `HEAD`, `status`), preserves immutable `objective` and `constraints`, marks completed tasks, writes `.agent_state/WORKLOG.md`, and saves `.agent_state/archive/WORKLOG_<timestamp>.md`. |
| `resume-context` | Loads `.agent_state/WORKLOG.md` + `schedule.json`, detects out-of-band git drift, and returns only the next pending batch and `resume_pointer`. |

## Example Workflow Output

```text
$ python3 scripts/budget_engine.py inspect-session --model claude-sonnet-4-6 --extra-tokens 42000
[inspect-session] status=GREEN | used=42,000/200,000 (21.0%) | remaining=158,000 | safe_ceiling=136,000 | estimator=heuristic | source=none

$ python3 scripts/budget_engine.py plan-schedule --model claude-sonnet-4-6 --used-tokens 42000 --tasks-json '[...]'
[plan-schedule] 4 task(s) across 2 batch(es) (safe_ceiling=136,000, batch1_budget=94,000)
  Batch 1 (~68,400/94,000 tok): t1, t2
  Batch 2 (~51,200/136,000 tok): t3, t4
  Saved: /path/to/repo/.agent_state/schedule.json

$ python3 scripts/budget_engine.py gate-check --task-id t3 --model claude-sonnet-4-6 --used-tokens 118000
[gate-check] HALT_AND_HANDOFF task=t3 | reason=Projected usage (144,100) would breach 68% safe ceiling (136,000).
```

## How `.agent_state/WORKLOG.md` Works

When a handoff occurs, the agent saves a structured checkpoint into `.agent_state/WORKLOG.md` (and a timestamped backup under `.agent_state/archive/`). It stores:
1. Session telemetry and token status
2. Git anchor (`branch`, short `HEAD`, working tree status) for drift detection
3. Immutable objective and constraints preserved across multiple handoffs
4. Completed tasks with files touched and verification results
5. Failed attempts / dead ends so the next session never repeats them
6. Exact `resume_pointer` so the fresh agent bootstraps in a fraction of the token budget

Add `.agent_state/` to your project or global `.gitignore` (note: `install.sh` automatically appends it if `git config --global core.excludesfile` is already configured).

## License

[MIT](LICENSE)
