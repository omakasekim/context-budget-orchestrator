# Native Cross-CLI Hooks & Integration Guide

`scripts/hook_adapter.py` lets you automate real-time context budget advisories inside **Claude Code**, **Gemini CLI**, and **OpenAI Codex CLI** without any third-party packages.

## 1. Claude Code (`~/.claude/settings.json`)

Add a lightweight `PreToolUse` (or `Stop`) hook so Claude Code automatically warns you whenever context utilization enters `YELLOW` (55%+) or `CRITICAL_HANDOFF` (>68%) or when noisy tool outputs trigger a context-rot alert:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Edit|Write|Bash",
        "hooks": [
          {
            "type": "command",
            "command": "python3 ~/.claude/skills/context-budget-orchestrator/scripts/hook_adapter.py --model claude-sonnet-4-6"
          }
        ]
      }
    ]
  }
}
```

## 2. Gemini CLI (`~/.gemini/settings.json`)

Wire `hook_adapter.py` into Gemini CLI's `BeforeTool` hook lifecycle:

```json
{
  "hooks": {
    "BeforeTool": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "python3 ~/.gemini/skills/context-budget-orchestrator/scripts/hook_adapter.py --model gemini-2.5-pro"
          }
        ]
      }
    ]
  }
}
```

## 3. OpenAI Codex CLI (`~/.codex/AGENTS.md` or project `AGENTS.md`)

Codex CLI discovers skills automatically from `~/.agents/skills/context-budget-orchestrator/SKILL.md`. To enforce the budget gate unconditionally across all repos, append this snippet to your global `AGENTS.md`:

```markdown
## Context Budget Discipline
- Before multi-file tasks (>2 files), run `python3 ~/.agents/skills/context-budget-orchestrator/scripts/budget_engine.py auto-plan` or `plan-schedule`.
- Before every subtask, run `gate-check`. On `HALT_AND_HANDOFF`, run `verify-handoff` and `save-worklog`, then stop cleanly.
- On `"resume worklog"`, run `resume-context --json` and read only the returned `focused_subgraph` and `resume_pointer`.
```

## 4. Piping Noisy Test / Build Logs Through `compress-output`

Save 70–95% context tokens when running verbose test suites:

```bash
pytest -vv 2>&1 | python3 ~/.claude/skills/context-budget-orchestrator/scripts/budget_engine.py compress-output --max-lines 60
```
