#!/usr/bin/env python3
"""
context-budget-orchestrator: Zero-dependency context-window budget & Context Graph engine.

Requires Python 3.9+ standard library only (automatically uses `tiktoken` if installed).
Supports Claude Code, Gemini CLI, and OpenAI Codex CLI across macOS, Linux, and Windows.
"""

from __future__ import annotations

import argparse
import datetime
import json
import math
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

# Optional tiktoken support with zero hard dependency
try:
    import tiktoken  # type: ignore
except ImportError:
    tiktoken = None  # type: ignore

DEFAULT_CONTEXT_LIMIT = 200_000
SAFE_UTILIZATION_RATIO = 0.68
YELLOW_UTILIZATION_PCT = 55.0
CRITICAL_UTILIZATION_PCT = 68.0
MIN_REMAINING_TOKENS = 25_000
OVERHEAD_MULTIPLIER = 1.35
MIN_BATCH1_BUDGET = 10_000

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
    # OpenAI / Codex
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
}

TOKEN_PATTERN = re.compile(r"\w+|[^\w\s]|\n")
SESSION_EXTENSIONS = frozenset({".jsonl", ".json", ".log", ".md", ".txt"})


def utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_now_compact() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def resolve_context_limit(model: Optional[str], limit_override: Optional[int] = None) -> int:
    if limit_override is not None and limit_override > 0:
        return int(limit_override)

    env_limit = os.environ.get("MODEL_CONTEXT_LIMIT", "").strip()
    if env_limit.isdigit() and int(env_limit) > 0:
        return int(env_limit)

    if model:
        normalized = model.strip().lower()
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
    """Return (total_tokens, file_count, skipped_reasons)."""
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


def discover_active_transcript(explicit_transcript: Optional[str] = None) -> Tuple[Optional[Path], str]:
    if explicit_transcript:
        p = Path(explicit_transcript).expanduser().resolve()
        return (p if p.exists() else None), "flag:--transcript"

    for env_key in ("CLAUDE_CODE_SESSION", "GEMINI_CLI_SESSION", "CODEX_SESSION"):
        val = os.environ.get(env_key, "").strip()
        if val:
            p = Path(val).expanduser()
            if p.is_file():
                return p.resolve(), f"env:{env_key}"

    home = Path.home()
    candidate_dirs = [
        (home / ".claude" / "projects", "scan:~/.claude/projects"),
        (home / ".gemini" / "tmp", "scan:~/.gemini/tmp"),
        (home / ".codex" / "sessions", "scan:~/.codex/sessions"),
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


def cmd_inspect_session(args: argparse.Namespace) -> Dict[str, Any]:
    context_limit = resolve_context_limit(args.model, args.limit)
    transcript_path, source = discover_active_transcript(args.transcript)

    transcript_tokens = 0
    estimator = "heuristic"
    if transcript_path is not None and transcript_path.is_file() and not is_binary_file(transcript_path):
        try:
            text = transcript_path.read_text(encoding="utf-8", errors="replace")
            transcript_tokens, estimator = estimate_text_tokens(text, model=args.model)
        except OSError:
            transcript_tokens = 0
    else:
        _, estimator = estimate_text_tokens("calibration sample", model=args.model)

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
    used_tokens = transcript_tokens + files_tokens + extra_tokens

    metrics = classify_session_status(used_tokens, context_limit)
    result: Dict[str, Any] = {
        "model": args.model or "default",
        "estimator": estimator,
        "detected_source": source,
        "transcript_path": str(transcript_path) if transcript_path else None,
        "transcript_tokens": transcript_tokens,
        "files_tokens": files_tokens,
        "files_measured": files_measured,
        "extra_tokens": extra_tokens,
        **metrics,
    }
    if skipped:
        result["skipped"] = skipped
    return result


def _resolve_workspace_path(workspace: Path, raw_path: str) -> Path:
    p = Path(raw_path).expanduser()
    return p.resolve() if p.is_absolute() else (workspace / p).resolve()


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

        normalized = {
            "id": tid,
            "title": title,
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
    """Split a task exceeding max_task_budget into sequential chunks (<id>__part1, ...)."""
    tid = task["id"]
    title = task["title"]
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


def _state_dir(workspace: Path) -> Path:
    return workspace / ".agent_state"


def _load_schedule(workspace: Path) -> Optional[Dict[str, Any]]:
    sched_path = _state_dir(workspace) / "schedule.json"
    if not sched_path.is_file():
        return None
    try:
        return json.loads(sched_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _save_schedule(workspace: Path, schedule: Dict[str, Any]) -> Path:
    sdir = _state_dir(workspace)
    sdir.mkdir(parents=True, exist_ok=True)
    sched_path = sdir / "schedule.json"
    sched_path.write_text(json.dumps(schedule, indent=2) + "\n", encoding="utf-8")
    return sched_path


# ---------------------------------------------------------------------------
# Context Graph Engine (.agent_state/context_graph.json + Mermaid projection)
# ---------------------------------------------------------------------------

def build_context_graph(schedule: Dict[str, Any]) -> Dict[str, Any]:
    """Build a canonical typed knowledge & execution graph from schedule state."""
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

    completed_by_id: Dict[str, Dict[str, Any]] = {
        str(r.get("task_id")): r for r in schedule.get("completed_records", []) if r.get("task_id")
    }
    completed_set = set(schedule.get("completed_task_ids", []))

    for task in schedule.get("tasks", []):
        tid = str(task.get("id"))
        node_id = f"task:{tid}"
        rec = completed_by_id.get(tid)
        status = "completed" if tid in completed_set or task.get("status") == "completed" else "pending"
        add_node(
            node_id,
            "task",
            str(task.get("title", tid)),
            task_id=tid,
            status=status,
            batch=int(task.get("batch", 1)),
            estimated_tokens=int(task.get("estimated_tokens", 0)),
            summary=rec.get("summary", "") if rec else "",
            verification=rec.get("verification", "") if rec else "",
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

    # Dead ends / failed attempts linked to matching tasks or files when mentioned
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
            f"{git_anchor.get('branch', 'N/A')}@{git_anchor.get('head_short', 'N/A')}",
            git_anchor=git_anchor,
            resume_pointer=schedule.get("resume_pointer", ""),
            updated_at=schedule.get("updated_at", utc_now_iso()),
        )

    return {
        "schema_version": 1,
        "updated_at": schedule.get("updated_at", utc_now_iso()),
        "workspace": schedule.get("workspace", ""),
        "resume_pointer": schedule.get("resume_pointer", ""),
        "stats": {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "task_count": sum(1 for n in nodes.values() if n["type"] == "task"),
            "file_count": sum(1 for n in nodes.values() if n["type"] == "file"),
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
    graph_path = _state_dir(workspace) / "context_graph.json"
    if not graph_path.is_file():
        return None
    try:
        return json.loads(graph_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _mermaid_safe_id(raw: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", raw)


def _mermaid_safe_label(raw: str) -> str:
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
        mid = _mermaid_safe_id(n["id"])
        st = "DONE" if n.get("status") == "completed" else f"B{n.get('batch', 1)}"
        lbl = _mermaid_safe_label(f"{n.get('task_id')} [{st}]: {n.get('label')}")
        lines.append(f'  {mid}["{lbl}"]')

    for e in graph.get("edges", []):
        if e["relation"] != "DEPENDS_ON":
            continue
        # Edge is task -> dep (DEPENDS_ON), render execution flow dep --> task
        task_n = nodes_by_id.get(e["from"])
        dep_n = nodes_by_id.get(e["to"])
        if not task_n or not dep_n:
            continue
        if only_node_ids is not None and (e["from"] not in only_node_ids or e["to"] not in only_node_ids):
            continue
        lines.append(f"  {_mermaid_safe_id(dep_n['id'])} --> {_mermaid_safe_id(task_n['id'])}")

    return "\n".join(lines)


def extract_relevant_subgraph(
    graph: Dict[str, Any], target_task_ids: Sequence[str], depth: int = 2
) -> Dict[str, Any]:
    """
    Prune the full Context Graph down to ONLY the subgraph needed for target_task_ids:
      - Immutable objective & constraints
      - Target tasks + upstream DEPENDS_ON ancestors up to `depth` hops
      - Files read/touched by those tasks (plus 1-hop shared-file producers)
      - Dead-end warnings attached to those tasks/files or global objective
      - Latest checkpoint node
    """
    nodes_by_id: Dict[str, Dict[str, Any]] = {n["id"]: n for n in graph.get("nodes", [])}
    edges: List[Dict[str, str]] = graph.get("edges", [])

    selected: Set[str] = set()
    # Always include immutable contract & latest checkpoint
    for nid, node in nodes_by_id.items():
        if node["type"] in ("objective", "constraint", "checkpoint"):
            selected.add(nid)

    # Traverse upstream DEPENDS_ON from target tasks
    frontier: Set[str] = {f"task:{tid}" for tid in target_task_ids if f"task:{tid}" in nodes_by_id}
    selected.update(frontier)
    for _ in range(max(1, depth)):
        next_frontier: Set[str] = set()
        for e in edges:
            if e["relation"] == "DEPENDS_ON" and e["from"] in frontier and e["to"] in nodes_by_id:
                if e["to"] not in selected:
                    selected.add(e["to"])
                    next_frontier.add(e["to"])
        if not next_frontier:
            break
        frontier = next_frontier

    # Attach files read or touched by selected tasks
    selected_files: Set[str] = set()
    for e in edges:
        if e["relation"] in ("READS", "TOUCHES") and e["from"] in selected and e["to"] in nodes_by_id:
            selected.add(e["to"])
            selected_files.add(e["to"])

    # Attach dead-end warnings linked to selected tasks, selected files, or global objective
    for e in edges:
        if e["relation"] in ("WARNS_TASK", "WARNS_FILE", "WARNS_GLOBAL") and e["to"] in selected:
            selected.add(e["from"])

    sub_nodes = [n for n in graph.get("nodes", []) if n["id"] in selected]
    sub_edges = [e for e in edges if e["from"] in selected and e["to"] in selected]

    full_tok, _ = estimate_text_tokens(json.dumps(graph))
    sub_payload = {"nodes": sub_nodes, "edges": sub_edges}
    sub_tok, _ = estimate_text_tokens(json.dumps(sub_payload))
    saved_pct = round(max(0.0, ((full_tok - sub_tok) / max(1, full_tok)) * 100.0), 1)

    return {
        "target_task_ids": list(target_task_ids),
        "depth": depth,
        "nodes": sub_nodes,
        "edges": sub_edges,
        "relevant_files": sorted(nodes_by_id[fid]["path"] for fid in selected_files if "path" in nodes_by_id[fid]),
        "relevant_dead_ends": [n["label"] for n in sub_nodes if n["type"] == "dead_end"],
        "full_graph_tokens": full_tok,
        "subgraph_tokens": sub_tok,
        "tokens_saved_pct": saved_pct,
        "mermaid": render_mermaid_graph(graph, only_node_ids=selected),
    }


def cmd_plan_schedule(args: argparse.Namespace) -> Dict[str, Any]:
    workspace = Path(args.workspace).expanduser().resolve()
    if args.tasks_file:
        raw_text = Path(args.tasks_file).expanduser().read_text(encoding="utf-8")
        raw_tasks = json.loads(raw_text)
    elif args.tasks_json:
        raw_tasks = json.loads(args.tasks_json)
    else:
        raise ValueError("Provide either --tasks-json or --tasks-file.")

    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise ValueError("Task input must be a non-empty JSON array.")

    ordered_tasks = _validate_and_topo_sort(raw_tasks)
    context_limit = resolve_context_limit(args.model, args.limit)
    safe_ceiling = int(context_limit * SAFE_UTILIZATION_RATIO)
    used_tokens = max(0, int(args.used_tokens or 0))
    batch1_budget = max(MIN_BATCH1_BUDGET, safe_ceiling - used_tokens)

    warnings: List[str] = []
    expanded_tasks: List[Dict[str, Any]] = []
    rewire_map: Dict[str, str] = {}

    for task in ordered_tasks:
        remapped_deps = [rewire_map.get(d, d) for d in task["dependencies"]]
        task["dependencies"] = remapped_deps

        est_total, file_tok, per_file_tok = _estimate_task_cost(task, workspace, args.model)
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
            batches.append(
                {
                    "batch": num,
                    "budget_tokens": cap,
                    "estimated_tokens": 0,
                    "task_ids": [],
                }
            )
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

    existing = _load_schedule(workspace) or {}
    now = utc_now_iso()
    schedule: Dict[str, Any] = {
        "schema_version": 1,
        "workspace": str(workspace),
        "model": args.model or "default",
        "context_limit": context_limit,
        "safe_ceiling_tokens": safe_ceiling,
        "used_tokens_at_plan": used_tokens,
        "batch1_budget_tokens": batch1_budget,
        "objective": existing.get("objective", ""),
        "constraints": existing.get("constraints", []),
        "completed_task_ids": existing.get("completed_task_ids", []),
        "completed_records": existing.get("completed_records", []),
        "failed_attempts": existing.get("failed_attempts", []),
        "resume_pointer": existing.get("resume_pointer", ""),
        "git_anchor": existing.get("git_anchor"),
        "warnings": warnings,
        "tasks": expanded_tasks,
        "batches": batches,
        "created_at": existing.get("created_at", now),
        "updated_at": now,
    }
    sched_path = _save_schedule(workspace, schedule)
    graph = build_context_graph(schedule)
    graph_path, _ = _save_context_graph(workspace, graph, archive=False)

    return {
        "schedule_path": str(sched_path),
        "graph_path": str(graph_path),
        "model": schedule["model"],
        "context_limit": context_limit,
        "safe_ceiling_tokens": safe_ceiling,
        "batch1_budget_tokens": batch1_budget,
        "total_tasks": len(expanded_tasks),
        "total_batches": len(batches),
        "warnings": warnings,
        "batches": batches,
        "tasks": expanded_tasks,
    }


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
        reasons.append(
            f"Current session usage ({current_used}) already exceeds safe ceiling ({safe_ceiling})."
        )
    if projected_used > safe_ceiling:
        reasons.append(
            f"Projected usage ({projected_used}) would breach 68% safe ceiling ({safe_ceiling})."
        )
    if remaining_after_task < MIN_REMAINING_TOKENS:
        reasons.append(
            f"Remaining context after task ({remaining_after_task}) would drop below minimum buffer ({MIN_REMAINING_TOKENS})."
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


def capture_git_state(workspace: Path) -> Dict[str, Any]:
    if shutil.which("git") is None:
        return {
            "is_git_repo": False,
            "branch": "N/A",
            "head_short": "N/A",
            "status_short": "",
        }

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

    inside = run_git(["rev-parse", "--is-inside-work-tree"])
    if inside != "true":
        return {
            "is_git_repo": False,
            "branch": "N/A",
            "head_short": "N/A",
            "status_short": "",
        }

    branch = run_git(["rev-parse", "--abbrev-ref", "HEAD"]) or "DETACHED"
    head_short = run_git(["rev-parse", "--short", "HEAD"]) or "UNBORN"
    status_short = run_git(["status", "--short"]) or ""
    return {
        "is_git_repo": True,
        "branch": branch,
        "head_short": head_short,
        "status_short": status_short,
    }


def _render_worklog_markdown(
    timestamp_utc: str,
    model: str,
    metrics: Dict[str, Any],
    git_state: Dict[str, Any],
    objective: str,
    constraints: List[str],
    completed_records: List[Dict[str, Any]],
    pending_tasks: List[Dict[str, Any]],
    failed_attempts: List[str],
    resume_pointer: str,
    mermaid_graph: str,
) -> str:
    template_path = Path(__file__).resolve().parent.parent / "references" / "WORKLOG_TEMPLATE.md"
    if template_path.is_file():
        template = template_path.read_text(encoding="utf-8")
    else:
        template = (
            "# Context Budget Worklog\n\n"
            "## Session Telemetry\n"
            "- **Timestamp (UTC):** {{TIMESTAMP_UTC}}\n"
            "- **Model:** `{{MODEL}}`\n"
            "- **Token Usage:** {{USED_TOKENS}} / {{CONTEXT_LIMIT}} ({{UTILIZATION_PCT}}%)\n"
            "- **Safe Ceiling (68%):** {{SAFE_CEILING_TOKENS}} tokens\n"
            "- **Status:** `{{STATUS}}`\n\n"
            "## Git Anchor\n"
            "- **Repository:** {{IS_GIT_REPO}}\n"
            "- **Branch:** `{{GIT_BRANCH}}`\n"
            "- **HEAD (short):** `{{GIT_HEAD_SHORT}}`\n"
            "- **Working Tree Status:**\n```text\n{{GIT_STATUS_SHORT}}\n```\n\n"
            "## Execution & Dependency Graph\n```mermaid\n{{MERMAID_GRAPH}}\n```\n\n"
            "## Immutable Objective\n{{OBJECTIVE}}\n\n"
            "## Constraints & Guardrails\n{{CONSTRAINTS_BULLETS}}\n\n"
            "## Completed Tasks\n{{COMPLETED_TASKS_SECTION}}\n\n"
            "## Remaining Schedule\n{{REMAINING_TASKS_SECTION}}\n\n"
            "## Failed Attempts & Dead Ends (Do Not Repeat)\n{{FAILED_ATTEMPTS_BULLETS}}\n\n"
            "## Exact Resume Pointer\n> {{RESUME_POINTER}}\n"
        )

    constraints_md = "\n".join(f"- {c}" for c in constraints) if constraints else "- None recorded."
    if completed_records:
        comp_lines: List[str] = []
        for rec in completed_records:
            tid = rec.get("task_id", "unknown")
            summary = rec.get("summary", "")
            files = rec.get("files_touched", [])
            files_str = ", ".join(f"`{f}`" for f in files) if files else "none"
            verif = rec.get("verification", "not specified")
            comp_lines.append(
                f"- **`{tid}`**: {summary}\n"
                f"  - Files touched: {files_str}\n"
                f"  - Verification: {verif}"
            )
        completed_md = "\n".join(comp_lines)
    else:
        completed_md = "- None yet."

    if pending_tasks:
        rem_lines = [
            f"- [ ] **Batch {t.get('batch', 1)} · `{t.get('id')}`**: {t.get('title')} (~{t.get('estimated_tokens', 0):,} tokens)"
            for t in pending_tasks
        ]
        remaining_md = "\n".join(rem_lines)
    else:
        remaining_md = "- All scheduled tasks completed."

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
    schedule = _load_schedule(workspace) or {
        "schema_version": 1,
        "workspace": str(workspace),
        "model": args.model or "default",
        "context_limit": resolve_context_limit(args.model, args.limit),
        "safe_ceiling_tokens": int(resolve_context_limit(args.model, args.limit) * SAFE_UTILIZATION_RATIO),
        "objective": "",
        "constraints": [],
        "completed_task_ids": [],
        "completed_records": [],
        "failed_attempts": [],
        "resume_pointer": "",
        "tasks": [],
        "batches": [],
        "created_at": utc_now_iso(),
    }

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

    schedule.update(
        {
            "model": model,
            "context_limit": context_limit,
            "safe_ceiling_tokens": metrics["safe_ceiling_tokens"],
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

    # Canonical Context Graph + timestamped archive
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

    subgraph = extract_relevant_subgraph(graph, targets, depth=max(1, int(args.depth)))
    return {
        "graph_path": str(_state_dir(workspace) / "context_graph.json"),
        "graph_stats": graph.get("stats", {}),
        **subgraph,
    }


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

    # Build/load canonical context graph & extract pruned subgraph for next batch only
    graph = _load_context_graph(workspace) or build_context_graph(schedule)
    target_ids = [str(t["id"]) for t in next_batch_tasks]
    subgraph = extract_relevant_subgraph(graph, target_ids, depth=2)

    return {
        "workspace": str(workspace),
        "graph_path": str(graph_path),
        "graph_exists": graph_path.is_file(),
        "worklog_path": str(worklog_path),
        "worklog_exists": worklog_path.is_file(),
        "objective": schedule.get("objective", ""),
        "constraints": schedule.get("constraints", []),
        "resume_pointer": schedule.get("resume_pointer", ""),
        "completed_task_ids": list(schedule.get("completed_task_ids", [])),
        "pending_count": len(pending_tasks),
        "next_batch": next_batch_num,
        "next_batch_tasks": [
            {
                "id": t.get("id"),
                "title": t.get("title"),
                "files_to_read": t.get("files_to_read", []),
                "estimated_tokens": t.get("estimated_tokens", 0),
                "dependencies": t.get("dependencies", []),
            }
            for t in next_batch_tasks
        ],
        "focused_subgraph": {
            "node_count": len(subgraph["nodes"]),
            "edge_count": len(subgraph["edges"]),
            "relevant_files": subgraph["relevant_files"],
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


def _format_human_output(command: str, data: Dict[str, Any], raw_args: argparse.Namespace) -> str:
    if command == "inspect-session":
        return (
            f"[inspect-session] status={data['status']} | "
            f"used={data['used_tokens']:,}/{data['context_limit']:,} ({data['utilization_pct']}%) | "
            f"remaining={data['remaining_tokens']:,} | safe_ceiling={data['safe_ceiling_tokens']:,} | "
            f"estimator={data['estimator']} | source={data['detected_source']}"
        )
    if command == "plan-schedule":
        lines = [
            f"[plan-schedule] {data['total_tasks']} task(s) across {data['total_batches']} batch(es) "
            f"(safe_ceiling={data['safe_ceiling_tokens']:,}, batch1_budget={data['batch1_budget_tokens']:,})"
        ]
        for b in data["batches"]:
            lines.append(
                f"  Batch {b['batch']} (~{b['estimated_tokens']:,}/{b['budget_tokens']:,} tok): {', '.join(b['task_ids'])}"
            )
        for w in data["warnings"]:
            lines.append(f"  WARNING: {w}")
        lines.append(f"  Saved schedule: {data['schedule_path']}")
        lines.append(f"  Saved graph:    {data['graph_path']}")
        return "\n".join(lines)
    if command == "gate-check":
        if data["decision"] == "PROCEED":
            return (
                f"[gate-check] PROCEED task={data['task_id']} | "
                f"projected={data['projected_used_tokens']:,}/{data['safe_ceiling_tokens']:,} safe | "
                f"remaining_after={data['remaining_after_task']:,}"
            )
        return (
            f"[gate-check] HALT_AND_HANDOFF task={data['task_id']} | reason={data['reason']}"
        )
    if command == "save-worklog":
        gs = data.get("graph_stats", {})
        return (
            f"[save-worklog] Saved graph={data['graph_path']} ({gs.get('node_count', 0)} nodes, {gs.get('edge_count', 0)} edges) "
            f"and worklog={data['worklog_path']} | completed={data['completed_count']} pending={data['pending_count']}"
        )
    if command == "query-graph":
        if getattr(raw_args, "mermaid", False):
            return data["mermaid"]
        lines = [
            f"[query-graph] targets={data['target_task_ids']} | "
            f"pruned={data['subgraph_tokens']:,}/{data['full_graph_tokens']:,} tok (-{data['tokens_saved_pct']}%)",
            f"  relevant_files: {', '.join(data['relevant_files']) or '(none)'}",
            f"  relevant_dead_ends: {len(data['relevant_dead_ends'])}",
        ]
        return "\n".join(lines)
    if command == "resume-context":
        fg = data.get("focused_subgraph", {})
        lines = [
            f"[resume-context] objective={data['objective'] or '(unset)'}",
            f"  resume_pointer: {data['resume_pointer'] or '(unset)'}",
            f"  pending={data['pending_count']} | next_batch={data['next_batch']} | git_drift={data['git_drift_detected']}",
            f"  context_graph_pruning: {fg.get('subgraph_tokens', 0):,}/{fg.get('full_graph_tokens', 0):,} tok (-{fg.get('tokens_saved_pct', 0)}%)",
        ]
        for d in data["drift_details"]:
            lines.append(f"  DRIFT: {d}")
        for t in data["next_batch_tasks"]:
            lines.append(f"  - {t['id']}: {t['title']} (~{t['estimated_tokens']:,} tok)")
        return "\n".join(lines)
    return json.dumps(data, indent=2)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="budget_engine.py",
        description="Zero-dependency cross-platform context budget & Context Graph orchestrator.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # 1. inspect-session
    p_ins = sub.add_parser("inspect-session", help="Inspect current session token utilization and status.")
    p_ins.add_argument("--model", default=None, help="Active model identifier.")
    p_ins.add_argument("--limit", type=int, default=None, help="Explicit context window token limit override.")
    p_ins.add_argument("--transcript", default=None, help="Path to active session transcript file.")
    p_ins.add_argument("--files", action="append", nargs="+", default=None, help="File or directory paths to include.")
    p_ins.add_argument("--extra-tokens", type=int, default=0, help="Additional estimated tokens (e.g. system/tools).")
    p_ins.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    # 2. plan-schedule
    p_plan = sub.add_parser("plan-schedule", help="Plan token-safe sequential session batches and build context_graph.json.")
    p_plan.add_argument("--tasks-json", default=None, help="Inline JSON array of task definitions.")
    p_plan.add_argument("--tasks-file", default=None, help="Path to JSON file containing task definitions.")
    p_plan.add_argument("--model", default=None, help="Active model identifier.")
    p_plan.add_argument("--limit", type=int, default=None, help="Explicit context window token limit override.")
    p_plan.add_argument("--used-tokens", type=int, default=0, help="Current session tokens already consumed.")
    p_plan.add_argument("--workspace", default=".", help="Workspace root directory (default: .).")
    p_plan.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    # 3. gate-check
    p_gate = sub.add_parser("gate-check", help="Check whether starting a task stays within safe budget.")
    p_gate.add_argument("--task-id", required=True, help="Scheduled task ID to gate-check.")
    p_gate.add_argument("--model", default=None, help="Active model identifier.")
    p_gate.add_argument("--limit", type=int, default=None, help="Explicit context window token limit override.")
    p_gate.add_argument("--used-tokens", type=int, default=None, help="Current session tokens consumed.")
    p_gate.add_argument("--transcript", default=None, help="Optional transcript path if --used-tokens is omitted.")
    p_gate.add_argument("--workspace", default=".", help="Workspace root directory (default: .).")
    p_gate.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    # 4. save-worklog
    p_save = sub.add_parser("save-worklog", help="Persist canonical context_graph.json, condensed WORKLOG.md, and archives.")
    p_save.add_argument("--objective", default=None, help="Immutable overarching objective.")
    p_save.add_argument("--constraints-json", default=None, help="JSON array of immutable constraint strings.")
    p_save.add_argument("--completed-json", default=None, help="JSON array of completed task records.")
    p_save.add_argument("--failed-json", default=None, help="JSON array of failed attempt notes.")
    p_save.add_argument("--resume-pointer", default=None, help="Exact file/line/task pointer for next session.")
    p_save.add_argument("--model", default=None, help="Active model identifier.")
    p_save.add_argument("--limit", type=int, default=None, help="Explicit context window token limit override.")
    p_save.add_argument("--used-tokens", type=int, default=0, help="Current session tokens consumed.")
    p_save.add_argument("--workspace", default=".", help="Workspace root directory (default: .).")
    p_save.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    # 5. query-graph
    p_qg = sub.add_parser("query-graph", help="Extract a token-pruned ancestor/file/dead-end subgraph for a target task.")
    p_qg.add_argument("--task-id", default=None, help="Target task ID (or comma-separated IDs). Defaults to next pending batch.")
    p_qg.add_argument("--depth", type=int, default=2, help="Maximum upstream dependency hop depth (default: 2).")
    p_qg.add_argument("--mermaid", action="store_true", help="Output Mermaid flowchart diagram for the queried subgraph.")
    p_qg.add_argument("--workspace", default=".", help="Workspace root directory (default: .).")
    p_qg.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    # 6. resume-context
    p_res = sub.add_parser("resume-context", help="Load graph-pruned handoff context and detect git drift.")
    p_res.add_argument("--workspace", default=".", help="Workspace root directory (default: .).")
    p_res.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    dispatch = {
        "inspect-session": cmd_inspect_session,
        "plan-schedule": cmd_plan_schedule,
        "gate-check": cmd_gate_check,
        "save-worklog": cmd_save_worklog,
        "query-graph": cmd_query_graph,
        "resume-context": cmd_resume_context,
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
