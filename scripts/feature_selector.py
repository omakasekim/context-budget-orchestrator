#!/usr/bin/env python3
"""
Zero-dependency interactive terminal checkbox selector for context-budget-orchestrator.

Controls (interactive TTY on macOS/Linux via termios/tty, Windows via msvcrt):
  Up / Down (or k / j) : Move cursor
  Space                : Toggle [x] / [ ]
  a / n / d            : Select All / None / Defaults
  Enter                : Confirm & save to .agent_state/features.json
  q / Esc              : Cancel

Gracefully degrades in non-TTY / agent tool contexts by returning a structured multi-select
payload or applying --preset / --enable / --disable flags non-interactively.
"""

from __future__ import annotations

import datetime
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

FEATURE_CATALOG: List[Dict[str, Any]] = [
    {
        "id": "budget_gates",
        "label": "68% Context Budget & Pre-Task Gates",
        "detail": "inspect-session, gate-check, exact JSONL usage & rot alerts",
        "default": True,
    },
    {
        "id": "auto_plan_waves",
        "label": "AST Auto-Plan & File-Disjoint Parallel Waves",
        "detail": "auto-plan / plan-schedule topological batches & oversized-task splitting",
        "default": True,
    },
    {
        "id": "graph_memory",
        "label": "Typed Context Graph & Subgraph-Pruned Resume",
        "detail": "context_graph.json, memory, record-decision, query-graph, resume-context",
        "default": True,
    },
    {
        "id": "subagent_delegation",
        "label": "Specialist Subagent Delegation & File Leases",
        "detail": "delegate briefs across 8 harnesses + atomic TTL file locks",
        "default": True,
    },
    {
        "id": "log_compression",
        "label": "Auto-Compressed Code Execution Logs",
        "detail": "exec-task + compress-output (70-95% log token savings)",
        "default": True,
    },
    {
        "id": "truth_verification",
        "label": "Ground-Truth Git/Diff & Liveness Referee",
        "detail": "verify shipped, commit-audit (hollow tests), liveness, arbitrate, escalate",
        "default": True,
    },
    {
        "id": "skill_router",
        "label": "Metadata-Only Companion Skill Router",
        "detail": "discover-skills frontmatter routing without body bloat",
        "default": True,
    },
    {
        "id": "security_audit",
        "label": "Security & Bug Audit Engine (30 CWE + OpenAPI + SARIF/HTML)",
        "detail": "audit scan, openapi, diff, --retest-new, 7-Q triage, secret redactor",
        "default": False,
    },
]

PRESETS: Dict[str, List[str]] = {
    "minimal": ["budget_gates", "graph_memory"],
    "standard": [f["id"] for f in FEATURE_CATALOG if f["default"]],
    "security": ["budget_gates", "graph_memory", "truth_verification", "security_audit"],
    "full": [f["id"] for f in FEATURE_CATALOG],
}

VALID_FEATURE_IDS = frozenset(f["id"] for f in FEATURE_CATALOG)


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def default_feature_map() -> Dict[str, bool]:
    return {f["id"]: bool(f["default"]) for f in FEATURE_CATALOG}


def _global_config_path() -> Path:
    cfg_home = os.environ.get("XDG_CONFIG_HOME")
    if cfg_home:
        base = Path(cfg_home).expanduser()
    elif os.name == "nt" and os.environ.get("APPDATA"):
        base = Path(os.environ["APPDATA"])
    else:
        base = Path.home() / ".config"
    return base / "context-budget-orchestrator" / "features.json"


def load_feature_config(state_dir: Path, use_global: bool = False) -> Dict[str, Any]:
    cfg_path = _global_config_path() if use_global else (state_dir / "features.json")
    defaults = default_feature_map()
    if cfg_path.is_file():
        try:
            loaded = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("features"), dict):
                merged = {k: bool(loaded["features"].get(k, v)) for k, v in defaults.items()}
                return {
                    "configured": True,
                    "config_path": str(cfg_path),
                    "preset": loaded.get("preset", "custom"),
                    "features": merged,
                    "enabled_ids": [k for k, v in merged.items() if v],
                    "updated_at": loaded.get("updated_at", ""),
                }
        except (OSError, json.JSONDecodeError):
            pass

    # Pre-populate unconfigured workspace defaults from global user preferences if present
    seed = dict(defaults)
    seed_preset = "standard"
    if not use_global:
        gpath = _global_config_path()
        if gpath.is_file():
            try:
                gloaded = json.loads(gpath.read_text(encoding="utf-8"))
                if isinstance(gloaded, dict) and isinstance(gloaded.get("features"), dict):
                    seed = {k: bool(gloaded["features"].get(k, v)) for k, v in defaults.items()}
                    seed_preset = str(gloaded.get("preset", "custom"))
            except (OSError, json.JSONDecodeError):
                pass

    return {
        "configured": False,
        "config_path": str(cfg_path),
        "preset": seed_preset,
        "features": seed,
        "enabled_ids": [k for k, v in seed.items() if v],
        "updated_at": "",
    }


def save_feature_config(
    state_dir: Path,
    feature_map: Dict[str, bool],
    preset: str = "custom",
    use_global: bool = False,
) -> Dict[str, Any]:
    cfg_path = _global_config_path() if use_global else (state_dir / "features.json")
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    clean_map = {f["id"]: bool(feature_map.get(f["id"], f["default"])) for f in FEATURE_CATALOG}
    now = _utc_now_iso()
    payload = {
        "schema_version": 1,
        "preset": preset,
        "updated_at": now,
        "features": clean_map,
        "enabled_ids": [k for k, v in clean_map.items() if v],
    }
    cfg_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return {
        "configured": True,
        "config_path": str(cfg_path),
        **payload,
    }


def build_agent_multiselect_prompt(current_map: Dict[str, bool]) -> Dict[str, Any]:
    """Return a structured multi-select prompt payload for chat-based CLIs when no TTY is attached."""
    return {
        "question": "Select which context-budget-orchestrator features to enable for this workspace:",
        "multi_select": True,
        "controls_hint": "Use Up/Down to move, Space to toggle, Enter to confirm.",
        "options": [
            {
                "id": f["id"],
                "label": f["label"],
                "detail": f["detail"],
                "selected": bool(current_map.get(f["id"], f["default"])),
            }
            for f in FEATURE_CATALOG
        ],
    }


def _read_key_posix() -> str:
    import select
    import termios
    import tty

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == "\x1b":
            # Wait briefly for escape-sequence continuation so lone Esc does not block
            ready, _, _ = select.select([fd], [], [], 0.05)
            if not ready:
                return "ESC"
            nxt = sys.stdin.read(1)
            if nxt in ("[", "O"):
                ready2, _, _ = select.select([fd], [], [], 0.05)
                if not ready2:
                    return "ESC"
                code = sys.stdin.read(1)
                if code == "A":
                    return "UP"
                if code == "B":
                    return "DOWN"
            return "ESC"
        if ch in ("\r", "\n"):
            return "ENTER"
        if ch == " ":
            return "SPACE"
        if ch == "\x03":
            return "ESC"
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def _read_key_windows() -> str:
    import msvcrt  # type: ignore

    ch = msvcrt.getwch()
    if ch in ("\x00", "\xe0"):
        code = msvcrt.getwch()
        if code == "H":
            return "UP"
        if code == "P":
            return "DOWN"
        return ""
    if ch in ("\r", "\n"):
        return "ENTER"
    if ch == " ":
        return "SPACE"
    if ch in ("\x1b", "\x03"):
        return "ESC"
    return ch


def _read_key() -> str:
    return _read_key_windows() if os.name == "nt" else _read_key_posix()


def run_interactive_tui(initial_map: Dict[str, bool], title: str = "context-budget-orchestrator — Feature Selector") -> Optional[Dict[str, bool]]:
    """
    Launch zero-dependency ANSI checkbox TUI.
    Returns updated {feature_id: bool} on Enter, or None if cancelled via q/Esc.
    """
    selected = dict(initial_map)
    cursor = 0
    total = len(FEATURE_CATALOG)
    rendered_lines = 0

    def draw() -> None:
        nonlocal rendered_lines
        if rendered_lines > 0:
            sys.stdout.write(f"\x1b[{rendered_lines}A\r")
        lines = [
            f"\x1b[1;36m{title}\x1b[0m",
            "\x1b[90m  ↑/↓ (or j/k) move  ·  Space toggle  ·  a all  ·  n none  ·  d defaults  ·  Enter confirm  ·  q cancel\x1b[0m",
            "",
        ]
        for idx, feat in enumerate(FEATURE_CATALOG):
            is_cur = idx == cursor
            is_on = bool(selected.get(feat["id"], False))
            pointer = "\x1b[1;36m❯\x1b[0m" if is_cur else " "
            box = "\x1b[1;32m[✓]\x1b[0m" if is_on else "\x1b[90m[ ]\x1b[0m"
            lbl = f"\x1b[1;97m{feat['label']}\x1b[0m" if is_cur else feat["label"]
            lines.append(f" {pointer} {box} {lbl} \x1b[90m— {feat['detail']}\x1b[0m\x1b[K")
        lines.append("")
        enabled_cnt = sum(1 for v in selected.values() if v)
        lines.append(f"\x1b[36m  Enabled: {enabled_cnt}/{total} feature(s). Press Enter to save & proceed.\x1b[0m\x1b[K")
        sys.stdout.write("\n".join(lines) + "\n")
        sys.stdout.flush()
        rendered_lines = len(lines)

    sys.stdout.write("\x1b[?25l")  # hide cursor
    try:
        draw()
        while True:
            key = _read_key()
            if key in ("UP", "k"):
                cursor = (cursor - 1) % total
            elif key in ("DOWN", "j"):
                cursor = (cursor + 1) % total
            elif key == "SPACE":
                fid = FEATURE_CATALOG[cursor]["id"]
                selected[fid] = not selected.get(fid, False)
            elif key in ("a", "A"):
                for f in FEATURE_CATALOG:
                    selected[f["id"]] = True
            elif key in ("n", "N"):
                for f in FEATURE_CATALOG:
                    selected[f["id"]] = False
            elif key in ("d", "D"):
                selected = default_feature_map()
            elif key == "ENTER":
                return selected
            elif key in ("ESC", "q", "Q"):
                return None
            draw()
    finally:
        sys.stdout.write("\x1b[?25h")  # restore cursor
        sys.stdout.flush()


def configure_features(
    state_dir: Path,
    preset: Optional[str] = None,
    enable: Sequence[str] = (),
    disable: Sequence[str] = (),
    show_only: bool = False,
    non_interactive: bool = False,
    use_global: bool = False,
) -> Dict[str, Any]:
    current = load_feature_config(state_dir, use_global=use_global)
    fmap = dict(current["features"])

    if show_only:
        return {
            "mode": "show",
            **current,
            "prompt_schema": build_agent_multiselect_prompt(fmap),
        }

    # Explicit non-interactive flags take precedence
    if preset or enable or disable:
        chosen_preset = "custom"
        if preset:
            p_key = preset.strip().lower()
            if p_key not in PRESETS:
                raise ValueError(f"Unknown preset '{preset}'. Choose from: {', '.join(sorted(PRESETS.keys()))}")
            fmap = {f["id"]: (f["id"] in PRESETS[p_key]) for f in FEATURE_CATALOG}
            chosen_preset = p_key
        for fid in enable:
            if fid not in VALID_FEATURE_IDS:
                raise ValueError(f"Unknown feature ID '{fid}'. Valid: {', '.join(sorted(VALID_FEATURE_IDS))}")
            fmap[fid] = True
        for fid in disable:
            if fid not in VALID_FEATURE_IDS:
                raise ValueError(f"Unknown feature ID '{fid}'. Valid: {', '.join(sorted(VALID_FEATURE_IDS))}")
            fmap[fid] = False
        saved = save_feature_config(state_dir, fmap, preset=chosen_preset, use_global=use_global)
        return {"mode": "flags", "cancelled": False, **saved}

    # Live TTY -> launch interactive checkbox selector
    if not non_interactive and sys.stdin.isatty() and sys.stdout.isatty():
        chosen = run_interactive_tui(fmap)
        if chosen is None:
            return {"mode": "interactive", "cancelled": True, **current}
        saved = save_feature_config(state_dir, chosen, preset="custom", use_global=use_global)
        return {"mode": "interactive", "cancelled": False, **saved}

    # Non-TTY fallback: return structured multi-select prompt schema so host AI CLI renders native checkboxes
    return {
        "mode": "prompt_required",
        "cancelled": False,
        **current,
        "prompt_schema": build_agent_multiselect_prompt(fmap),
    }


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Interactive checkbox feature selector for context-budget-orchestrator.")
    ap.add_argument("--workspace", default=".")
    ap.add_argument("--global", dest="use_global", action="store_true", help="Read/write global user defaults")
    ap.add_argument("--preset", default=None, choices=sorted(PRESETS.keys()))
    ap.add_argument("--enable", default="")
    ap.add_argument("--disable", default="")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--non-interactive", action="store_true")
    ap.add_argument("--json", action="store_true")
    ns = ap.parse_args()

    res = configure_features(
        state_dir=Path(ns.workspace).expanduser().resolve() / ".agent_state",
        preset=ns.preset,
        enable=[x.strip() for x in ns.enable.split(",") if x.strip()],
        disable=[x.strip() for x in ns.disable.split(",") if x.strip()],
        show_only=ns.show,
        non_interactive=ns.non_interactive,
        use_global=ns.use_global,
    )
    if ns.json:
        print(json.dumps(res, indent=2))
    else:
        print(f"[configure] mode={res['mode']} enabled={len(res['enabled_ids'])}/{len(FEATURE_CATALOG)}: {', '.join(res['enabled_ids'])}")
