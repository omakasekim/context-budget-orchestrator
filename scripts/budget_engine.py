#!/usr/bin/env python3
"""
context-budget-orchestrator v3.0 (Universal Multi-Harness Flagship Edition)
Zero-dependency Context Budget, Typed Memory Graph, Subagent Delegation, Skill Router & Execution Engine.

Requires Python 3.9+ standard library only (automatically upgrades to `tiktoken` if installed).
Works natively across Claude Code, Gemini CLI, OpenAI Codex CLI, GitHub Copilot CLI,
OpenClaw, OpenCode, DeepSeek Harness (dsh), and xAI Grok CLI.
"""

from __future__ import annotations

import argparse
import ast
import datetime
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

# Optional tiktoken support with zero hard dependency
try:
    import tiktoken  # type: ignore
except ImportError:
    tiktoken = None  # type: ignore

ENGINE_VERSION = "3.2.0"
DEFAULT_CONTEXT_LIMIT = 200_000
SAFE_UTILIZATION_RATIO = 0.68
YELLOW_UTILIZATION_PCT = 55.0
CRITICAL_UTILIZATION_PCT = 68.0
MIN_REMAINING_TOKENS = 25_000
OVERHEAD_MULTIPLIER = 1.35
MIN_BATCH1_BUDGET = 10_000
DEFAULT_LEASE_TTL_SECONDS = 1800

IGNORED_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        "target",
        "dist",
        "build",
        ".next",
        ".agent_state",
        ".mypy_cache",
        ".pytest_cache",
        "coverage",
    }
)

MODEL_CONTEXT_WINDOWS: Dict[str, int] = {
    # Anthropic Claude
    "claude-opus-4-6": 200_000,
    "claude-sonnet-4-6": 200_000,
    "claude-haiku-4-5": 200_000,
    "claude-opus-4-1": 200_000,
    "claude-sonnet-4-5": 200_000,
    "claude-sonnet-4": 200_000,
    "claude-3-7-sonnet": 200_000,
    "claude-3-5-sonnet": 200_000,
    "claude-3-5-haiku": 200_000,
    # Google Gemini
    "gemini-3.1-pro": 1_048_576,
    "gemini-3-pro": 1_048_576,
    "gemini-3-flash": 1_048_576,
    "gemini-2.5-pro": 1_048_576,
    "gemini-2.5-flash": 1_048_576,
    "gemini-2.0-flash": 1_048_576,
    "gemini-1.5-pro": 2_097_152,
    # OpenAI / Codex / Copilot
    "gpt-5-codex": 400_000,
    "gpt-5": 400_000,
    "gpt-4.1": 1_047_576,
    "gpt-4.1-mini": 1_047_576,
    "gpt-4.1-nano": 1_047_576,
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "o3": 200_000,
    "o3-mini": 200_000,
    "o4-mini": 200_000,
    # DeepSeek (V4 / V3 / R1 / Harness)
    "deepseek-v4": 1_000_000,
    "deepseek-v3.2": 128_000,
    "deepseek-v3": 128_000,
    "deepseek-r1": 128_000,
    "deepseek-reasoner": 128_000,
    "deepseek-chat": 128_000,
    # xAI Grok (Grok CLI / Grok Build)
    "grok-4": 256_000,
    "grok-code-fast": 256_000,
    "grok-3": 131_072,
    "grok-3-mini": 131_072,
    # Local / Multi-Provider Routers (Ollama, LM Studio, Groq, Mistral, OpenRouter, LiteLLM)
    "llama-3.3": 128_000,
    "llama3.3": 128_000,
    "qwen-2.5-coder": 128_000,
    "qwen2.5-coder": 128_000,
    "qwen3": 128_000,
    "mistral-large": 128_000,
    "codestral": 256_000,
    "gemma3": 128_000,
}

SPECIALIST_ROLES: Dict[str, str] = {
    "researcher": "Investigate docs, APIs, and codebase patterns; record concise verified findings via `memory --action add --kind research` without modifying production code.",
    "architect": "Design interfaces, dependency boundaries, and trade-offs; lock invariants with `record-decision` before implementation begins.",
    "implementer": "Implement only the scoped files for this task cleanly; avoid touching unrelated modules and verify syntax/tests before returning.",
    "reviewer": "Audit diffs for security, correctness, edge cases, and regressions; report concrete file:line findings.",
    "verifier": "Execute build, lint, and test suites via `exec-task` (auto-compressed logs) and certify atomic completion.",
}

HARNESS_DISPATCH_TEMPLATES: Dict[str, str] = {
    "claude": 'claude -p "$(cat {brief_file})"',
    "gemini": 'gemini -p "$(cat {brief_file})"',
    "codex": 'codex exec "$(cat {brief_file})"',
    "opencode": 'opencode run "$(cat {brief_file})"',
    "openclaw": 'openclaw agent run --prompt-file "{brief_file}"',
    "copilot": 'gh copilot -p "$(cat {brief_file})"',
    "deepseek": 'dsh run --prompt-file "{brief_file}"',
    "grok": 'grok -p "$(cat {brief_file})"',
}

TOKEN_PATTERN = re.compile(r"\w+|[^\w\s]|\n")
SESSION_EXTENSIONS = frozenset({".jsonl", ".json", ".log", ".md", ".txt"})
CODE_EXTENSIONS = frozenset({".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".go", ".rs"})
CONFLICT_MARKER_RE = re.compile(r"^(<<<<<<< |=======$|>>>>>>> )", re.MULTILINE)


# ---------------------------------------------------------------------------
# Time, Model, and Token Estimation Primitives
# ---------------------------------------------------------------------------

def utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def utc_now_iso() -> str:
    return utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_now_compact() -> str:
    return utc_now().strftime("%Y%m%dT%H%M%SZ")


def resolve_context_limit(model: Optional[str], limit_override: Optional[int] = None) -> int:
    if limit_override is not None and limit_override > 0:
        return int(limit_override)

    env_limit = os.environ.get("MODEL_CONTEXT_LIMIT", "").strip()
    if env_limit.isdigit() and int(env_limit) > 0:
        return int(env_limit)

    if model:
        normalized = model.strip().lower()
        # Strip OpenCode / OpenClaw / OpenRouter provider prefix e.g. "deepseek/deepseek-v4"
        if "/" in normalized:
            normalized = normalized.rsplit("/", 1)[-1]
        if normalized in MODEL_CONTEXT_WINDOWS:
            return MODEL_CONTEXT_WINDOWS[normalized]
        for key in sorted(MODEL_CONTEXT_WINDOWS.keys(), key=len, reverse=True):
            if normalized.startswith(key) or key in normalized:
                return MODEL_CONTEXT_WINDOWS[key]

    return DEFAULT_CONTEXT_LIMIT


def _select_tiktoken_encoding(model: Optional[str]) -> Optional[Tuple[Any, str]]:
    if tiktoken is None:
        return None
    normalized = (model or "").lower()
    preferred = (
        "o200k_base"
        if any(k in normalized for k in ("gpt-5", "gpt-4.1", "gpt-4o", "o3", "o4"))
        else "cl100k_base"
    )
    for enc_name in (preferred, "cl100k_base", "o200k_base"):
        try:
            return tiktoken.get_encoding(enc_name), f"tiktoken:{enc_name}"
        except Exception:
            continue
    return None


def estimate_text_tokens(text: str, model: Optional[str] = None) -> Tuple[int, str]:
    if not text:
        return 0, "heuristic"

    tk = _select_tiktoken_encoding(model)
    if tk is not None:
        enc, label = tk
        try:
            return len(enc.encode(text, disallowed_special=())), label
        except Exception:
            pass

    symbol_and_whitespace_tokens = len(TOKEN_PATTERN.findall(text))
    char_calibrated = len(text) / 3.6
    return int(math.ceil(max(char_calibrated, float(symbol_and_whitespace_tokens)))), "heuristic"


def is_binary_file(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            sample = fh.read(8192)
        return b"\x00" in sample
    except OSError:
        return True


def measure_path_tokens(
    target: Path, model: Optional[str] = None
) -> Tuple[int, int, List[str]]:
    if not target.exists():
        return 0, 0, [f"missing:{target}"]

    if target.is_file():
        if is_binary_file(target):
            return 0, 0, [f"binary:{target}"]
        try:
            content = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return 0, 0, [f"unreadable:{target} ({exc})"]
        tokens, _ = estimate_text_tokens(content, model=model)
        return tokens, 1, []

    total_tokens = 0
    file_count = 0
    skipped: List[str] = []

    for root, dirs, files in os.walk(target):
        dirs[:] = sorted(d for d in dirs if d not in IGNORED_DIRS)
        for fname in sorted(files):
            fpath = Path(root) / fname
            if is_binary_file(fpath):
                skipped.append(f"binary:{fpath}")
                continue
            try:
                content = fpath.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                skipped.append(f"unreadable:{fpath} ({exc})")
                continue
            tok, _ = estimate_text_tokens(content, model=model)
            total_tokens += tok
            file_count += 1

    return total_tokens, file_count, skipped


# ---------------------------------------------------------------------------
# Universal Multi-Harness Transcript Parser & Context-Rot Detector
# ---------------------------------------------------------------------------

def discover_active_transcript(explicit_transcript: Optional[str] = None) -> Tuple[Optional[Path], str]:
    if explicit_transcript:
        p = Path(explicit_transcript).expanduser().resolve()
        return (p if p.exists() else None), "flag:--transcript"

    env_vars = (
        "CLAUDE_CODE_SESSION",
        "GEMINI_CLI_SESSION",
        "CODEX_SESSION",
        "OPENCLAW_SESSION",
        "OPENCODE_SESSION",
        "COPILOT_SESSION",
        "DEEPSEEK_SESSION",
        "GROK_SESSION",
    )
    for env_key in env_vars:
        val = os.environ.get(env_key, "").strip()
        if val:
            p = Path(val).expanduser()
            if p.is_file():
                return p.resolve(), f"env:{env_key}"

    home = Path.home()
    xdg_data = Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share"))
    candidate_dirs = [
        (home / ".claude" / "projects", "scan:~/.claude/projects"),
        (home / ".gemini" / "tmp", "scan:~/.gemini/tmp"),
        (home / ".codex" / "sessions", "scan:~/.codex/sessions"),
        (home / ".openclaw" / "sessions", "scan:~/.openclaw/sessions"),
        (xdg_data / "opencode", "scan:~/.local/share/opencode"),
        (home / ".copilot" / "session-state", "scan:~/.copilot/session-state"),
        (home / ".dsh" / "sessions", "scan:~/.dsh/sessions"),
        (home / ".grok" / "sessions", "scan:~/.grok/sessions"),
    ]

    newest_path: Optional[Path] = None
    newest_mtime = -1.0
    newest_source = "none"

    for base_dir, source_label in candidate_dirs:
        if not base_dir.is_dir():
            continue
        for root, dirs, files in os.walk(base_dir):
            dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]
            for fname in files:
                fpath = Path(root) / fname
                if fpath.suffix.lower() not in SESSION_EXTENSIONS:
                    continue
                try:
                    mtime = fpath.stat().st_mtime
                except OSError:
                    continue
                if mtime > newest_mtime:
                    newest_mtime = mtime
                    newest_path = fpath.resolve()
                    newest_source = source_label

    return newest_path, newest_source


def _extract_usage_tokens(obj: Any) -> Optional[int]:
    """Extract exact provider-reported token count across Claude, Gemini, OpenAI/Codex, DeepSeek, Grok, OpenCode."""
    if not isinstance(obj, dict):
        return None

    candidates = [
        obj.get("usage"),
        obj.get("usageMetadata"),
        (obj.get("message") or {}).get("usage") if isinstance(obj.get("message"), dict) else None,
        (obj.get("response") or {}).get("usage") if isinstance(obj.get("response"), dict) else None,
    ]
    for usage in candidates:
        if not isinstance(usage, dict):
            continue
        if "input_tokens" in usage:
            total = (
                int(usage.get("input_tokens") or 0)
                + int(usage.get("cache_creation_input_tokens") or 0)
                + int(usage.get("cache_read_input_tokens") or 0)
                + int(usage.get("output_tokens") or 0)
            )
            if total > 0:
                return total
        if "totalTokenCount" in usage and int(usage.get("totalTokenCount") or 0) > 0:
            return int(usage["totalTokenCount"])
        if "promptTokenCount" in usage:
            total = int(usage.get("promptTokenCount") or 0) + int(usage.get("candidatesTokenCount") or 0)
            if total > 0:
                return total
        if "total_tokens" in usage and int(usage.get("total_tokens") or 0) > 0:
            return int(usage["total_tokens"])
        if "prompt_tokens" in usage:
            total = int(usage.get("prompt_tokens") or 0) + int(usage.get("completion_tokens") or 0)
            if total > 0:
                return total
    return None


def analyze_transcript(transcript_path: Path, model: Optional[str] = None) -> Dict[str, Any]:
    raw = transcript_path.read_text(encoding="utf-8", errors="replace")
    fallback_tokens, estimator = estimate_text_tokens(raw, model=model)

    breakdown = {"system": 0, "user": 0, "assistant": 0, "tool_output": 0}
    last_reported_usage: Optional[int] = None
    file_edit_counts: Dict[str, int] = {}
    failed_cmd_counts: Dict[str, int] = {}
    parsed_records = 0

    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    json_records: List[Dict[str, Any]] = []
    if transcript_path.suffix.lower() == ".json":
        try:
            loaded = json.loads(raw)
            if isinstance(loaded, list):
                json_records = [x for x in loaded if isinstance(x, dict)]
            elif isinstance(loaded, dict):
                msgs = loaded.get("messages") or loaded.get("history") or loaded.get("events")
                json_records = [x for x in msgs if isinstance(x, dict)] if isinstance(msgs, list) else [loaded]
        except json.JSONDecodeError:
            json_records = []
    else:
        for ln in lines:
            if not ln.startswith("{"):
                continue
            try:
                rec = json.loads(ln)
                if isinstance(rec, dict):
                    json_records.append(rec)
            except json.JSONDecodeError:
                continue

    for rec in json_records:
        parsed_records += 1
        usage_val = _extract_usage_tokens(rec)
        if usage_val is not None:
            last_reported_usage = usage_val

        msg = rec.get("message") if isinstance(rec.get("message"), dict) else rec
        role = str(msg.get("role") or rec.get("type") or "").lower()
        content = msg.get("content", "")
        text_blob = content if isinstance(content, str) else json.dumps(content)
        tok, _ = estimate_text_tokens(text_blob, model=model)

        if "tool" in role or rec.get("type") in ("tool_result", "function_response"):
            breakdown["tool_output"] += tok
            if any(err in text_blob.lower() for err in ("traceback", "error:", "failed", "exit code 1")):
                sig = text_blob.splitlines()[0][:80] if text_blob else "tool_error"
                failed_cmd_counts[sig] = failed_cmd_counts.get(sig, 0) + 1
        elif role == "system":
            breakdown["system"] += tok
        elif role in ("assistant", "model"):
            breakdown["assistant"] += tok
        else:
            breakdown["user"] += tok

        for match in re.findall(r'"(?:path|file_path|target_file)"\s*:\s*"([^"]+)"', text_blob):
            file_edit_counts[match] = file_edit_counts.get(match, 0) + 1

    structured_sum = sum(breakdown.values())
    if last_reported_usage is not None:
        effective_tokens = last_reported_usage
        telemetry_mode = "provider_usage_exact"
    elif parsed_records > 0 and structured_sum > 0:
        effective_tokens = structured_sum
        telemetry_mode = f"structured_jsonl:{estimator}"
    else:
        effective_tokens = fallback_tokens
        telemetry_mode = f"raw_text:{estimator}"

    rot_alerts: List[str] = []
    if structured_sum > 0:
        tool_share = round((breakdown["tool_output"] / structured_sum) * 100.0, 1)
        if tool_share >= 45.0:
            rot_alerts.append(
                f"Noisy tool outputs consume {tool_share}% of parsed context — pipe logs through `compress-output`."
            )
    thrash_files = [f"{fp} ({cnt}x)" for fp, cnt in file_edit_counts.items() if cnt >= 4]
    if thrash_files:
        rot_alerts.append(f"Potential edit thrash loop detected on: {', '.join(thrash_files[:3])}.")
    repeated_failures = [sig for sig, cnt in failed_cmd_counts.items() if cnt >= 3]
    if repeated_failures:
        rot_alerts.append(f"Repeated identical tool failure ({len(repeated_failures)} pattern(s)) — record dead end and pivot.")

    return {
        "tokens": effective_tokens,
        "estimator": estimator,
        "telemetry_mode": telemetry_mode,
        "provider_reported_tokens": last_reported_usage,
        "breakdown": breakdown,
        "rot_alerts": rot_alerts,
    }


def classify_session_status(used_tokens: int, context_limit: int) -> Dict[str, Any]:
    safe_ceiling = int(context_limit * SAFE_UTILIZATION_RATIO)
    remaining = max(0, context_limit - used_tokens)
    utilization_pct = round((used_tokens / max(1, context_limit)) * 100.0, 2)

    if utilization_pct > CRITICAL_UTILIZATION_PCT or remaining < MIN_REMAINING_TOKENS:
        status = "CRITICAL_HANDOFF"
    elif YELLOW_UTILIZATION_PCT <= utilization_pct <= CRITICAL_UTILIZATION_PCT:
        status = "YELLOW"
    else:
        status = "GREEN"

    return {
        "used_tokens": used_tokens,
        "context_limit": context_limit,
        "remaining_tokens": remaining,
        "utilization_pct": utilization_pct,
        "safe_ceiling_tokens": safe_ceiling,
        "status": status,
    }


def _flatten_files_arg(raw_files: Optional[List[Any]]) -> List[str]:
    if not raw_files:
        return []
    out: List[str] = []
    for item in raw_files:
        if isinstance(item, list):
            out.extend(str(x) for x in item)
        else:
            out.append(str(item))
    return out


# ---------------------------------------------------------------------------
# Subcommand 1: inspect-session
# ---------------------------------------------------------------------------

def cmd_inspect_session(args: argparse.Namespace) -> Dict[str, Any]:
    context_limit = resolve_context_limit(args.model, args.limit)
    transcript_path, source = discover_active_transcript(args.transcript)

    transcript_info: Dict[str, Any] = {
        "tokens": 0,
        "estimator": estimate_text_tokens("calibration", model=args.model)[1],
        "telemetry_mode": "none",
        "provider_reported_tokens": None,
        "breakdown": {"system": 0, "user": 0, "assistant": 0, "tool_output": 0},
        "rot_alerts": [],
    }
    if transcript_path is not None and transcript_path.is_file() and not is_binary_file(transcript_path):
        try:
            transcript_info = analyze_transcript(transcript_path, model=args.model)
        except OSError:
            pass

    files_tokens = 0
    files_measured = 0
    skipped: List[str] = []
    for raw_path in _flatten_files_arg(args.files):
        p = Path(raw_path).expanduser()
        if not p.is_absolute():
            p = (Path.cwd() / p).resolve()
        tok, count, sk = measure_path_tokens(p, model=args.model)
        files_tokens += tok
        files_measured += count
        skipped.extend(sk)

    extra_tokens = max(0, int(args.extra_tokens or 0))
    used_tokens = int(transcript_info["tokens"]) + files_tokens + extra_tokens
    metrics = classify_session_status(used_tokens, context_limit)

    result: Dict[str, Any] = {
        "engine_version": ENGINE_VERSION,
        "model": args.model or "default",
        "estimator": transcript_info["estimator"],
        "telemetry_mode": transcript_info["telemetry_mode"],
        "provider_reported_tokens": transcript_info["provider_reported_tokens"],
        "detected_source": source,
        "transcript_path": str(transcript_path) if transcript_path else None,
        "transcript_tokens": transcript_info["tokens"],
        "breakdown": transcript_info["breakdown"],
        "rot_alerts": transcript_info["rot_alerts"],
        "files_tokens": files_tokens,
        "files_measured": files_measured,
        "extra_tokens": extra_tokens,
        **metrics,
    }
    if skipped:
        result["skipped"] = skipped
    return result


# ---------------------------------------------------------------------------
# Workspace State, Scheduling, Splitting & Parallel Wave Computation
# ---------------------------------------------------------------------------

def _resolve_workspace_path(workspace: Path, raw_path: str) -> Path:
    p = Path(raw_path).expanduser()
    return p.resolve() if p.is_absolute() else (workspace / p).resolve()


def _state_dir(workspace: Path) -> Path:
    return workspace / ".agent_state"


def _load_json_file(path: Path) -> Optional[Dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _save_json_file(path: Path, data: Dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def _default_schedule(workspace: Path, model: Optional[str] = None, limit: Optional[int] = None) -> Dict[str, Any]:
    ctx = resolve_context_limit(model, limit)
    now = utc_now_iso()
    return {
        "schema_version": 3,
        "engine_version": ENGINE_VERSION,
        "workspace": str(workspace),
        "model": model or "default",
        "context_limit": ctx,
        "safe_ceiling_tokens": int(ctx * SAFE_UTILIZATION_RATIO),
        "objective": "",
        "constraints": [],
        "decisions": [],
        "memories": [],
        "execution_receipts": [],
        "handoff_epoch": 0,
        "completed_task_ids": [],
        "completed_records": [],
        "failed_attempts": [],
        "resume_pointer": "",
        "git_anchor": None,
        "warnings": [],
        "tasks": [],
        "batches": [],
        "parallel_waves": [],
        "created_at": now,
        "updated_at": now,
    }


def _load_schedule(workspace: Path) -> Optional[Dict[str, Any]]:
    return _load_json_file(_state_dir(workspace) / "schedule.json")


def _save_schedule(workspace: Path, schedule: Dict[str, Any]) -> Path:
    return _save_json_file(_state_dir(workspace) / "schedule.json", schedule)


def _load_leases(workspace: Path) -> Dict[str, Any]:
    data = _load_json_file(_state_dir(workspace) / "leases.json")
    if not data or not isinstance(data.get("active_leases"), dict):
        return {"schema_version": 1, "active_leases": {}}
    now_ts = utc_now().timestamp()
    active = {
        tid: lease
        for tid, lease in data["active_leases"].items()
        if float(lease.get("expires_at_ts", 0)) > now_ts
    }
    return {"schema_version": 1, "active_leases": active}


def _save_leases(workspace: Path, leases: Dict[str, Any]) -> Path:
    return _save_json_file(_state_dir(workspace) / "leases.json", leases)


def _validate_and_topo_sort(tasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_id: Dict[str, Dict[str, Any]] = {}
    order_index: Dict[str, int] = {}

    for idx, task in enumerate(tasks):
        tid = str(task.get("id", "")).strip()
        title = str(task.get("title", "")).strip()
        if not tid:
            raise ValueError(f"Task at index {idx} is missing a non-empty 'id'.")
        if not title:
            raise ValueError(f"Task '{tid}' is missing a non-empty 'title'.")
        if tid in by_id:
            raise ValueError(f"Duplicate task id detected: '{tid}'.")
        deps = task.get("dependencies", [])
        if not isinstance(deps, list):
            raise ValueError(f"Task '{tid}' field 'dependencies' must be a list.")
        files_to_read = task.get("files_to_read", [])
        if not isinstance(files_to_read, list):
            raise ValueError(f"Task '{tid}' field 'files_to_read' must be a list.")
        expected_out = int(task.get("expected_output_tokens", 1000))
        if expected_out < 0:
            raise ValueError(f"Task '{tid}' field 'expected_output_tokens' must be >= 0.")

        role = str(task.get("role", "implementer")).strip().lower()
        normalized = {
            "id": tid,
            "title": title,
            "role": role if role in SPECIALIST_ROLES else "implementer",
            "files_to_read": [str(f) for f in files_to_read],
            "expected_output_tokens": expected_out,
            "dependencies": [str(d).strip() for d in deps if str(d).strip()],
        }
        by_id[tid] = normalized
        order_index[tid] = idx

    in_degree: Dict[str, int] = {tid: 0 for tid in by_id}
    dependents: Dict[str, List[str]] = {tid: [] for tid in by_id}
    for tid, task in by_id.items():
        for dep in task["dependencies"]:
            if dep not in by_id:
                raise ValueError(f"Task '{tid}' references missing dependency '{dep}'.")
            if dep == tid:
                raise ValueError(f"Task '{tid}' cannot depend on itself.")
            in_degree[tid] += 1
            dependents[dep].append(tid)

    ready = sorted([tid for tid, deg in in_degree.items() if deg == 0], key=lambda k: order_index[k])
    sorted_tasks: List[Dict[str, Any]] = []

    while ready:
        cur = ready.pop(0)
        sorted_tasks.append(by_id[cur])
        for nxt in dependents[cur]:
            in_degree[nxt] -= 1
            if in_degree[nxt] == 0:
                ready.append(nxt)
        ready.sort(key=lambda k: order_index[k])

    if len(sorted_tasks) != len(by_id):
        cycle_nodes = sorted(tid for tid, deg in in_degree.items() if deg > 0)
        raise ValueError(f"Circular dependency detected involving tasks: {', '.join(cycle_nodes)}")

    return sorted_tasks


def _estimate_task_cost(
    task: Dict[str, Any], workspace: Path, model: Optional[str]
) -> Tuple[int, int, Dict[str, int]]:
    per_file_tokens: Dict[str, int] = {}
    total_file_tokens = 0
    for raw in task.get("files_to_read", []):
        resolved = _resolve_workspace_path(workspace, raw)
        tok, _, _ = measure_path_tokens(resolved, model=model)
        per_file_tokens[raw] = tok
        total_file_tokens += tok
    expected_out = int(task.get("expected_output_tokens", 0))
    estimated_total = int(math.ceil((total_file_tokens + expected_out) * OVERHEAD_MULTIPLIER))
    return estimated_total, total_file_tokens, per_file_tokens


def _split_oversized_task(
    task: Dict[str, Any],
    per_file_tokens: Dict[str, int],
    max_task_budget: int,
) -> Tuple[List[Dict[str, Any]], str]:
    tid = task["id"]
    title = task["title"]
    role = task.get("role", "implementer")
    files = list(task["files_to_read"])
    expected_out = int(task["expected_output_tokens"])
    base_deps = list(task["dependencies"])
    raw_cap = max(1000, int(max_task_budget / OVERHEAD_MULTIPLIER))

    chunks: List[Dict[str, Any]] = []

    if files:
        file_buckets: List[List[str]] = []
        current_bucket: List[str] = []
        current_tokens = 0
        per_bucket_file_cap = max(500, int(raw_cap * 0.8))

        for f in files:
            ftok = per_file_tokens.get(f, 0)
            if current_bucket and (current_tokens + ftok > per_bucket_file_cap):
                file_buckets.append(current_bucket)
                current_bucket = [f]
                current_tokens = ftok
            else:
                current_bucket.append(f)
                current_tokens += ftok
        if current_bucket:
            file_buckets.append(current_bucket)

        total_raw = sum(per_file_tokens.values()) + expected_out
        min_parts = max(2, int(math.ceil(total_raw / max(1, raw_cap))))
        if len(file_buckets) == 1 and min_parts > 1:
            part_file_tok = int(math.ceil(sum(per_file_tokens.values()) / min_parts))
            part_out_tok = int(math.ceil(expected_out / min_parts))
            for idx in range(1, min_parts + 1):
                part_id = f"{tid}__part{idx}"
                part_deps = base_deps if idx == 1 else [f"{tid}__part{idx - 1}"]
                est = int(math.ceil((part_file_tok + part_out_tok) * OVERHEAD_MULTIPLIER))
                chunks.append(
                    {
                        "id": part_id,
                        "title": f"{title} (Part {idx}/{min_parts} — slice {files[0]})",
                        "role": role,
                        "files_to_read": list(files),
                        "expected_output_tokens": part_out_tok,
                        "file_tokens": part_file_tok,
                        "estimated_tokens": est,
                        "dependencies": part_deps,
                        "split_from": tid,
                        "status": "pending",
                    }
                )
        else:
            num_parts = len(file_buckets)
            out_per_part = max(250, int(math.ceil(expected_out / max(1, num_parts))))
            for idx, bucket in enumerate(file_buckets, start=1):
                part_id = f"{tid}__part{idx}"
                part_deps = base_deps if idx == 1 else [f"{tid}__part{idx - 1}"]
                bucket_file_tok = sum(per_file_tokens.get(f, 0) for f in bucket)
                est = int(math.ceil((bucket_file_tok + out_per_part) * OVERHEAD_MULTIPLIER))
                chunks.append(
                    {
                        "id": part_id,
                        "title": f"{title} (Part {idx}/{num_parts})",
                        "role": role,
                        "files_to_read": bucket,
                        "expected_output_tokens": out_per_part,
                        "file_tokens": bucket_file_tok,
                        "estimated_tokens": est,
                        "dependencies": part_deps,
                        "split_from": tid,
                        "status": "pending",
                    }
                )
    else:
        num_parts = max(2, int(math.ceil(expected_out / max(1, raw_cap))))
        out_per_part = int(math.ceil(expected_out / num_parts))
        for idx in range(1, num_parts + 1):
            part_id = f"{tid}__part{idx}"
            part_deps = base_deps if idx == 1 else [f"{tid}__part{idx - 1}"]
            est = int(math.ceil(out_per_part * OVERHEAD_MULTIPLIER))
            chunks.append(
                {
                    "id": part_id,
                    "title": f"{title} (Part {idx}/{num_parts})",
                    "role": role,
                    "files_to_read": [],
                    "expected_output_tokens": out_per_part,
                    "file_tokens": 0,
                    "estimated_tokens": est,
                    "dependencies": part_deps,
                    "split_from": tid,
                    "status": "pending",
                }
            )

    warning = (
        f"Task '{tid}' exceeded per-session safe budget ({max_task_budget} tokens) "
        f"and was automatically split into {len(chunks)} sequential chunks: "
        f"{', '.join(c['id'] for c in chunks)}."
    )
    return chunks, warning


def compute_parallel_waves(tasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    remaining = list(tasks)
    scheduled_ids: Set[str] = set()
    waves: List[Dict[str, Any]] = []
    wave_num = 1

    while remaining:
        wave_tasks: List[Dict[str, Any]] = []
        used_files: Set[str] = set()
        next_remaining: List[Dict[str, Any]] = []

        for t in remaining:
            deps_met = all(d in scheduled_ids for d in t.get("dependencies", []))
            t_files = set(t.get("files_to_read", []))
            file_conflict = bool(t_files & used_files)
            if deps_met and (not file_conflict or not wave_tasks):
                wave_tasks.append(t)
                used_files.update(t_files)
            else:
                next_remaining.append(t)

        if not wave_tasks:
            wave_tasks.append(next_remaining.pop(0))

        for t in wave_tasks:
            t["parallel_wave"] = wave_num
            scheduled_ids.add(t["id"])

        waves.append(
            {
                "wave": wave_num,
                "parallel_safe": len(wave_tasks) > 1,
                "task_ids": [t["id"] for t in wave_tasks],
                "total_estimated_tokens": sum(int(t.get("estimated_tokens", 0)) for t in wave_tasks),
            }
        )
        remaining = next_remaining
        wave_num += 1

    return waves


# ---------------------------------------------------------------------------
# Context Graph Engine (.agent_state/context_graph.json + Mermaid + DOT)
# ---------------------------------------------------------------------------

def build_context_graph(schedule: Dict[str, Any]) -> Dict[str, Any]:
    """Build canonical typed Memory + Research + Execution Graph from schedule state."""
    nodes: Dict[str, Dict[str, Any]] = {}
    edges: List[Dict[str, str]] = []
    seen_edges: Set[Tuple[str, str, str]] = set()

    def add_node(node_id: str, node_type: str, label: str, **attrs: Any) -> None:
        if node_id not in nodes:
            nodes[node_id] = {"id": node_id, "type": node_type, "label": label, **attrs}
        else:
            nodes[node_id].update({k: v for k, v in attrs.items() if v is not None})

    def add_edge(src: str, dst: str, rel: str) -> None:
        key = (src, dst, rel)
        if key not in seen_edges:
            seen_edges.add(key)
            edges.append({"from": src, "to": dst, "relation": rel})

    objective = schedule.get("objective", "")
    if objective:
        add_node("objective:root", "objective", objective)

    for idx, constraint in enumerate(schedule.get("constraints", []), start=1):
        cid = f"constraint:{idx}"
        add_node(cid, "constraint", constraint)
        if objective:
            add_edge(cid, "objective:root", "CONSTRAINS")

    for dec in schedule.get("decisions", []):
        did = f"decision:{dec.get('id', 'd')}"
        add_node(
            did,
            "decision",
            str(dec.get("decision", "")),
            decision_id=dec.get("id"),
            rationale=dec.get("rationale", ""),
            recorded_at=dec.get("recorded_at", ""),
        )
        if objective:
            add_edge(did, "objective:root", "GOVERNS")
        for tid in dec.get("related_tasks", []):
            add_edge(did, f"task:{tid}", "GOVERNS_TASK")
        for fp in dec.get("related_files", []):
            fnode = f"file:{fp}"
            add_node(fnode, "file", fp, path=fp)
            add_edge(did, fnode, "GOVERNS_FILE")

    # Structured Research & Ongoing Memory Nodes
    for mem in schedule.get("memories", []):
        mid = f"memory:{mem.get('id', 'm')}"
        add_node(
            mid,
            "research" if mem.get("kind") == "research" else "memory",
            str(mem.get("title", "")),
            memory_id=mem.get("id"),
            kind=mem.get("kind", "fact"),
            content=mem.get("content", ""),
            source=mem.get("source", ""),
            tags=mem.get("tags", []),
            recorded_at=mem.get("recorded_at", ""),
        )
        if objective:
            add_edge(mid, "objective:root", "INFORMS")
        for tid in mem.get("related_tasks", []):
            add_edge(mid, f"task:{tid}", "INFORMS_TASK")
        for fp in mem.get("related_files", []):
            fnode = f"file:{fp}"
            add_node(fnode, "file", fp, path=fp)
            add_edge(mid, fnode, "INFORMS_FILE")

    completed_by_id: Dict[str, Dict[str, Any]] = {
        str(r.get("task_id")): r for r in schedule.get("completed_records", []) if r.get("task_id")
    }
    completed_set = set(schedule.get("completed_task_ids", []))
    receipts_by_tid: Dict[str, List[Dict[str, Any]]] = {}
    for rcpt in schedule.get("execution_receipts", []):
        tid = str(rcpt.get("task_id", ""))
        if tid:
            receipts_by_tid.setdefault(tid, []).append(rcpt)

    for task in schedule.get("tasks", []):
        tid = str(task.get("id"))
        node_id = f"task:{tid}"
        rec = completed_by_id.get(tid)
        status = "completed" if tid in completed_set or task.get("status") == "completed" else "pending"
        last_rcpt = receipts_by_tid.get(tid, [])[-1] if receipts_by_tid.get(tid) else None
        add_node(
            node_id,
            "task",
            str(task.get("title", tid)),
            task_id=tid,
            role=task.get("role", "implementer"),
            status=status,
            batch=int(task.get("batch", 1)),
            parallel_wave=int(task.get("parallel_wave", 1)),
            estimated_tokens=int(task.get("estimated_tokens", 0)),
            summary=rec.get("summary", "") if rec else "",
            verification=rec.get("verification", "") if rec else (
                f"exit={last_rcpt['exit_code']} ({last_rcpt['command']})" if last_rcpt else ""
            ),
        )
        if objective:
            add_edge(node_id, "objective:root", "IMPLEMENTS")
        for dep in task.get("dependencies", []):
            add_edge(node_id, f"task:{dep}", "DEPENDS_ON")
        for fp in task.get("files_to_read", []):
            fnode = f"file:{fp}"
            add_node(fnode, "file", fp, path=fp)
            add_edge(node_id, fnode, "READS")
        if rec:
            for fp in rec.get("files_touched", []):
                fnode = f"file:{fp}"
                add_node(fnode, "file", fp, path=fp, last_touched_by=tid)
                add_edge(node_id, fnode, "TOUCHES")

    all_task_ids = [str(t.get("id")) for t in schedule.get("tasks", [])]
    all_file_paths = [n["path"] for n in nodes.values() if n["type"] == "file"]
    for idx, note in enumerate(schedule.get("failed_attempts", []), start=1):
        did = f"dead_end:{idx}"
        add_node(did, "dead_end", note)
        linked = False
        low = note.lower()
        for tid in all_task_ids:
            if re.search(rf"\b{re.escape(tid.lower())}\b", low):
                add_edge(did, f"task:{tid}", "WARNS_TASK")
                linked = True
        for fp in all_file_paths:
            if fp.lower() in low or Path(fp).name.lower() in low:
                add_edge(did, f"file:{fp}", "WARNS_FILE")
                linked = True
        if not linked and objective:
            add_edge(did, "objective:root", "WARNS_GLOBAL")

    git_anchor = schedule.get("git_anchor")
    if git_anchor:
        add_node(
            "checkpoint:latest",
            "checkpoint",
            f"Epoch {schedule.get('handoff_epoch', 0)} · {git_anchor.get('branch', 'N/A')}@{git_anchor.get('head_short', 'N/A')}",
            handoff_epoch=schedule.get("handoff_epoch", 0),
            git_anchor=git_anchor,
            resume_pointer=schedule.get("resume_pointer", ""),
            updated_at=schedule.get("updated_at", utc_now_iso()),
        )

    return {
        "schema_version": 3,
        "engine_version": ENGINE_VERSION,
        "handoff_epoch": schedule.get("handoff_epoch", 0),
        "updated_at": schedule.get("updated_at", utc_now_iso()),
        "workspace": schedule.get("workspace", ""),
        "resume_pointer": schedule.get("resume_pointer", ""),
        "stats": {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "task_count": sum(1 for n in nodes.values() if n["type"] == "task"),
            "file_count": sum(1 for n in nodes.values() if n["type"] == "file"),
            "decision_count": sum(1 for n in nodes.values() if n["type"] == "decision"),
            "memory_count": sum(1 for n in nodes.values() if n["type"] in ("memory", "research")),
            "dead_end_count": sum(1 for n in nodes.values() if n["type"] == "dead_end"),
        },
        "nodes": list(nodes.values()),
        "edges": edges,
    }


def _save_context_graph(workspace: Path, graph: Dict[str, Any], archive: bool = False) -> Tuple[Path, Optional[Path]]:
    sdir = _state_dir(workspace)
    sdir.mkdir(parents=True, exist_ok=True)
    graph_path = sdir / "context_graph.json"
    serialized = json.dumps(graph, indent=2) + "\n"
    graph_path.write_text(serialized, encoding="utf-8")
    archive_path: Optional[Path] = None
    if archive:
        archive_dir = sdir / "archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_path = archive_dir / f"context_graph_{utc_now_compact()}.json"
        archive_path.write_text(serialized, encoding="utf-8")
    return graph_path, archive_path


def _load_context_graph(workspace: Path) -> Optional[Dict[str, Any]]:
    return _load_json_file(_state_dir(workspace) / "context_graph.json")


def _safe_id(raw: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", raw)


def _safe_label(raw: str) -> str:
    return raw.replace('"', "'").replace("\n", " ").strip()[:64]


def render_mermaid_graph(graph: Dict[str, Any], only_node_ids: Optional[Set[str]] = None) -> str:
    lines = ["flowchart LR"]
    nodes_by_id = {n["id"]: n for n in graph.get("nodes", [])}

    included_tasks = [
        n
        for n in graph.get("nodes", [])
        if n["type"] == "task" and (only_node_ids is None or n["id"] in only_node_ids)
    ]
    if not included_tasks:
        lines.append('  empty["No scheduled tasks yet"]')
        return "\n".join(lines)

    for n in included_tasks:
        mid = _safe_id(n["id"])
        st = "DONE" if n.get("status") == "completed" else f"B{n.get('batch', 1)}/W{n.get('parallel_wave', 1)}"
        lbl = _safe_label(f"{n.get('task_id')} [{st}]: {n.get('label')}")
        lines.append(f'  {mid}["{lbl}"]')

    for e in graph.get("edges", []):
        if e["relation"] != "DEPENDS_ON":
            continue
        task_n = nodes_by_id.get(e["from"])
        dep_n = nodes_by_id.get(e["to"])
        if not task_n or not dep_n:
            continue
        if only_node_ids is not None and (e["from"] not in only_node_ids or e["to"] not in only_node_ids):
            continue
        lines.append(f"  {_safe_id(dep_n['id'])} --> {_safe_id(task_n['id'])}")

    return "\n".join(lines)


def render_dot_graph(graph: Dict[str, Any], only_node_ids: Optional[Set[str]] = None) -> str:
    lines = ["digraph ContextGraph {", "  rankdir=LR;", "  node [shape=box, style=rounded];"]
    nodes_by_id = {n["id"]: n for n in graph.get("nodes", [])}
    for n in graph.get("nodes", []):
        if n["type"] != "task":
            continue
        if only_node_ids is not None and n["id"] not in only_node_ids:
            continue
        lbl = _safe_label(f"{n.get('task_id')}: {n.get('label')}")
        lines.append(f'  "{_safe_id(n["id"])}" [label="{lbl}"];')
    for e in graph.get("edges", []):
        if e["relation"] != "DEPENDS_ON":
            continue
        if e["from"] not in nodes_by_id or e["to"] not in nodes_by_id:
            continue
        if only_node_ids is not None and (e["from"] not in only_node_ids or e["to"] not in only_node_ids):
            continue
        lines.append(f'  "{_safe_id(e["to"])}" -> "{_safe_id(e["from"])}";')
    lines.append("}")
    return "\n".join(lines)


def extract_relevant_subgraph(
    graph: Dict[str, Any],
    target_task_ids: Sequence[str],
    depth: int = 2,
    token_budget: Optional[int] = None,
) -> Dict[str, Any]:
    nodes_by_id: Dict[str, Dict[str, Any]] = {n["id"]: n for n in graph.get("nodes", [])}
    edges: List[Dict[str, str]] = graph.get("edges", [])

    selected: Set[str] = set()
    hop_distance: Dict[str, int] = {}

    for nid, node in nodes_by_id.items():
        if node["type"] in ("objective", "constraint", "checkpoint"):
            selected.add(nid)
            hop_distance[nid] = 0

    frontier: Set[str] = {f"task:{tid}" for tid in target_task_ids if f"task:{tid}" in nodes_by_id}
    for fid in frontier:
        selected.add(fid)
        hop_distance[fid] = 0

    for step in range(1, max(1, depth) + 1):
        next_frontier: Set[str] = set()
        for e in edges:
            if e["relation"] == "DEPENDS_ON" and e["from"] in frontier and e["to"] in nodes_by_id:
                if e["to"] not in selected:
                    selected.add(e["to"])
                    hop_distance[e["to"]] = step
                    next_frontier.add(e["to"])
        if not next_frontier:
            break
        frontier = next_frontier

    for e in edges:
        if e["relation"] in ("READS", "TOUCHES") and e["from"] in selected and e["to"] in nodes_by_id:
            selected.add(e["to"])
            hop_distance.setdefault(e["to"], 1)

    for e in edges:
        if e["relation"] in ("GOVERNS", "GOVERNS_TASK", "GOVERNS_FILE") and e["to"] in selected:
            selected.add(e["from"])
            hop_distance.setdefault(e["from"], 0)

    for e in edges:
        if e["relation"] in ("INFORMS_TASK", "INFORMS_FILE", "INFORMS") and e["to"] in selected:
            # Prefer task/file-linked research; include global INFORMS at hop 2
            selected.add(e["from"])
            hop_distance.setdefault(e["from"], 1 if e["relation"] != "INFORMS" else 2)

    for e in edges:
        if e["relation"] in ("WARNS_TASK", "WARNS_FILE", "WARNS_GLOBAL") and e["to"] in selected:
            selected.add(e["from"])
            hop_distance.setdefault(e["from"], 1)

    priority_rank = {
        "objective": 0,
        "constraint": 1,
        "decision": 2,
        "checkpoint": 3,
        "task": 4,
        "dead_end": 5,
        "research": 6,
        "memory": 6,
        "file": 7,
    }
    ordered_selected = sorted(
        (nodes_by_id[nid] for nid in selected),
        key=lambda n: (priority_rank.get(n["type"], 9), hop_distance.get(n["id"], 9), n["id"]),
    )

    budget_trimmed = False
    if token_budget is not None and token_budget > 0:
        kept_ids: Set[str] = set()
        running_tokens = 0
        for node in ordered_selected:
            ntok, _ = estimate_text_tokens(json.dumps(node))
            must_keep = node["type"] == "objective" or (node["type"] == "task" and hop_distance.get(node["id"], 9) == 0)
            if must_keep or (running_tokens + ntok <= token_budget):
                kept_ids.add(node["id"])
                running_tokens += ntok
            else:
                budget_trimmed = True
        selected = kept_ids

    sub_nodes = [n for n in graph.get("nodes", []) if n["id"] in selected]
    sub_edges = [e for e in edges if e["from"] in selected and e["to"] in selected]

    full_tok, _ = estimate_text_tokens(json.dumps(graph))
    sub_tok, _ = estimate_text_tokens(json.dumps({"nodes": sub_nodes, "edges": sub_edges}))
    saved_pct = round(max(0.0, ((full_tok - sub_tok) / max(1, full_tok)) * 100.0), 1)

    return {
        "target_task_ids": list(target_task_ids),
        "depth": depth,
        "token_budget": token_budget,
        "budget_trimmed": budget_trimmed,
        "nodes": sub_nodes,
        "edges": sub_edges,
        "relevant_files": sorted(n["path"] for n in sub_nodes if n["type"] == "file" and "path" in n),
        "relevant_decisions": [
            {"id": n.get("decision_id"), "decision": n["label"], "rationale": n.get("rationale", "")}
            for n in sub_nodes
            if n["type"] == "decision"
        ],
        "relevant_memories": [
            {
                "id": n.get("memory_id"),
                "kind": n.get("kind"),
                "title": n["label"],
                "content": n.get("content", ""),
                "source": n.get("source", ""),
            }
            for n in sub_nodes
            if n["type"] in ("memory", "research")
        ],
        "relevant_dead_ends": [n["label"] for n in sub_nodes if n["type"] == "dead_end"],
        "full_graph_tokens": full_tok,
        "subgraph_tokens": sub_tok,
        "tokens_saved_pct": saved_pct,
        "mermaid": render_mermaid_graph(graph, only_node_ids=selected),
        "dot": render_dot_graph(graph, only_node_ids=selected),
    }


# ---------------------------------------------------------------------------
# Subcommand 2 & 3: plan-schedule & auto-plan
# ---------------------------------------------------------------------------

def _execute_plan(
    raw_tasks: List[Dict[str, Any]],
    workspace: Path,
    model: Optional[str],
    limit: Optional[int],
    used_tokens: int,
    objective: Optional[str] = None,
) -> Dict[str, Any]:
    ordered_tasks = _validate_and_topo_sort(raw_tasks)
    context_limit = resolve_context_limit(model, limit)
    safe_ceiling = int(context_limit * SAFE_UTILIZATION_RATIO)
    used_tokens = max(0, int(used_tokens or 0))
    batch1_budget = max(MIN_BATCH1_BUDGET, safe_ceiling - used_tokens)

    warnings: List[str] = []
    expanded_tasks: List[Dict[str, Any]] = []
    rewire_map: Dict[str, str] = {}

    for task in ordered_tasks:
        remapped_deps = [rewire_map.get(d, d) for d in task["dependencies"]]
        task["dependencies"] = remapped_deps

        est_total, file_tok, per_file_tok = _estimate_task_cost(task, workspace, model)
        if est_total > safe_ceiling:
            chunks, warn = _split_oversized_task(task, per_file_tok, safe_ceiling)
            warnings.append(warn)
            expanded_tasks.extend(chunks)
            rewire_map[task["id"]] = chunks[-1]["id"]
        else:
            expanded_tasks.append(
                {
                    "id": task["id"],
                    "title": task["title"],
                    "role": task.get("role", "implementer"),
                    "files_to_read": task["files_to_read"],
                    "expected_output_tokens": task["expected_output_tokens"],
                    "file_tokens": file_tok,
                    "estimated_tokens": est_total,
                    "dependencies": remapped_deps,
                    "status": "pending",
                }
            )

    batches: List[Dict[str, Any]] = []
    task_batch_map: Dict[str, int] = {}

    def ensure_batch(b_num: int) -> Dict[str, Any]:
        while len(batches) < b_num:
            num = len(batches) + 1
            cap = batch1_budget if num == 1 else safe_ceiling
            batches.append({"batch": num, "budget_tokens": cap, "estimated_tokens": 0, "task_ids": []})
        return batches[b_num - 1]

    for task in expanded_tasks:
        min_batch = 1
        for dep in task["dependencies"]:
            if dep in task_batch_map:
                min_batch = max(min_batch, task_batch_map[dep])

        placed = False
        candidate = min_batch
        while not placed:
            b = ensure_batch(candidate)
            fits = (b["estimated_tokens"] + task["estimated_tokens"]) <= b["budget_tokens"]
            empty_overflow_ok = (len(b["task_ids"]) == 0) and (
                candidate > 1 or task["estimated_tokens"] <= b["budget_tokens"]
            )
            if fits or empty_overflow_ok:
                b["task_ids"].append(task["id"])
                b["estimated_tokens"] += task["estimated_tokens"]
                task["batch"] = candidate
                task_batch_map[task["id"]] = candidate
                placed = True
            else:
                candidate += 1

    parallel_waves = compute_parallel_waves(expanded_tasks)

    existing = _load_schedule(workspace) or _default_schedule(workspace, model, limit)
    now = utc_now_iso()
    existing.update(
        {
            "schema_version": 3,
            "engine_version": ENGINE_VERSION,
            "workspace": str(workspace),
            "model": model or existing.get("model") or "default",
            "context_limit": context_limit,
            "safe_ceiling_tokens": safe_ceiling,
            "used_tokens_at_plan": used_tokens,
            "batch1_budget_tokens": batch1_budget,
            "objective": objective.strip() if objective and objective.strip() else existing.get("objective", ""),
            "warnings": warnings,
            "tasks": expanded_tasks,
            "batches": batches,
            "parallel_waves": parallel_waves,
            "updated_at": now,
        }
    )
    sched_path = _save_schedule(workspace, existing)
    graph = build_context_graph(existing)
    graph_path, _ = _save_context_graph(workspace, graph, archive=False)

    return {
        "schedule_path": str(sched_path),
        "graph_path": str(graph_path),
        "model": existing["model"],
        "context_limit": context_limit,
        "safe_ceiling_tokens": safe_ceiling,
        "batch1_budget_tokens": batch1_budget,
        "total_tasks": len(expanded_tasks),
        "total_batches": len(batches),
        "total_parallel_waves": len(parallel_waves),
        "warnings": warnings,
        "batches": batches,
        "parallel_waves": parallel_waves,
        "tasks": expanded_tasks,
    }


def cmd_plan_schedule(args: argparse.Namespace) -> Dict[str, Any]:
    workspace = Path(args.workspace).expanduser().resolve()
    if args.tasks_file:
        raw_tasks = json.loads(Path(args.tasks_file).expanduser().read_text(encoding="utf-8"))
    elif args.tasks_json:
        raw_tasks = json.loads(args.tasks_json)
    else:
        raise ValueError("Provide either --tasks-json or --tasks-file.")

    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise ValueError("Task input must be a non-empty JSON array.")

    return _execute_plan(
        raw_tasks=raw_tasks,
        workspace=workspace,
        model=args.model,
        limit=args.limit,
        used_tokens=args.used_tokens,
        objective=getattr(args, "objective", None),
    )


def _extract_local_imports(fpath: Path, workspace: Path, known_rel_paths: Set[str]) -> Set[str]:
    deps: Set[str] = set()
    try:
        src = fpath.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return deps

    rel_parent = fpath.parent
    stem_to_rel = {Path(r).with_suffix("").as_posix(): r for r in known_rel_paths}

    if fpath.suffix == ".py":
        try:
            tree = ast.parse(src)
            for node in ast.walk(tree):
                candidates: List[str] = []
                if isinstance(node, ast.Import):
                    candidates.extend(alias.name.replace(".", "/") for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    mod_path = (node.module or "").replace(".", "/")
                    sub_names = [alias.name for alias in node.names if alias.name != "*"]
                    if node.level > 0:
                        base = rel_parent
                        for _ in range(node.level - 1):
                            base = base.parent
                        rel_base = base / mod_path if mod_path else base
                        try:
                            rel_mod = rel_base.resolve().relative_to(workspace).as_posix()
                            if rel_mod and rel_mod != ".":
                                candidates.append(rel_mod)
                            for sub in sub_names:
                                candidates.append(f"{rel_mod}/{sub}" if rel_mod and rel_mod != "." else sub)
                        except ValueError:
                            pass
                    if mod_path:
                        candidates.append(mod_path)
                        for sub in sub_names:
                            candidates.append(f"{mod_path}/{sub}")
                for cand in candidates:
                    if cand in stem_to_rel:
                        deps.add(stem_to_rel[cand])
                    elif f"{cand}/__init__" in stem_to_rel:
                        deps.add(stem_to_rel[f"{cand}/__init__"])
        except SyntaxError:
            pass
    elif fpath.suffix in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
        for match in re.findall(r"""(?:from|require\()\s*['"](\.[^'"]+)['"]""", src):
            target = (rel_parent / match).resolve()
            try:
                rel_no_ext = target.relative_to(workspace).with_suffix("").as_posix()
            except ValueError:
                continue
            if rel_no_ext in stem_to_rel:
                deps.add(stem_to_rel[rel_no_ext])
            elif f"{rel_no_ext}/index" in stem_to_rel:
                deps.add(stem_to_rel[f"{rel_no_ext}/index"])

    own_rel = fpath.relative_to(workspace).as_posix()
    deps.discard(own_rel)
    return deps


def cmd_auto_plan(args: argparse.Namespace) -> Dict[str, Any]:
    workspace = Path(args.workspace).expanduser().resolve()
    scan_root = _resolve_workspace_path(workspace, args.path or ".")
    if not scan_root.exists():
        raise ValueError(f"Path does not exist: {scan_root}")

    discovered_files: List[Path] = []
    if scan_root.is_file():
        discovered_files.append(scan_root)
    else:
        for root, dirs, files in os.walk(scan_root):
            dirs[:] = sorted(d for d in dirs if d not in IGNORED_DIRS)
            for fname in sorted(files):
                fp = Path(root) / fname
                if fp.suffix.lower() in CODE_EXTENSIONS and not is_binary_file(fp):
                    discovered_files.append(fp)

    if not discovered_files:
        raise ValueError(f"No supported source files found under {scan_root}.")

    rel_paths = [fp.relative_to(workspace).as_posix() for fp in discovered_files]
    known_set = set(rel_paths)
    raw_imports: Dict[str, Set[str]] = {
        rel: _extract_local_imports(workspace / rel, workspace, known_set) for rel in rel_paths
    }
    rank = {rel: idx for idx, rel in enumerate(sorted(rel_paths, key=lambda r: (len(raw_imports[r]), r)))}
    rel_to_tid = {rel: f"mod_{idx + 1}" for idx, rel in enumerate(sorted(rel_paths, key=lambda r: rank[r]))}

    tasks: List[Dict[str, Any]] = []
    for rel in sorted(rel_paths, key=lambda r: rank[r]):
        deps = sorted(rel_to_tid[d] for d in raw_imports[rel] if rank[d] < rank[rel])
        tasks.append(
            {
                "id": rel_to_tid[rel],
                "title": f"{args.action}: {rel}",
                "role": "implementer",
                "files_to_read": [rel],
                "expected_output_tokens": max(500, int(args.output_per_file)),
                "dependencies": deps,
            }
        )

    plan = _execute_plan(
        raw_tasks=tasks,
        workspace=workspace,
        model=args.model,
        limit=args.limit,
        used_tokens=args.used_tokens,
        objective=args.objective or f"{args.action} across {len(rel_paths)} discovered source files",
    )
    plan["discovered_files"] = len(rel_paths)
    plan["import_edges_detected"] = sum(len(v) for v in raw_imports.values())
    return plan


# ---------------------------------------------------------------------------
# Subcommand 4: gate-check
# ---------------------------------------------------------------------------

def cmd_gate_check(args: argparse.Namespace) -> Dict[str, Any]:
    workspace = Path(args.workspace).expanduser().resolve()
    schedule = _load_schedule(workspace)
    if schedule is None:
        raise ValueError(f"No schedule found at {_state_dir(workspace) / 'schedule.json'}. Run plan-schedule first.")

    task = next((t for t in schedule.get("tasks", []) if t.get("id") == args.task_id), None)
    if task is None:
        raise ValueError(f"Task ID '{args.task_id}' not found in schedule.json.")

    model = args.model or schedule.get("model")
    context_limit = resolve_context_limit(model, args.limit or schedule.get("context_limit"))
    safe_ceiling = int(context_limit * SAFE_UTILIZATION_RATIO)

    if args.used_tokens is not None:
        current_used = max(0, int(args.used_tokens))
    else:
        inspect_ns = argparse.Namespace(
            model=model,
            limit=context_limit,
            transcript=getattr(args, "transcript", None),
            files=None,
            extra_tokens=0,
        )
        current_used = cmd_inspect_session(inspect_ns)["used_tokens"]

    live_est, live_file_tokens, _ = _estimate_task_cost(task, workspace, model)
    if task.get("split_from") and task.get("estimated_tokens"):
        live_est = min(live_est, int(task["estimated_tokens"]))

    projected_used = current_used + live_est
    remaining_after_task = context_limit - projected_used

    reasons: List[str] = []
    if current_used > safe_ceiling:
        reasons.append(f"Current session usage ({current_used}) already exceeds safe ceiling ({safe_ceiling}).")
    if projected_used > safe_ceiling:
        reasons.append(f"Projected usage ({projected_used}) would breach 68% safe ceiling ({safe_ceiling}).")
    if remaining_after_task < MIN_REMAINING_TOKENS:
        reasons.append(
            f"Remaining context after task ({remaining_after_task}) would drop below minimum buffer ({MIN_REMAINING_TOKENS})."
        )

    leases = _load_leases(workspace)["active_leases"]
    agent_id = getattr(args, "agent_id", None)
    task_files = set(task.get("files_to_read", []))
    for leased_tid, lease in leases.items():
        if agent_id and lease.get("agent_id") == agent_id:
            continue
        if leased_tid == args.task_id:
            reasons.append(f"Task '{args.task_id}' is currently leased by agent '{lease.get('agent_id')}'.")
        elif task_files & set(lease.get("files", [])):
            overlap = sorted(task_files & set(lease.get("files", [])))
            reasons.append(
                f"File lock conflict with active lease '{leased_tid}' ({lease.get('agent_id')}): {', '.join(overlap)}."
            )

    if reasons:
        return {
            "decision": "HALT_AND_HANDOFF",
            "task_id": args.task_id,
            "task_title": task.get("title", ""),
            "reason": " ".join(reasons),
            "current_used_tokens": current_used,
            "task_estimated_tokens": live_est,
            "live_file_tokens": live_file_tokens,
            "projected_used_tokens": projected_used,
            "safe_ceiling_tokens": safe_ceiling,
            "context_limit": context_limit,
            "remaining_after_task": max(0, remaining_after_task),
        }

    return {
        "decision": "PROCEED",
        "task_id": args.task_id,
        "task_title": task.get("title", ""),
        "current_used_tokens": current_used,
        "task_estimated_tokens": live_est,
        "live_file_tokens": live_file_tokens,
        "projected_used_tokens": projected_used,
        "safe_ceiling_tokens": safe_ceiling,
        "context_limit": context_limit,
        "remaining_after_task": remaining_after_task,
    }


# ---------------------------------------------------------------------------
# Subcommand 5: lease (Multi-Agent Lock & Conflict Guard)
# ---------------------------------------------------------------------------

def cmd_lease(args: argparse.Namespace) -> Dict[str, Any]:
    workspace = Path(args.workspace).expanduser().resolve()
    leases_doc = _load_leases(workspace)
    active = leases_doc["active_leases"]
    action = args.action

    if action == "status":
        return {"action": "status", "active_count": len(active), "active_leases": active}

    if not args.task_id:
        raise ValueError(f"--task-id is required for lease --action {action}.")

    schedule = _load_schedule(workspace) or {}
    task = next((t for t in schedule.get("tasks", []) if t.get("id") == args.task_id), None)

    if action == "claim":
        agent_id = (args.agent_id or "agent-default").strip()
        files = list(task.get("files_to_read", [])) if task else []
        if args.files:
            files = sorted(set(files + _flatten_files_arg(args.files)))

        for other_tid, lease in active.items():
            if lease.get("agent_id") == agent_id and other_tid == args.task_id:
                continue
            if other_tid == args.task_id:
                raise ValueError(f"Conflict: task '{args.task_id}' is already claimed by '{lease.get('agent_id')}'.")
            overlap = sorted(set(files) & set(lease.get("files", [])))
            if overlap:
                raise ValueError(
                    f"File conflict: {', '.join(overlap)} locked by '{lease.get('agent_id')}' (task '{other_tid}')."
                )

        now_dt = utc_now()
        ttl = max(60, int(args.ttl_seconds or DEFAULT_LEASE_TTL_SECONDS))
        lease_obj = {
            "task_id": args.task_id,
            "agent_id": agent_id,
            "files": files,
            "claimed_at": now_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "expires_at_ts": now_dt.timestamp() + ttl,
        }
        active[args.task_id] = lease_obj
        _save_leases(workspace, leases_doc)
        return {"action": "claim", "granted": True, "lease": lease_obj}

    if action in ("release", "complete"):
        removed = active.pop(args.task_id, None)
        _save_leases(workspace, leases_doc)
        if action == "complete" and schedule:
            comp_ids = list(schedule.get("completed_task_ids", []))
            if args.task_id not in comp_ids:
                comp_ids.append(args.task_id)
            schedule["completed_task_ids"] = comp_ids
            for t in schedule.get("tasks", []):
                if t.get("id") == args.task_id:
                    t["status"] = "completed"
            schedule["updated_at"] = utc_now_iso()
            _save_schedule(workspace, schedule)
            _save_context_graph(workspace, build_context_graph(schedule), archive=False)
        return {"action": action, "task_id": args.task_id, "released": removed is not None}

    raise ValueError(f"Unsupported lease action: {action}")


# ---------------------------------------------------------------------------
# Subcommand 6: delegate (Specialized Subagent Brief Generator + Lease + Harness Dispatch)
# ---------------------------------------------------------------------------

def cmd_delegate(args: argparse.Namespace) -> Dict[str, Any]:
    workspace = Path(args.workspace).expanduser().resolve()
    schedule = _load_schedule(workspace)
    if schedule is None:
        raise ValueError(f"No schedule found in {_state_dir(workspace)}. Run plan-schedule or auto-plan first.")

    task = next((t for t in schedule.get("tasks", []) if t.get("id") == args.task_id), None)
    if task is None:
        raise ValueError(f"Task '{args.task_id}' not found in schedule.json.")

    role = (args.role or task.get("role") or "implementer").strip().lower()
    role_guidance = SPECIALIST_ROLES.get(role, SPECIALIST_ROLES["implementer"])
    worker_id = (args.agent_id or f"{role}-{args.task_id}").strip()

    # Claim exclusive file/task lease unless --no-lease
    lease_info: Optional[Dict[str, Any]] = None
    if not args.no_lease:
        lease_res = cmd_lease(
            argparse.Namespace(
                action="claim",
                task_id=args.task_id,
                agent_id=worker_id,
                files=None,
                ttl_seconds=DEFAULT_LEASE_TTL_SECONDS,
                workspace=str(workspace),
            )
        )
        lease_info = lease_res["lease"]

    graph = _load_context_graph(workspace) or build_context_graph(schedule)
    sub = extract_relevant_subgraph(graph, [args.task_id], depth=2, token_budget=args.token_budget or 3500)

    # Discover top 2 matching installed skills (metadata-only, zero bloat)
    skill_matches = discover_installed_skills(
        workspace=workspace, query=f"{task.get('title', '')} {role}", limit=2
    )["matches"]

    brief_lines = [
        f"# Subagent Delegation Brief: `{args.task_id}`",
        f"- **Role:** `{role}` — {role_guidance}",
        f"- **Worker Lease ID:** `{worker_id}`",
        f"- **Objective:** {schedule.get('objective') or '(see task)'}",
        f"- **Task:** {task.get('title')}",
        f"- **Scoped Files:** {', '.join(f'`{f}`' for f in task.get('files_to_read', [])) or '(none)'}",
        "",
        "## Constraints & Architectural Decisions",
        *([f"- {c}" for c in schedule.get("constraints", [])] or ["- None recorded."]),
        *[f"- **[{d['id']}]** {d['decision']} ({d['rationale']})" for d in sub["relevant_decisions"]],
        "",
        "## Relevant Research & Memory",
        *(
            [f"- **[{m['id']} / {m['kind']}] {m['title']}**: {m['content']}" for m in sub["relevant_memories"]]
            or ["- None linked."]
        ),
        "",
        "## Dead Ends to Avoid",
        *([f"- {d}" for d in sub["relevant_dead_ends"]] or ["- None."]),
        "",
        "## Completion Protocol",
        f"1. Edit only scoped files; run tests via `python3 scripts/budget_engine.py exec-task --task-id {args.task_id} --cmd \"<test-cmd>\"`.",
        f"2. Release lease on completion: `python3 scripts/budget_engine.py lease --action complete --task-id {args.task_id} --agent-id {worker_id}`.",
    ]
    if skill_matches:
        brief_lines.extend(
            ["", "## Recommended Companion Skills", *[f"- `{s['name']}`: {s['description']}" for s in skill_matches]]
        )

    brief_md = "\n".join(brief_lines) + "\n"
    briefs_dir = _state_dir(workspace) / "delegations"
    briefs_dir.mkdir(parents=True, exist_ok=True)
    brief_path = briefs_dir / f"{args.task_id}_{role}.md"
    brief_path.write_text(brief_md, encoding="utf-8")

    harness = (args.harness or "claude").strip().lower()
    dispatch_cmd = HARNESS_DISPATCH_TEMPLATES.get(harness, HARNESS_DISPATCH_TEMPLATES["claude"]).format(
        brief_file=str(brief_path)
    )

    return {
        "task_id": args.task_id,
        "role": role,
        "agent_id": worker_id,
        "harness": harness,
        "brief_path": str(brief_path),
        "brief_tokens": estimate_text_tokens(brief_md)[0],
        "dispatch_command": dispatch_cmd,
        "lease": lease_info,
        "recommended_skills": skill_matches,
        "subgraph_tokens_saved_pct": sub["tokens_saved_pct"],
        "brief_markdown": brief_md,
    }


# ---------------------------------------------------------------------------
# Subcommand 7: memory & record-decision (Research Organization + Long-Horizon Memory)
# ---------------------------------------------------------------------------

def cmd_memory(args: argparse.Namespace) -> Dict[str, Any]:
    workspace = Path(args.workspace).expanduser().resolve()
    schedule = _load_schedule(workspace) or _default_schedule(workspace)
    memories = list(schedule.get("memories", []))

    if args.action == "add":
        if not args.title or not args.content:
            raise ValueError("--title and --content are required for memory --action add.")
        mid = args.id.strip() if args.id else f"MEM-{len(memories) + 1:02d}"
        entry = {
            "id": mid,
            "kind": args.kind,
            "title": args.title.strip(),
            "content": args.content.strip(),
            "source": (args.source or "").strip(),
            "tags": [t.strip().lower() for t in (args.tags or "").split(",") if t.strip()],
            "related_tasks": [t.strip() for t in (args.tasks or "").split(",") if t.strip()],
            "related_files": _flatten_files_arg(args.files),
            "recorded_at": utc_now_iso(),
        }
        memories = [m for m in memories if m.get("id") != mid]
        memories.append(entry)
        schedule["memories"] = memories
        schedule["updated_at"] = utc_now_iso()
        _save_schedule(workspace, schedule)
        graph_path, _ = _save_context_graph(workspace, build_context_graph(schedule), archive=False)
        return {"action": "add", "memory": entry, "total_memories": len(memories), "graph_path": str(graph_path)}

    if args.action == "list":
        filtered = [m for m in memories if not args.kind or args.kind == "all" or m.get("kind") == args.kind]
        return {"action": "list", "count": len(filtered), "memories": filtered}

    if args.action == "query":
        q_terms = [w for w in re.findall(r"[a-z0-9_.-]+", (args.query or "").lower()) if len(w) >= 2]
        scored: List[Tuple[int, Dict[str, Any]]] = []
        for m in memories:
            if args.kind and args.kind != "all" and m.get("kind") != args.kind:
                continue
            hay = " ".join(
                [m.get("title", ""), m.get("content", ""), m.get("source", ""), *m.get("tags", []), *m.get("related_tasks", [])]
            ).lower()
            score = sum(3 if t in m.get("tags", []) else 1 for t in q_terms if t in hay) if q_terms else 1
            if score > 0:
                scored.append((score, m))
        scored.sort(key=lambda pair: (-pair[0], pair[1].get("id", "")))
        top = [m for _, m in scored[: max(1, int(args.limit or 8))]]
        return {"action": "query", "query": args.query or "", "count": len(top), "memories": top}

    raise ValueError(f"Unsupported memory action: {args.action}")


def cmd_record_decision(args: argparse.Namespace) -> Dict[str, Any]:
    workspace = Path(args.workspace).expanduser().resolve()
    schedule = _load_schedule(workspace) or _default_schedule(workspace)
    decisions = list(schedule.get("decisions", []))
    dec_id = args.id.strip() if args.id else f"ADR-{len(decisions) + 1:02d}"
    entry = {
        "id": dec_id,
        "decision": args.decision.strip(),
        "rationale": (args.rationale or "").strip(),
        "related_tasks": [t.strip() for t in (args.tasks or "").split(",") if t.strip()],
        "related_files": _flatten_files_arg(args.files),
        "recorded_at": utc_now_iso(),
    }
    decisions = [d for d in decisions if d.get("id") != dec_id]
    decisions.append(entry)
    schedule["decisions"] = decisions
    schedule["updated_at"] = utc_now_iso()

    _save_schedule(workspace, schedule)
    graph_path, _ = _save_context_graph(workspace, build_context_graph(schedule), archive=False)
    return {"decision": entry, "total_decisions": len(decisions), "graph_path": str(graph_path)}


# ---------------------------------------------------------------------------
# Subcommand 8: discover-skills (Metadata-Only Cross-Harness Skill Router)
# ---------------------------------------------------------------------------

def _parse_skill_frontmatter(skill_md: Path) -> Optional[Dict[str, str]]:
    try:
        head = skill_md.read_text(encoding="utf-8", errors="replace")[:2048]
    except OSError:
        return None
    if not head.startswith("---"):
        return None
    parts = head.split("---", 2)
    if len(parts) < 3:
        return None
    fm = parts[1]
    name_m = re.search(r"(?m)^name:\s*(.+)$", fm)
    desc_m = re.search(r"(?m)^description:\s*(.+)$", fm)
    name = name_m.group(1).strip().strip("\"'") if name_m else skill_md.parent.name
    desc = desc_m.group(1).strip().strip("\"'") if desc_m else ""
    if desc in (">", "|", ">-", "|-"):
        # Collect indented continuation lines after YAML block scalar
        block_m = re.search(r"(?ms)^description:\s*[>|]-?\s*\n((?:[ \t]+.+\n?)+)", fm)
        if block_m:
            desc = " ".join(ln.strip() for ln in block_m.group(1).splitlines())
    return {"name": name, "description": desc[:260], "path": str(skill_md)}


def discover_installed_skills(
    workspace: Path, query: str = "", extra_dirs: Optional[Sequence[Path]] = None, limit: int = 5
) -> Dict[str, Any]:
    home = Path.home()
    xdg_config = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
    scan_dirs: List[Path] = [
        workspace / ".agents" / "skills",
        workspace / ".github" / "skills",
        workspace / ".claude" / "skills",
        home / ".agents" / "skills",
        home / ".claude" / "skills",
        home / ".gemini" / "skills",
        home / ".copilot" / "skills",
        home / ".openclaw" / "skills",
        xdg_config / "opencode" / "skills",
    ]
    if extra_dirs:
        scan_dirs.extend(extra_dirs)

    found_by_name: Dict[str, Dict[str, str]] = {}
    for d in scan_dirs:
        if not d.is_dir():
            continue
        for child in sorted(d.iterdir()):
            skill_md = child / "SKILL.md" if child.is_dir() else None
            if skill_md and skill_md.is_file():
                meta = _parse_skill_frontmatter(skill_md)
                if meta and meta["name"] not in found_by_name:
                    found_by_name[meta["name"]] = meta

    q_terms = [w for w in re.findall(r"[a-z0-9_-]+", query.lower()) if len(w) >= 2]
    scored: List[Tuple[int, Dict[str, str]]] = []
    for meta in found_by_name.values():
        hay = f"{meta['name']} {meta['description']}".lower()
        score = sum(3 if t in meta["name"].lower() else 1 for t in q_terms if t in hay) if q_terms else 1
        if score > 0:
            scored.append((score, meta))
    scored.sort(key=lambda p: (-p[0], p[1]["name"]))

    return {
        "total_discovered": len(found_by_name),
        "query": query,
        "matches": [m for _, m in scored[: max(1, limit)]],
    }


def cmd_discover_skills(args: argparse.Namespace) -> Dict[str, Any]:
    workspace = Path(args.workspace).expanduser().resolve()
    extra = [Path(p).expanduser().resolve() for p in _flatten_files_arg(args.dirs)]
    return discover_installed_skills(workspace=workspace, query=args.query or "", extra_dirs=extra, limit=int(args.limit))


# ---------------------------------------------------------------------------
# Subcommand 9 & 10: compress-output & exec-task (Safe Code Execution + Log Compression)
# ---------------------------------------------------------------------------

def compress_tool_output(raw_text: str, max_lines: int = 80) -> Dict[str, Any]:
    lines = raw_text.splitlines()
    raw_tokens, _ = estimate_text_tokens(raw_text)

    if len(lines) <= max_lines:
        return {
            "compressed_text": raw_text,
            "original_lines": len(lines),
            "compressed_lines": len(lines),
            "original_tokens": raw_tokens,
            "compressed_tokens": raw_tokens,
            "tokens_saved_pct": 0.0,
        }

    signal_re = re.compile(
        r"(error|fail|failed|exception|traceback|assert|warning|conflict|^\+\+\+ |^--- |^@@ |:\d+:)",
        re.IGNORECASE,
    )
    head = lines[:10]
    tail = lines[-15:]
    middle = lines[10:-15]

    signal_lines: List[str] = []
    seen_norm: Set[str] = set()
    omitted = 0
    middle_cap = max(10, max_lines - 28)

    for ln in middle:
        if signal_re.search(ln):
            norm = re.sub(r"\d+", "#", ln.strip())
            if norm not in seen_norm and len(signal_lines) < middle_cap:
                seen_norm.add(norm)
                signal_lines.append(ln)
            else:
                omitted += 1
        else:
            omitted += 1

    out_lines = [
        *head,
        f"... [compressed {omitted} repetitive/passing lines; kept {len(signal_lines)} high-signal lines] ...",
        *signal_lines,
        "... [tail] ...",
        *tail,
    ]
    compressed_text = "\n".join(out_lines)
    comp_tokens, _ = estimate_text_tokens(compressed_text)
    saved_pct = round(max(0.0, ((raw_tokens - comp_tokens) / max(1, raw_tokens)) * 100.0), 1)
    return {
        "compressed_text": compressed_text,
        "original_lines": len(lines),
        "compressed_lines": len(out_lines),
        "original_tokens": raw_tokens,
        "compressed_tokens": comp_tokens,
        "tokens_saved_pct": saved_pct,
    }


def cmd_compress_output(args: argparse.Namespace) -> Dict[str, Any]:
    raw = Path(args.file).expanduser().read_text(encoding="utf-8", errors="replace") if args.file else sys.stdin.read()
    return compress_tool_output(raw, max_lines=max(25, int(args.max_lines)))


def cmd_exec_task(args: argparse.Namespace) -> Dict[str, Any]:
    """Execute a code/test/build command for a task with auto-compressed output and graph receipt."""
    workspace = Path(args.workspace).expanduser().resolve()
    t0 = time.monotonic()
    proc = subprocess.run(
        args.cmd,
        cwd=str(workspace),
        shell=True,
        capture_output=True,
        text=True,
        timeout=max(5, int(args.timeout)),
        check=False,
    )
    elapsed_sec = round(time.monotonic() - t0, 3)
    combined = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    comp = compress_tool_output(combined.strip(), max_lines=max(25, int(args.max_lines)))

    receipt = {
        "task_id": args.task_id or "ad-hoc",
        "command": args.cmd,
        "exit_code": proc.returncode,
        "passed": proc.returncode == 0,
        "elapsed_sec": elapsed_sec,
        "tokens_saved_pct": comp["tokens_saved_pct"],
        "recorded_at": utc_now_iso(),
    }

    schedule = _load_schedule(workspace)
    if schedule is not None:
        receipts = list(schedule.get("execution_receipts", []))
        receipts.append(receipt)
        schedule["execution_receipts"] = receipts[-50:]
        schedule["updated_at"] = utc_now_iso()
        _save_schedule(workspace, schedule)
        _save_context_graph(workspace, build_context_graph(schedule), archive=False)

    return {**receipt, "output": comp["compressed_text"], "original_tokens": comp["original_tokens"], "compressed_tokens": comp["compressed_tokens"]}


# ---------------------------------------------------------------------------
# Subcommand 11: verify-handoff
# ---------------------------------------------------------------------------

def capture_git_state(workspace: Path) -> Dict[str, Any]:
    if shutil.which("git") is None:
        return {"is_git_repo": False, "branch": "N/A", "head_short": "N/A", "status_short": ""}

    def run_git(git_args: Sequence[str]) -> Optional[str]:
        try:
            proc = subprocess.run(
                ["git", *git_args],
                cwd=str(workspace),
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            return proc.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None

    if run_git(["rev-parse", "--is-inside-work-tree"]) != "true":
        return {"is_git_repo": False, "branch": "N/A", "head_short": "N/A", "status_short": ""}

    return {
        "is_git_repo": True,
        "branch": run_git(["rev-parse", "--abbrev-ref", "HEAD"]) or "DETACHED",
        "head_short": run_git(["rev-parse", "--short", "HEAD"]) or "UNBORN",
        "status_short": run_git(["status", "--short"]) or "",
    }


def cmd_verify_handoff(args: argparse.Namespace) -> Dict[str, Any]:
    workspace = Path(args.workspace).expanduser().resolve()
    git_state = capture_git_state(workspace)
    issues: List[str] = []
    checked_files: List[str] = []

    candidate_paths: List[Path] = []
    if args.files:
        for raw in _flatten_files_arg(args.files):
            candidate_paths.append(_resolve_workspace_path(workspace, raw))
    elif git_state["is_git_repo"] and git_state["status_short"]:
        for line in git_state["status_short"].splitlines():
            rel = line[3:].strip().split(" -> ")[-1]
            p = workspace / rel
            if p.is_file():
                candidate_paths.append(p)
    else:
        for root, dirs, files in os.walk(workspace):
            dirs[:] = sorted(d for d in dirs if d not in IGNORED_DIRS)
            for fn in sorted(files):
                p = Path(root) / fn
                if p.suffix.lower() in (".py", ".json"):
                    candidate_paths.append(p)

    for fp in candidate_paths:
        if not fp.is_file() or is_binary_file(fp):
            continue
        rel = fp.relative_to(workspace).as_posix() if workspace in fp.parents else str(fp)
        checked_files.append(rel)
        text = fp.read_text(encoding="utf-8", errors="replace")
        if CONFLICT_MARKER_RE.search(text):
            issues.append(f"Merge conflict marker detected in {rel}")
        if fp.suffix == ".py":
            try:
                ast.parse(text, filename=rel)
            except SyntaxError as exc:
                issues.append(f"Python syntax error in {rel}:{exc.lineno}: {exc.msg}")
        elif fp.suffix == ".json":
            try:
                json.loads(text)
            except json.JSONDecodeError as exc:
                issues.append(f"Invalid JSON in {rel}:{exc.lineno}: {exc.msg}")

    verify_cmd_result: Optional[Dict[str, Any]] = None
    if args.verify_cmd:
        proc = subprocess.run(
            args.verify_cmd,
            cwd=str(workspace),
            shell=True,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        verify_cmd_result = {
            "command": args.verify_cmd,
            "exit_code": proc.returncode,
            "passed": proc.returncode == 0,
            "output_summary": compress_tool_output(proc.stdout + "\n" + proc.stderr, max_lines=30)["compressed_text"],
        }
        if proc.returncode != 0:
            issues.append(f"Verification command failed (exit {proc.returncode}): {args.verify_cmd}")

    return {
        "handoff_ready": len(issues) == 0,
        "checked_files": checked_files,
        "issues": issues,
        "verify_cmd_result": verify_cmd_result,
        "git_state": git_state,
    }


# ---------------------------------------------------------------------------
# Subcommand 12, 13, 14, 15: save-worklog, query-graph, resume-context, status
# ---------------------------------------------------------------------------

def _render_worklog_markdown(
    timestamp_utc: str,
    model: str,
    metrics: Dict[str, Any],
    git_state: Dict[str, Any],
    objective: str,
    constraints: List[str],
    decisions: List[Dict[str, Any]],
    memories: List[Dict[str, Any]],
    completed_records: List[Dict[str, Any]],
    pending_tasks: List[Dict[str, Any]],
    failed_attempts: List[str],
    resume_pointer: str,
    mermaid_graph: str,
) -> str:
    template_path = Path(__file__).resolve().parent.parent / "references" / "WORKLOG_TEMPLATE.md"
    template = (
        template_path.read_text(encoding="utf-8")
        if template_path.is_file()
        else (
            "# Context Budget Worklog\n\n"
            "## Session Telemetry\n- **Timestamp (UTC):** {{TIMESTAMP_UTC}}\n- **Model:** `{{MODEL}}`\n"
            "- **Token Usage:** {{USED_TOKENS}} / {{CONTEXT_LIMIT}} ({{UTILIZATION_PCT}}%)\n"
            "- **Safe Ceiling (68%):** {{SAFE_CEILING_TOKENS}} tokens\n- **Status:** `{{STATUS}}`\n\n"
            "## Git Anchor\n- **Repository:** {{IS_GIT_REPO}}\n- **Branch:** `{{GIT_BRANCH}}`\n"
            "- **HEAD (short):** `{{GIT_HEAD_SHORT}}`\n```text\n{{GIT_STATUS_SHORT}}\n```\n\n"
            "## Execution & Dependency Graph\n```mermaid\n{{MERMAID_GRAPH}}\n```\n\n"
            "## Immutable Objective\n{{OBJECTIVE}}\n\n## Constraints & Guardrails\n{{CONSTRAINTS_BULLETS}}\n\n"
            "## Architectural Decisions (Invariants)\n{{DECISIONS_BULLETS}}\n\n"
            "## Organized Research & Memory\n{{MEMORIES_BULLETS}}\n\n"
            "## Completed Tasks\n{{COMPLETED_TASKS_SECTION}}\n\n"
            "## Remaining Schedule\n{{REMAINING_TASKS_SECTION}}\n\n"
            "## Failed Attempts & Dead Ends (Do Not Repeat)\n{{FAILED_ATTEMPTS_BULLETS}}\n\n"
            "## Exact Resume Pointer\n> {{RESUME_POINTER}}\n"
        )
    )

    constraints_md = "\n".join(f"- {c}" for c in constraints) if constraints else "- None recorded."
    decisions_md = (
        "\n".join(
            f"- **`{d.get('id')}`**: {d.get('decision')}"
            + (f" *(Rationale: {d.get('rationale')})*" if d.get("rationale") else "")
            for d in decisions
        )
        if decisions
        else "- None recorded."
    )
    memories_md = (
        "\n".join(
            f"- **`{m.get('id')}` [{m.get('kind')}] {m.get('title')}**: {m.get('content')}"
            + (f" *(Source: {m.get('source')})*" if m.get("source") else "")
            for m in memories
        )
        if memories
        else "- None recorded."
    )
    if completed_records:
        completed_md = "\n".join(
            f"- **`{r.get('task_id')}`**: {r.get('summary', '')}\n"
            f"  - Files touched: {', '.join(f'`{f}`' for f in r.get('files_touched', [])) or 'none'}\n"
            f"  - Verification: {r.get('verification') or 'not specified'}"
            for r in completed_records
        )
    else:
        completed_md = "- None yet."

    remaining_md = (
        "\n".join(
            f"- [ ] **Batch {t.get('batch', 1)} · Wave {t.get('parallel_wave', 1)} · `{t.get('id')}` ({t.get('role', 'implementer')})**: "
            f"{t.get('title')} (~{t.get('estimated_tokens', 0):,} tokens)"
            for t in pending_tasks
        )
        if pending_tasks
        else "- All scheduled tasks completed."
    )
    failed_md = "\n".join(f"- {f}" for f in failed_attempts) if failed_attempts else "- None recorded."

    replacements = {
        "{{TIMESTAMP_UTC}}": timestamp_utc,
        "{{MODEL}}": model,
        "{{USED_TOKENS}}": f"{metrics['used_tokens']:,}",
        "{{CONTEXT_LIMIT}}": f"{metrics['context_limit']:,}",
        "{{UTILIZATION_PCT}}": f"{metrics['utilization_pct']:.2f}",
        "{{SAFE_CEILING_TOKENS}}": f"{metrics['safe_ceiling_tokens']:,}",
        "{{STATUS}}": metrics["status"],
        "{{IS_GIT_REPO}}": "yes" if git_state["is_git_repo"] else "no",
        "{{GIT_BRANCH}}": git_state["branch"],
        "{{GIT_HEAD_SHORT}}": git_state["head_short"],
        "{{GIT_STATUS_SHORT}}": git_state["status_short"] or "(clean)",
        "{{MERMAID_GRAPH}}": mermaid_graph,
        "{{OBJECTIVE}}": objective or "Not specified.",
        "{{CONSTRAINTS_BULLETS}}": constraints_md,
        "{{DECISIONS_BULLETS}}": decisions_md,
        "{{MEMORIES_BULLETS}}": memories_md,
        "{{COMPLETED_TASKS_SECTION}}": completed_md,
        "{{REMAINING_TASKS_SECTION}}": remaining_md,
        "{{FAILED_ATTEMPTS_BULLETS}}": failed_md,
        "{{RESUME_POINTER}}": resume_pointer or "Inspect remaining tasks in .agent_state/context_graph.json.",
    }
    rendered = template
    for placeholder, value in replacements.items():
        rendered = rendered.replace(placeholder, value)
    return rendered


def cmd_save_worklog(args: argparse.Namespace) -> Dict[str, Any]:
    workspace = Path(args.workspace).expanduser().resolve()
    schedule = _load_schedule(workspace) or _default_schedule(workspace, args.model, args.limit)

    objective = args.objective.strip() if args.objective and args.objective.strip() else schedule.get("objective", "")
    if args.constraints_json is not None:
        parsed_constraints = json.loads(args.constraints_json)
        if not isinstance(parsed_constraints, list):
            raise ValueError("--constraints-json must be a JSON array of strings.")
        constraints = [str(c) for c in parsed_constraints]
    else:
        constraints = list(schedule.get("constraints", []))

    completed_records = list(schedule.get("completed_records", []))
    completed_ids = list(schedule.get("completed_task_ids", []))
    existing_by_tid = {str(r.get("task_id")): idx for idx, r in enumerate(completed_records)}

    if args.completed_json is not None:
        new_completed = json.loads(args.completed_json)
        if not isinstance(new_completed, list):
            raise ValueError("--completed-json must be a JSON array of objects.")
        for entry in new_completed:
            if not isinstance(entry, dict) or not entry.get("task_id"):
                raise ValueError("Each completed entry must be an object containing 'task_id'.")
            tid = str(entry["task_id"])
            normalized_rec = {
                "task_id": tid,
                "summary": str(entry.get("summary", "")),
                "files_touched": [str(f) for f in entry.get("files_touched", [])],
                "verification": str(entry.get("verification", "")),
                "recorded_at": utc_now_iso(),
            }
            if tid in existing_by_tid:
                completed_records[existing_by_tid[tid]] = normalized_rec
            else:
                existing_by_tid[tid] = len(completed_records)
                completed_records.append(normalized_rec)
            if tid not in completed_ids:
                completed_ids.append(tid)

    failed_attempts = list(schedule.get("failed_attempts", []))
    if args.failed_json is not None:
        new_failed = json.loads(args.failed_json)
        if not isinstance(new_failed, list):
            raise ValueError("--failed-json must be a JSON array of strings.")
        for item in new_failed:
            s = str(item).strip()
            if s and s not in failed_attempts:
                failed_attempts.append(s)

    resume_pointer = (
        args.resume_pointer.strip()
        if args.resume_pointer and args.resume_pointer.strip()
        else schedule.get("resume_pointer", "")
    )

    completed_set = set(completed_ids)
    pending_tasks: List[Dict[str, Any]] = []
    for task in schedule.get("tasks", []):
        if task.get("id") in completed_set:
            task["status"] = "completed"
        else:
            task["status"] = "pending"
            pending_tasks.append(task)

    model = args.model or schedule.get("model") or "default"
    context_limit = resolve_context_limit(model, args.limit or schedule.get("context_limit"))
    used_tokens = max(0, int(args.used_tokens or 0))
    metrics = classify_session_status(used_tokens, context_limit)
    git_state = capture_git_state(workspace)
    timestamp_utc = utc_now_iso()
    epoch = int(schedule.get("handoff_epoch", 0)) + 1

    schedule.update(
        {
            "model": model,
            "context_limit": context_limit,
            "safe_ceiling_tokens": metrics["safe_ceiling_tokens"],
            "handoff_epoch": epoch,
            "objective": objective,
            "constraints": constraints,
            "completed_task_ids": completed_ids,
            "completed_records": completed_records,
            "failed_attempts": failed_attempts,
            "resume_pointer": resume_pointer,
            "git_anchor": git_state,
            "last_handoff_metrics": metrics,
            "updated_at": timestamp_utc,
        }
    )
    sched_path = _save_schedule(workspace, schedule)

    graph = build_context_graph(schedule)
    graph_path, graph_archive_path = _save_context_graph(workspace, graph, archive=True)
    mermaid_graph = render_mermaid_graph(graph)

    markdown = _render_worklog_markdown(
        timestamp_utc=timestamp_utc,
        model=model,
        metrics=metrics,
        git_state=git_state,
        objective=objective,
        constraints=constraints,
        decisions=schedule.get("decisions", []),
        memories=schedule.get("memories", []),
        completed_records=completed_records,
        pending_tasks=pending_tasks,
        failed_attempts=failed_attempts,
        resume_pointer=resume_pointer,
        mermaid_graph=mermaid_graph,
    )

    sdir = _state_dir(workspace)
    archive_dir = sdir / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    worklog_path = sdir / "WORKLOG.md"
    archive_path = archive_dir / f"WORKLOG_{utc_now_compact()}.md"

    worklog_path.write_text(markdown, encoding="utf-8")
    archive_path.write_text(markdown, encoding="utf-8")

    return {
        "handoff_epoch": epoch,
        "worklog_path": str(worklog_path),
        "archive_path": str(archive_path),
        "graph_path": str(graph_path),
        "graph_archive_path": str(graph_archive_path) if graph_archive_path else None,
        "graph_stats": graph["stats"],
        "schedule_path": str(sched_path),
        "objective": objective,
        "constraints": constraints,
        "completed_count": len(completed_ids),
        "pending_count": len(pending_tasks),
        "git_anchor": git_state,
        "resume_pointer": resume_pointer,
        "session_metrics": metrics,
    }


def cmd_query_graph(args: argparse.Namespace) -> Dict[str, Any]:
    workspace = Path(args.workspace).expanduser().resolve()
    graph = _load_context_graph(workspace)
    if graph is None:
        schedule = _load_schedule(workspace)
        if schedule is None:
            raise ValueError(f"No context_graph.json or schedule.json found in {_state_dir(workspace)}.")
        graph = build_context_graph(schedule)
        _save_context_graph(workspace, graph, archive=False)

    if args.task_id:
        targets = [t.strip() for t in args.task_id.split(",") if t.strip()]
    else:
        pending_nodes = [n for n in graph.get("nodes", []) if n["type"] == "task" and n.get("status") != "completed"]
        if pending_nodes:
            min_batch = min(int(n.get("batch", 1)) for n in pending_nodes)
            targets = [str(n["task_id"]) for n in pending_nodes if int(n.get("batch", 1)) == min_batch]
        else:
            targets = []

    subgraph = extract_relevant_subgraph(
        graph,
        targets,
        depth=max(1, int(args.depth)),
        token_budget=args.token_budget,
    )
    return {"graph_path": str(_state_dir(workspace) / "context_graph.json"), "graph_stats": graph.get("stats", {}), **subgraph}


def cmd_resume_context(args: argparse.Namespace) -> Dict[str, Any]:
    workspace = Path(args.workspace).expanduser().resolve()
    sdir = _state_dir(workspace)
    worklog_path = sdir / "WORKLOG.md"
    graph_path = sdir / "context_graph.json"
    schedule = _load_schedule(workspace)

    if schedule is None and not worklog_path.is_file() and not graph_path.is_file():
        raise ValueError(f"No .agent_state/context_graph.json, WORKLOG.md, or schedule.json found in {workspace}.")

    schedule = schedule or {}
    saved_git = schedule.get("git_anchor") or {}
    current_git = capture_git_state(workspace)

    drift_details: List[str] = []
    if saved_git.get("is_git_repo") and current_git.get("is_git_repo"):
        if saved_git.get("head_short") != current_git.get("head_short"):
            drift_details.append(
                f"Git HEAD moved: saved '{saved_git.get('head_short')}' vs current '{current_git.get('head_short')}'."
            )
        if saved_git.get("branch") != current_git.get("branch"):
            drift_details.append(
                f"Git branch changed: saved '{saved_git.get('branch')}' vs current '{current_git.get('branch')}'."
            )
        if (saved_git.get("status_short") or "") != (current_git.get("status_short") or ""):
            drift_details.append("Working tree status differs from saved handoff anchor.")

    completed_set = set(schedule.get("completed_task_ids", []))
    all_tasks = schedule.get("tasks", [])
    pending_tasks = [t for t in all_tasks if t.get("id") not in completed_set]

    next_batch_num: Optional[int] = None
    next_batch_tasks: List[Dict[str, Any]] = []
    if pending_tasks:
        next_batch_num = min(int(t.get("batch", 1)) for t in pending_tasks)
        next_batch_tasks = [t for t in pending_tasks if int(t.get("batch", 1)) == next_batch_num]

    graph = _load_context_graph(workspace) or build_context_graph(schedule)
    target_ids = [str(t["id"]) for t in next_batch_tasks]
    subgraph = extract_relevant_subgraph(graph, target_ids, depth=2, token_budget=getattr(args, "token_budget", None))
    active_leases = _load_leases(workspace)["active_leases"]

    return {
        "workspace": str(workspace),
        "handoff_epoch": schedule.get("handoff_epoch", 0),
        "graph_path": str(graph_path),
        "graph_exists": graph_path.is_file(),
        "worklog_path": str(worklog_path),
        "worklog_exists": worklog_path.is_file(),
        "objective": schedule.get("objective", ""),
        "constraints": schedule.get("constraints", []),
        "decisions": schedule.get("decisions", []),
        "resume_pointer": schedule.get("resume_pointer", ""),
        "completed_task_ids": list(schedule.get("completed_task_ids", [])),
        "pending_count": len(pending_tasks),
        "next_batch": next_batch_num,
        "next_batch_tasks": [
            {
                "id": t.get("id"),
                "title": t.get("title"),
                "role": t.get("role", "implementer"),
                "parallel_wave": t.get("parallel_wave", 1),
                "files_to_read": t.get("files_to_read", []),
                "estimated_tokens": t.get("estimated_tokens", 0),
                "dependencies": t.get("dependencies", []),
            }
            for t in next_batch_tasks
        ],
        "active_leases": active_leases,
        "focused_subgraph": {
            "node_count": len(subgraph["nodes"]),
            "edge_count": len(subgraph["edges"]),
            "relevant_files": subgraph["relevant_files"],
            "relevant_decisions": subgraph["relevant_decisions"],
            "relevant_memories": subgraph["relevant_memories"],
            "relevant_dead_ends": subgraph["relevant_dead_ends"],
            "full_graph_tokens": subgraph["full_graph_tokens"],
            "subgraph_tokens": subgraph["subgraph_tokens"],
            "tokens_saved_pct": subgraph["tokens_saved_pct"],
            "nodes": subgraph["nodes"],
            "edges": subgraph["edges"],
        },
        "failed_attempts": schedule.get("failed_attempts", []),
        "saved_git_anchor": saved_git,
        "current_git_state": current_git,
        "git_drift_detected": len(drift_details) > 0,
        "drift_details": drift_details,
    }


def cmd_status(args: argparse.Namespace) -> Dict[str, Any]:
    workspace = Path(args.workspace).expanduser().resolve()
    schedule = _load_schedule(workspace) or {}
    session = cmd_inspect_session(
        argparse.Namespace(
            model=args.model or schedule.get("model"),
            limit=args.limit or schedule.get("context_limit"),
            transcript=args.transcript,
            files=None,
            extra_tokens=args.extra_tokens,
        )
    )
    leases = _load_leases(workspace)["active_leases"]
    graph = _load_context_graph(workspace)
    tasks = schedule.get("tasks", [])
    completed_set = set(schedule.get("completed_task_ids", []))
    pending = [t for t in tasks if t.get("id") not in completed_set]
    return {
        "engine_version": ENGINE_VERSION,
        "workspace": str(workspace),
        "handoff_epoch": schedule.get("handoff_epoch", 0),
        "session": session,
        "objective": schedule.get("objective", ""),
        "completed_count": len(completed_set),
        "pending_count": len(pending),
        "total_tasks": len(tasks),
        "decision_count": len(schedule.get("decisions", [])),
        "memory_count": len(schedule.get("memories", [])),
        "active_leases": len(leases),
        "graph_stats": graph.get("stats", {}) if graph else {},
        "resume_pointer": schedule.get("resume_pointer", ""),
        "next_task": pending[0]["id"] if pending else None,
    }


# ---------------------------------------------------------------------------
# Human-Readable Rendering & CLI Parser
# ---------------------------------------------------------------------------

def _render_progress_bar(pct: float, width: int = 20) -> str:
    filled = min(width, max(0, int(round((pct / 100.0) * width))))
    return f"[{'#' * filled}{'-' * (width - filled)}]"


def _format_human_output(command: str, data: Dict[str, Any], raw_args: argparse.Namespace) -> str:
    if command == "inspect-session":
        bar = _render_progress_bar(data["utilization_pct"])
        out = [
            f"[inspect-session] {bar} {data['utilization_pct']}% {data['status']} | "
            f"used={data['used_tokens']:,}/{data['context_limit']:,} | remaining={data['remaining_tokens']:,} | "
            f"safe_ceiling={data['safe_ceiling_tokens']:,} | telemetry={data['telemetry_mode']}"
        ]
        for alert in data.get("rot_alerts", []):
            out.append(f"  ROT ALERT: {alert}")
        return "\n".join(out)

    if command in ("plan-schedule", "auto-plan"):
        lines = [
            f"[{command}] {data['total_tasks']} task(s) | {data['total_batches']} batch(es) | "
            f"{data['total_parallel_waves']} parallel wave(s) (safe_ceiling={data['safe_ceiling_tokens']:,})"
        ]
        for b in data["batches"]:
            lines.append(f"  Batch {b['batch']} (~{b['estimated_tokens']:,}/{b['budget_tokens']:,} tok): {', '.join(b['task_ids'])}")
        for w in data.get("parallel_waves", []):
            tag = "PARALLEL" if w["parallel_safe"] else "SERIAL"
            lines.append(f"  Wave {w['wave']} [{tag}]: {', '.join(w['task_ids'])}")
        for warn in data["warnings"]:
            lines.append(f"  WARNING: {warn}")
        return "\n".join(lines)

    if command == "gate-check":
        if data["decision"] == "PROCEED":
            return f"[gate-check] PROCEED task={data['task_id']} | projected={data['projected_used_tokens']:,}/{data['safe_ceiling_tokens']:,} safe"
        return f"[gate-check] HALT_AND_HANDOFF task={data['task_id']} | reason={data['reason']}"

    if command == "lease":
        if data["action"] == "status":
            return f"[lease] active={data['active_count']} lease(s): {', '.join(data['active_leases'].keys()) or '(none)'}"
        if data["action"] == "claim":
            return f"[lease] CLAIMED task={data['lease']['task_id']} by agent={data['lease']['agent_id']}"
        return f"[lease] {data['action'].upper()} task={data['task_id']} released={data['released']}"

    if command == "delegate":
        return (
            f"[delegate] task={data['task_id']} role={data['role']} agent={data['agent_id']} "
            f"brief={data['brief_path']} ({data['brief_tokens']} tok, -{data['subgraph_tokens_saved_pct']}% pruned)\n"
            f"  dispatch ({data['harness']}): {data['dispatch_command']}"
        )

    if command == "memory":
        if data["action"] == "add":
            m = data["memory"]
            return f"[memory] Saved {m['id']} [{m['kind']}]: {m['title']} (total={data['total_memories']})"
        return "\n".join(
            [f"[memory:{data['action']}] {data['count']} item(s)"]
            + [f"  - {m['id']} [{m['kind']}] {m['title']}: {m['content'][:90]}" for m in data["memories"]]
        )

    if command == "discover-skills":
        return "\n".join(
            [f"[discover-skills] matched {len(data['matches'])} of {data['total_discovered']} installed skill(s)"]
            + [f"  - {m['name']}: {m['description'][:100]}" for m in data["matches"]]
        )

    if command == "exec-task":
        verdict = "PASS" if data["passed"] else f"FAIL (exit {data['exit_code']})"
        return (
            f"[exec-task] {verdict} task={data['task_id']} ({data['elapsed_sec']}s, -{data['tokens_saved_pct']}% log tok)\n"
            f"{data['output']}"
        )

    if command == "record-decision":
        d = data["decision"]
        return f"[record-decision] Saved {d['id']}: {d['decision']} (total={data['total_decisions']})"

    if command == "compress-output":
        return data["compressed_text"]

    if command == "verify-handoff":
        verdict = "READY" if data["handoff_ready"] else "BLOCKED"
        lines = [f"[verify-handoff] {verdict} | checked={len(data['checked_files'])} file(s) | issues={len(data['issues'])}"]
        for iss in data["issues"]:
            lines.append(f"  ISSUE: {iss}")
        return "\n".join(lines)

    if command == "save-worklog":
        gs = data.get("graph_stats", {})
        return (
            f"[save-worklog] Epoch {data['handoff_epoch']} | graph={data['graph_path']} "
            f"({gs.get('node_count', 0)} nodes, {gs.get('edge_count', 0)} edges) | "
            f"completed={data['completed_count']} pending={data['pending_count']}"
        )

    if command == "query-graph":
        if getattr(raw_args, "mermaid", False):
            return data["mermaid"]
        if getattr(raw_args, "dot", False):
            return data["dot"]
        return (
            f"[query-graph] targets={data['target_task_ids']} | "
            f"pruned={data['subgraph_tokens']:,}/{data['full_graph_tokens']:,} tok (-{data['tokens_saved_pct']}%)"
        )

    if command == "resume-context":
        fg = data.get("focused_subgraph", {})
        lines = [
            f"[resume-context] Epoch {data['handoff_epoch']} | objective={data['objective'] or '(unset)'}",
            f"  resume_pointer: {data['resume_pointer'] or '(unset)'}",
            f"  pending={data['pending_count']} | next_batch={data['next_batch']} | git_drift={data['git_drift_detected']}",
            f"  graph_pruning: {fg.get('subgraph_tokens', 0):,}/{fg.get('full_graph_tokens', 0):,} tok (-{fg.get('tokens_saved_pct', 0)}%)",
        ]
        for d in data["drift_details"]:
            lines.append(f"  DRIFT: {d}")
        return "\n".join(lines)

    if command == "audit":
        act = data.get("action")
        if act == "redact":
            return data["redacted_text"]
        if act == "triage":
            return f"[audit:triage] {data['verdict']} ({data['passed_gates']}/7 gates) | {data['severity']} {data['cwe']} @ {data['location']}"
        if act == "init-scope":
            return f"[audit:init-scope] Saved scope boundary: {data['scope_path']}"
        if act == "diff":
            return (
                f"[audit:diff] new={data['new_count']} fixed={data['fixed_count']} persisting={data['persisting_count']} "
                f"regression={data['regression_detected']}"
            )
        lines = [
            f"[audit:{act}] {data['total_findings']} finding(s) "
            f"(CRITICAL={data['by_severity']['CRITICAL']}, HIGH={data['by_severity']['HIGH']}, "
            f"MEDIUM={data['by_severity']['MEDIUM']}, LOW={data['by_severity']['LOW']})"
        ]
        for f in data.get("findings", [])[:15]:
            lines.append(f"  - [{f['severity']}] {f['cwe']} {f['rule_id']} @ {f['file']}:{f['line']} — {f['remediation']}")
        if data.get("exported_reports"):
            lines.append(f"  reports: {', '.join(f'{k}={v}' for k, v in data['exported_reports'].items())}")
        return "\n".join(lines)

    if command == "status":
        s = data["session"]
        bar = _render_progress_bar(s["utilization_pct"])
        return "\n".join(
            [
                f"=== context-budget-orchestrator v{data['engine_version']} (Epoch {data['handoff_epoch']}) ===",
                f"Budget:    {bar} {s['utilization_pct']}% {s['status']} ({s['used_tokens']:,}/{s['context_limit']:,} tok)",
                f"Progress:  {data['completed_count']}/{data['total_tasks']} tasks | {data['decision_count']} ADRs | {data['memory_count']} memories | {data['active_leases']} leases",
                f"Graph:     {data['graph_stats'].get('node_count', 0)} nodes / {data['graph_stats'].get('edge_count', 0)} edges",
                f"Next Task: {data['next_task'] or '(none)'} | Pointer: {data['resume_pointer'] or '(unset)'}",
            ]
        )

    return json.dumps(data, indent=2)


# ---------------------------------------------------------------------------
# Subcommand 17: audit (Delegates to modular zero-dep scripts/security_audit.py)
# ---------------------------------------------------------------------------

def _load_security_audit_module():
    script_dir = str(Path(__file__).resolve().parent)
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    import security_audit  # type: ignore
    return security_audit


def cmd_audit(args: argparse.Namespace) -> Dict[str, Any]:
    sa = _load_security_audit_module()
    workspace = Path(args.workspace).expanduser().resolve()
    action = args.action
    report_dir = _resolve_workspace_path(workspace, args.report_dir) if args.report_dir else (_state_dir(workspace) / "reports")
    formats = [f.strip() for f in (args.formats or "").split(",") if f.strip()]

    if action == "redact":
        raw = Path(args.file).expanduser().read_text(encoding="utf-8", errors="replace") if args.file else sys.stdin.read()
        redacted, count = sa.redact_sensitive_text(raw)
        return {"action": "redact", "redactions_applied": count, "redacted_text": redacted}

    if action == "init-scope":
        if args.scope_nl:
            parsed = sa.parse_nl_scope(args.scope_nl)
            in_scope = parsed["in_scope"]
            out_of_scope = parsed["out_of_scope"]
            hosts = parsed["hosts"]
        else:
            in_scope = [s.strip() for s in (args.in_scope or ".").split(",") if s.strip()]
            out_of_scope = [s.strip() for s in (args.out_of_scope or "").split(",") if s.strip()]
            hosts = []
        scope_doc = {
            "schema_version": 2,
            "created_at": utc_now_iso(),
            "authorization": args.authorization or "Authorized local repository audit",
            "in_scope": in_scope,
            "out_of_scope": out_of_scope,
            "hosts": hosts,
        }
        scope_path = _save_json_file(_state_dir(workspace) / "audit_scope.json", scope_doc)
        return {"action": "init-scope", "scope_path": str(scope_path), "scope": scope_doc}

    if action == "triage":
        gates = {
            "1_in_scope": bool(args.in_scope_confirmed),
            "2_reproducible": bool(args.reproducible and (args.poc or "").strip()),
            "3_root_cause_pinned": bool((args.location or "").strip()),
            "4_real_impact": bool((args.impact or "").strip()),
            "5_severity_calibrated": (args.severity or "").upper() in sa.SEVERITY_RANK,
            "6_remediation_clear": bool((args.remediation or "").strip()),
            "7_evidence_redacted": bool(args.evidence_redacted),
        }
        passed = sum(1 for v in gates.values() if v)
        missing = [k for k, v in gates.items() if not v]
        verdict = "VALID_FINDING" if passed == 7 else "NEEDS_EVIDENCE"
        safe_poc, _ = sa.redact_sensitive_text(args.poc or "")
        report_md = (
            f"### [{(args.severity or 'MEDIUM').upper()}] {args.title or 'Untitled Finding'} ({args.cwe or 'CWE-TBD'})\n"
            f"- **Verdict:** `{verdict}` ({passed}/7 triage gates passed)\n"
            f"- **Location:** `{args.location or 'unpinned'}`\n"
            f"- **Impact:** {args.impact or '(missing)'}\n"
            f"- **Reproduction (redacted):** `{safe_poc or '(missing)'}`\n"
            f"- **Remediation:** {args.remediation or '(missing)'}\n"
        )
        return {
            "action": "triage",
            "verdict": verdict,
            "passed_gates": passed,
            "missing_gates": missing,
            "gates": gates,
            "severity": (args.severity or "MEDIUM").upper(),
            "cwe": args.cwe or "CWE-TBD",
            "location": args.location or "",
            "report_markdown": report_md,
        }

    if action == "diff":
        if not args.baseline or not args.current:
            raise ValueError("audit --action diff requires both --baseline and --current JSON files.")
        base_doc = json.loads(Path(args.baseline).expanduser().read_text(encoding="utf-8"))
        curr_doc = json.loads(Path(args.current).expanduser().read_text(encoding="utf-8"))
        diff_res = sa.diff_scans(base_doc.get("findings", []), curr_doc.get("findings", []))
        exported: Dict[str, str] = {}
        if formats:
            synth = {
                "scanned_at": utc_now_iso(),
                "total_findings": diff_res["new_count"],
                "by_severity": {
                    sev: sum(1 for f in diff_res["new_findings"] if f.get("severity") == sev)
                    for sev in ("CRITICAL", "HIGH", "MEDIUM", "LOW")
                },
                "findings": diff_res["new_findings"],
            }
            exported = sa.export_reports(synth, formats, report_dir, prefix="audit_diff")
        return {"action": "diff", **diff_res, "exported_reports": exported}

    if action == "openapi":
        if not args.spec:
            raise ValueError("audit --action openapi requires --spec <path-to-openapi.json>.")
        spec_path = _resolve_workspace_path(workspace, args.spec)
        res = sa.audit_openapi_spec(spec_path)
        exported = sa.export_reports(res, formats, report_dir, prefix="openapi_audit") if formats else {}
        return {"action": "openapi", **res, "exported_reports": exported}

    if action == "scan":
        scan_root = _resolve_workspace_path(workspace, args.path or ".")
        if not scan_root.exists():
            raise ValueError(f"Scan path does not exist: {scan_root}")

        scope_doc = _load_json_file(_state_dir(workspace) / "audit_scope.json") or {}
        if args.scope_nl:
            parsed = sa.parse_nl_scope(args.scope_nl)
            in_scope = parsed["in_scope"]
            out_of_scope = parsed["out_of_scope"]
        else:
            in_scope = scope_doc.get("in_scope", ["."])
            out_of_scope = scope_doc.get("out_of_scope", [])

        custom_rules = sa.load_custom_rules(args.rules_file)
        scan_res = sa.scan_workspace(
            workspace=workspace,
            scan_root=scan_root,
            in_scope=in_scope,
            out_of_scope=out_of_scope,
            custom_rules=custom_rules,
        )

        # Optional --retest-new baseline filter (keeps ONLY newly introduced findings)
        retest_summary: Optional[Dict[str, Any]] = None
        if args.retest_new:
            base_doc = json.loads(Path(args.retest_new).expanduser().read_text(encoding="utf-8"))
            d = sa.diff_scans(base_doc.get("findings", []), scan_res["findings"])
            retest_summary = {
                "baseline_count": d["baseline_count"],
                "fixed_count": d["fixed_count"],
                "persisting_suppressed": d["persisting_count"],
            }
            scan_res["findings"] = d["new_findings"]
            scan_res["total_findings"] = len(d["new_findings"])
            scan_res["by_severity"] = {
                sev: sum(1 for f in d["new_findings"] if f.get("severity") == sev)
                for sev in ("CRITICAL", "HIGH", "MEDIUM", "LOW")
            }
            scan_res["compliance_summary"] = sa.summarize_compliance(d["new_findings"])

        findings = scan_res["findings"]
        graph_path_str: Optional[str] = None
        if args.save_graph or args.create_tasks:
            schedule = _load_schedule(workspace) or _default_schedule(workspace)
            memories = [m for m in schedule.get("memories", []) if not str(m.get("id", "")).startswith("FND-")]
            for f in findings[:30]:
                memories.append(
                    {
                        "id": f["id"],
                        "kind": "risk",
                        "title": f"[{f['severity']}] {f['cwe']} {f['title']}",
                        "content": f"{f['file']}:{f['line']} — {f['remediation']}",
                        "source": f"{f['file']}:{f['line']}",
                        "tags": ["security", f["cwe"].lower(), f["severity"].lower()],
                        "related_tasks": [],
                        "related_files": [f["file"]],
                        "recorded_at": utc_now_iso(),
                    }
                )
            schedule["memories"] = memories
            schedule["updated_at"] = utc_now_iso()
            _save_schedule(workspace, schedule)
            gp, _ = _save_context_graph(workspace, build_context_graph(schedule), archive=False)
            graph_path_str = str(gp)

        created_tasks_count = 0
        if args.create_tasks and findings:
            by_file: Dict[str, List[Dict[str, Any]]] = {}
            for f in findings:
                by_file.setdefault(f["file"], []).append(f)
            rem_tasks = [
                {
                    "id": f"fix_{idx + 1}",
                    "title": f"Remediate {len(flist)} finding(s) in {fpath} ({flist[0]['cwe']})",
                    "role": "implementer",
                    "files_to_read": [fpath],
                    "expected_output_tokens": 1500,
                    "dependencies": [],
                }
                for idx, (fpath, flist) in enumerate(by_file.items())
            ]
            _execute_plan(
                raw_tasks=rem_tasks,
                workspace=workspace,
                model=args.model,
                limit=args.limit,
                used_tokens=0,
                objective=f"Remediate {len(findings)} security/bug audit finding(s)",
            )
            created_tasks_count = len(rem_tasks)

        exported = sa.export_reports(scan_res, formats, report_dir, prefix="audit_report") if formats else {}
        return {
            "action": "scan",
            **scan_res,
            "retest_new_summary": retest_summary,
            "graph_path": graph_path_str,
            "remediation_tasks_created": created_tasks_count,
            "exported_reports": exported,
        }

    raise ValueError(f"Unsupported audit action: {action}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="budget_engine.py",
        description="Universal zero-dependency Context Budget, Typed Memory Graph, Delegation & Skill Router.",
    )
    parser.add_argument("--version", action="version", version=f"context-budget-orchestrator {ENGINE_VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)

    # 1. inspect-session
    p_ins = sub.add_parser("inspect-session", help="Inspect session usage, exact provider telemetry, and rot alerts.")
    p_ins.add_argument("--model", default=None)
    p_ins.add_argument("--limit", type=int, default=None)
    p_ins.add_argument("--transcript", default=None)
    p_ins.add_argument("--files", action="append", nargs="+", default=None)
    p_ins.add_argument("--extra-tokens", type=int, default=0)
    p_ins.add_argument("--json", action="store_true")

    # 2. plan-schedule
    p_plan = sub.add_parser("plan-schedule", help="Plan token-safe batches, parallel waves, and context_graph.json.")
    p_plan.add_argument("--tasks-json", default=None)
    p_plan.add_argument("--tasks-file", default=None)
    p_plan.add_argument("--objective", default=None)
    p_plan.add_argument("--model", default=None)
    p_plan.add_argument("--limit", type=int, default=None)
    p_plan.add_argument("--used-tokens", type=int, default=0)
    p_plan.add_argument("--workspace", default=".")
    p_plan.add_argument("--json", action="store_true")

    # 3. auto-plan
    p_auto = sub.add_parser("auto-plan", help="Auto-discover local code imports and build a topological schedule.")
    p_auto.add_argument("--path", default=".")
    p_auto.add_argument("--action", default="Audit and update")
    p_auto.add_argument("--objective", default=None)
    p_auto.add_argument("--output-per-file", type=int, default=1500)
    p_auto.add_argument("--model", default=None)
    p_auto.add_argument("--limit", type=int, default=None)
    p_auto.add_argument("--used-tokens", type=int, default=0)
    p_auto.add_argument("--workspace", default=".")
    p_auto.add_argument("--json", action="store_true")

    # 4. gate-check
    p_gate = sub.add_parser("gate-check", help="Check whether starting a task stays within safe budget and lease locks.")
    p_gate.add_argument("--task-id", required=True)
    p_gate.add_argument("--agent-id", default=None)
    p_gate.add_argument("--model", default=None)
    p_gate.add_argument("--limit", type=int, default=None)
    p_gate.add_argument("--used-tokens", type=int, default=None)
    p_gate.add_argument("--transcript", default=None)
    p_gate.add_argument("--workspace", default=".")
    p_gate.add_argument("--json", action="store_true")

    # 5. lease
    p_lease = sub.add_parser("lease", help="Manage atomic multi-agent task & file-conflict leases.")
    p_lease.add_argument("--action", required=True, choices=["claim", "release", "complete", "status"])
    p_lease.add_argument("--task-id", default=None)
    p_lease.add_argument("--agent-id", default=None)
    p_lease.add_argument("--files", action="append", nargs="+", default=None)
    p_lease.add_argument("--ttl-seconds", type=int, default=DEFAULT_LEASE_TTL_SECONDS)
    p_lease.add_argument("--workspace", default=".")
    p_lease.add_argument("--json", action="store_true")

    # 6. delegate
    p_del = sub.add_parser("delegate", help="Generate a token-pruned specialist subagent brief, claim lease, and emit harness dispatch command.")
    p_del.add_argument("--task-id", required=True)
    p_del.add_argument("--role", default=None, choices=sorted(SPECIALIST_ROLES.keys()))
    p_del.add_argument("--agent-id", default=None)
    p_del.add_argument("--harness", default="claude", choices=sorted(HARNESS_DISPATCH_TEMPLATES.keys()))
    p_del.add_argument("--token-budget", type=int, default=3500)
    p_del.add_argument("--no-lease", action="store_true")
    p_del.add_argument("--workspace", default=".")
    p_del.add_argument("--json", action="store_true")

    # 7. memory (research notes + ongoing project facts)
    p_mem = sub.add_parser("memory", help="Organize research findings and persistent project memory inside context_graph.json.")
    p_mem.add_argument("--action", required=True, choices=["add", "query", "list"])
    p_mem.add_argument("--kind", default="research", choices=["research", "fact", "risk", "all"])
    p_mem.add_argument("--id", default=None)
    p_mem.add_argument("--title", default=None)
    p_mem.add_argument("--content", default=None)
    p_mem.add_argument("--source", default="")
    p_mem.add_argument("--tags", default="")
    p_mem.add_argument("--tasks", default="")
    p_mem.add_argument("--files", action="append", nargs="+", default=None)
    p_mem.add_argument("--query", default="")
    p_mem.add_argument("--limit", type=int, default=8)
    p_mem.add_argument("--workspace", default=".")
    p_mem.add_argument("--json", action="store_true")

    # 8. record-decision
    p_dec = sub.add_parser("record-decision", help="Record an immutable architectural decision / invariant.")
    p_dec.add_argument("--id", default=None)
    p_dec.add_argument("--decision", required=True)
    p_dec.add_argument("--rationale", default="")
    p_dec.add_argument("--tasks", default="")
    p_dec.add_argument("--files", action="append", nargs="+", default=None)
    p_dec.add_argument("--workspace", default=".")
    p_dec.add_argument("--json", action="store_true")

    # 9. discover-skills
    p_sk = sub.add_parser("discover-skills", help="Metadata-only skill router across all installed CLI skill directories.")
    p_sk.add_argument("--query", default="")
    p_sk.add_argument("--dirs", action="append", nargs="+", default=None)
    p_sk.add_argument("--limit", type=int, default=5)
    p_sk.add_argument("--workspace", default=".")
    p_sk.add_argument("--json", action="store_true")

    # 10. compress-output
    p_cmp = sub.add_parser("compress-output", help="Compress verbose test/build/diff logs to prevent context rot.")
    p_cmp.add_argument("--file", default=None)
    p_cmp.add_argument("--max-lines", type=int, default=80)
    p_cmp.add_argument("--json", action="store_true")

    # 11. exec-task
    p_ex = sub.add_parser("exec-task", help="Execute code/tests for a task with auto-compressed logs and graph receipt.")
    p_ex.add_argument("--task-id", default="ad-hoc")
    p_ex.add_argument("--cmd", required=True)
    p_ex.add_argument("--timeout", type=int, default=120)
    p_ex.add_argument("--max-lines", type=int, default=60)
    p_ex.add_argument("--workspace", default=".")
    p_ex.add_argument("--json", action="store_true")

    # 12. verify-handoff
    p_ver = sub.add_parser("verify-handoff", help="Pre-handoff AST syntax, JSON, conflict-marker, and optional test gate.")
    p_ver.add_argument("--files", action="append", nargs="+", default=None)
    p_ver.add_argument("--verify-cmd", default=None)
    p_ver.add_argument("--workspace", default=".")
    p_ver.add_argument("--json", action="store_true")

    # 13. save-worklog
    p_save = sub.add_parser("save-worklog", help="Persist canonical context_graph.json, WORKLOG.md with Mermaid DAG, and archives.")
    p_save.add_argument("--objective", default=None)
    p_save.add_argument("--constraints-json", default=None)
    p_save.add_argument("--completed-json", default=None)
    p_save.add_argument("--failed-json", default=None)
    p_save.add_argument("--resume-pointer", default=None)
    p_save.add_argument("--model", default=None)
    p_save.add_argument("--limit", type=int, default=None)
    p_save.add_argument("--used-tokens", type=int, default=0)
    p_save.add_argument("--workspace", default=".")
    p_save.add_argument("--json", action="store_true")

    # 14. query-graph
    p_qg = sub.add_parser("query-graph", help="Extract a token-budgeted ancestor/decision/memory/file subgraph.")
    p_qg.add_argument("--task-id", default=None)
    p_qg.add_argument("--depth", type=int, default=2)
    p_qg.add_argument("--token-budget", type=int, default=None)
    p_qg.add_argument("--mermaid", action="store_true")
    p_qg.add_argument("--dot", action="store_true")
    p_qg.add_argument("--workspace", default=".")
    p_qg.add_argument("--json", action="store_true")

    # 15. resume-context
    p_res = sub.add_parser("resume-context", help="Load graph-pruned handoff context, decisions, memory, leases, and git drift.")
    p_res.add_argument("--token-budget", type=int, default=None)
    p_res.add_argument("--workspace", default=".")
    p_res.add_argument("--json", action="store_true")

    # 16. status
    p_st = sub.add_parser("status", help="One-screen mission-control dashboard.")
    p_st.add_argument("--model", default=None)
    p_st.add_argument("--limit", type=int, default=None)
    p_st.add_argument("--transcript", default=None)
    p_st.add_argument("--extra-tokens", type=int, default=0)
    p_st.add_argument("--workspace", default=".")
    p_st.add_argument("--json", action="store_true")

    # 17. audit (30 CWE static scan, OpenAPI contract audit, baseline diff, 7-Q triage, redactor, compliance, SARIF/HTML/JUnit/CSV)
    p_aud = sub.add_parser("audit", help="Enterprise zero-dep security audit: 30 CWE rules, OpenAPI audit, compliance, diff, SARIF/HTML/JUnit/CSV.")
    p_aud.add_argument("--action", required=True, choices=["scan", "openapi", "diff", "triage", "redact", "init-scope"])
    p_aud.add_argument("--path", default=".")
    p_aud.add_argument("--file", default=None)
    p_aud.add_argument("--spec", default=None, help="Path to OpenAPI/Swagger JSON spec for --action openapi.")
    p_aud.add_argument("--rules-file", default=None, help="Path to custom JSON audit rules.")
    p_aud.add_argument("--scope-nl", default="", help="Natural-language scope e.g. 'only src/* no tests/*'.")
    p_aud.add_argument("--baseline", default=None, help="Baseline JSON report for --action diff.")
    p_aud.add_argument("--current", default=None, help="Current JSON report for --action diff.")
    p_aud.add_argument("--retest-new", default=None, help="Baseline JSON to filter only new findings during scan.")
    p_aud.add_argument("--formats", default="", help="Comma-separated export formats: json,sarif,html,junit,csv,md")
    p_aud.add_argument("--report-dir", default=None, help="Directory for exported reports (default: .agent_state/reports).")
    p_aud.add_argument("--save-graph", action="store_true")
    p_aud.add_argument("--create-tasks", action="store_true")
    p_aud.add_argument("--model", default=None)
    p_aud.add_argument("--limit", type=int, default=None)
    p_aud.add_argument("--in-scope", default=".")
    p_aud.add_argument("--out-of-scope", default="")
    p_aud.add_argument("--authorization", default="")
    p_aud.add_argument("--title", default="")
    p_aud.add_argument("--severity", default="MEDIUM")
    p_aud.add_argument("--cwe", default="")
    p_aud.add_argument("--location", default="")
    p_aud.add_argument("--poc", default="")
    p_aud.add_argument("--impact", default="")
    p_aud.add_argument("--remediation", default="")
    p_aud.add_argument("--in-scope-confirmed", action="store_true")
    p_aud.add_argument("--reproducible", action="store_true")
    p_aud.add_argument("--evidence-redacted", action="store_true")
    p_aud.add_argument("--workspace", default=".")
    p_aud.add_argument("--json", action="store_true")

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    dispatch = {
        "inspect-session": cmd_inspect_session,
        "plan-schedule": cmd_plan_schedule,
        "auto-plan": cmd_auto_plan,
        "gate-check": cmd_gate_check,
        "lease": cmd_lease,
        "delegate": cmd_delegate,
        "memory": cmd_memory,
        "record-decision": cmd_record_decision,
        "discover-skills": cmd_discover_skills,
        "compress-output": cmd_compress_output,
        "exec-task": cmd_exec_task,
        "verify-handoff": cmd_verify_handoff,
        "save-worklog": cmd_save_worklog,
        "query-graph": cmd_query_graph,
        "resume-context": cmd_resume_context,
        "status": cmd_status,
        "audit": cmd_audit,
    }

    try:
        result = dispatch[args.command](args)
    except Exception as exc:
        if getattr(args, "json", False):
            sys.stdout.write(json.dumps({"error": str(exc)}, indent=2) + "\n")
        else:
            sys.stderr.write(f"Error: {exc}\n")
        return 1

    if getattr(args, "json", False):
        sys.stdout.write(json.dumps(result, indent=2) + "\n")
    else:
        sys.stdout.write(_format_human_output(args.command, result, args) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
