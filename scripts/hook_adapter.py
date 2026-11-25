#!/usr/bin/env python3
"""
Zero-dependency universal hook adapter for Claude Code and Gemini CLI.

Reads JSON event payload from stdin (e.g., Claude Code PreToolUse / Stop or Gemini CLI BeforeTool),
evaluates real-time context utilization via budget_engine.py, and returns a clean hook JSON advisory
when context utilization approaches or crosses the 68% safe ceiling.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import budget_engine  # noqa: E402


def evaluate_hook_event(payload: Dict[str, Any], model: str | None = None, limit: int | None = None) -> Dict[str, Any]:
    transcript = payload.get("transcript_path") or payload.get("session_path")
    extra_tokens = int(payload.get("extra_tokens") or 0)
    ns = argparse.Namespace(
        model=model or payload.get("model"),
        limit=limit or payload.get("context_limit"),
        transcript=transcript,
        files=None,
        extra_tokens=extra_tokens,
    )
    report = budget_engine.cmd_inspect_session(ns)
    status = report["status"]
    rot_alerts = report.get("rot_alerts", [])

    advisory = None
    if status == "CRITICAL_HANDOFF":
        advisory = (
            f"[context-budget-orchestrator] CRITICAL_HANDOFF ({report['utilization_pct']}% used, "
            f"{report['remaining_tokens']:,} tokens remaining). Stop after the current atomic step, "
            f"run verify-handoff + save-worklog, and instruct the user to resume in a fresh session."
        )
    elif status == "YELLOW" or rot_alerts:
        extra = f" Alerts: {' '.join(rot_alerts)}" if rot_alerts else ""
        advisory = (
            f"[context-budget-orchestrator] YELLOW ({report['utilization_pct']}% used). "
            f"Run gate-check before starting another subtask.{extra}"
        )

    out: Dict[str, Any] = {
        "continue": True,
        "status": status,
        "utilization_pct": report["utilization_pct"],
        "remaining_tokens": report["remaining_tokens"],
        "rot_alerts": rot_alerts,
    }
    if advisory:
        out["systemMessage"] = advisory
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Cross-CLI hook adapter for context-budget-orchestrator.")
    parser.add_argument("--model", default=None, help="Optional model override.")
    parser.add_argument("--limit", type=int, default=None, help="Optional context limit override.")
    args = parser.parse_args()

    raw = sys.stdin.read().strip() if not sys.stdin.isatty() else ""
    try:
        payload = json.loads(raw) if raw else {}
        if not isinstance(payload, dict):
            payload = {}
    except json.JSONDecodeError:
        payload = {}

    response = evaluate_hook_event(payload, model=args.model, limit=args.limit)
    sys.stdout.write(json.dumps(response) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
