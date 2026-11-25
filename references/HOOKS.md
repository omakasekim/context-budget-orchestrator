# Multi-Harness Integration & Hooks Guide

`context-budget-orchestrator` works natively across **8+ AI coding harnesses** with zero external dependencies.

## 1. Claude Code (`~/.claude/settings.json`)
Skill path: `~/.claude/skills/context-budget-orchestrator`
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
Skill path: `~/.gemini/skills/context-budget-orchestrator`
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

## 3. OpenAI Codex CLI (`~/.agents/skills/` & `AGENTS.md`)
Skill path: `~/.agents/skills/context-budget-orchestrator`
Append to `~/.codex/AGENTS.md` or your project `AGENTS.md`:
```markdown
- Use `context-budget-orchestrator` (`~/.agents/skills/context-budget-orchestrator/scripts/budget_engine.py`) for multi-file tasks, subagent delegation, and session handoffs.
```

## 4. GitHub Copilot CLI (`~/.copilot/skills/` or `.github/skills/`)
- Global install (`bash install.sh`) links to `~/.copilot/skills/context-budget-orchestrator`.
- Project install (`bash install.sh --project .`) installs into `.github/skills/context-budget-orchestrator`.
- Reload live inside Copilot CLI with `/skills reload` and verify with `/skills list`.

## 5. OpenCode (`~/.config/opencode/skills/`)
- Linked automatically to `${XDG_CONFIG_HOME:-$HOME/.config}/opencode/skills/context-budget-orchestrator`.
- Delegate specialized subagents directly using:
  `python3 scripts/budget_engine.py delegate --task-id <id> --role implementer --harness opencode`

## 6. OpenClaw (`~/.openclaw/skills/`)
- Linked automatically to `~/.openclaw/skills/context-budget-orchestrator`.
- Session transcripts under `~/.openclaw/sessions` (or `OPENCLAW_SESSION`) are auto-detected by `inspect-session`.

## 7. DeepSeek Harness (`dsh` / `~/.agents/skills/`)
- Discovered via `~/.agents/skills/context-budget-orchestrator` (or project `.agents/skills/`).
- Supports `deepseek-v4` (1M), `deepseek-v3.2`, `deepseek-v3`, `deepseek-r1`, and `deepseek/deepseek-v4` provider prefixes, plus `DEEPSEEK_SESSION` / `~/.dsh/sessions`.

## 8. xAI Grok CLI / Grok Build (`grok` / `~/.agents/skills/`)
- Discovered via `~/.agents/skills/context-budget-orchestrator` and `AGENTS.md`.
- Supports `grok-4` (256K), `grok-code-fast` (256K), `grok-3` (131K), `xai/grok-4`, plus `GROK_SESSION` / `~/.grok/sessions`, and non-interactive delegation via `delegate --harness grok` (`grok -p`).
