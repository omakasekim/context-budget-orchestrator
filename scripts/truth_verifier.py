#!/usr/bin/env python3
"""
Zero-dependency Ground-Truth Verification & Multi-Agent Referee module for context-budget-orchestrator.

Verifies agent claims against real-world witnesses (git ancestry, diffs, test assertions, file tree,
and liveness deltas) instead of trusting self-reported "done" narration. Returns deterministic
exit-code verdicts (0 = verified/granted/moving, 1 = unverified/refused/stalled).
"""

from __future__ import annotations

import datetime
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_git(workspace: Path, args: Sequence[str]) -> Tuple[int, str, str]:
    if shutil.which("git") is None:
        return 127, "", "git binary not found"
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)


def _is_git_repo(workspace: Path) -> bool:
    rc, out, _ = _run_git(workspace, ["rev-parse", "--is-inside-work-tree"])
    return rc == 0 and out == "true"


# ---------------------------------------------------------------------------
# 1. verify_shipped: Did this task/claim ACTUALLY land in git & filesystem?
# ---------------------------------------------------------------------------

def verify_shipped(
    workspace: Path,
    claim_id: str,
    expected_files: Optional[Sequence[str]] = None,
    max_commits: int = 80,
) -> Dict[str, Any]:
    """
    Verify a claimed task/feature against real git ancestry and non-empty file witnesses.
    Never trusts agent self-reports; returns verdict SHIPPED (exit 0) or NOT_SHIPPED (exit 1).
    """
    claim = claim_id.strip()
    expected = [f.strip() for f in (expected_files or []) if f.strip()]
    if not _is_git_repo(workspace):
        return {
            "action": "shipped",
            "claim": claim,
            "verdict": "NOT_SHIPPED",
            "exit_code": 1,
            "reason": "NOT_A_GIT_REPO",
            "witness_commit": None,
            "files_verified": [],
            "missing_files": expected,
        }

    # Inspect reachable git history from HEAD
    rc, log_out, _ = _run_git(
        workspace,
        ["log", f"-n{max_commits}", "--pretty=format:%H%x1f%h%x1f%s", "HEAD"],
    )
    if rc != 0 or not log_out:
        return {
            "action": "shipped",
            "claim": claim,
            "verdict": "NOT_SHIPPED",
            "exit_code": 1,
            "reason": "NO_COMMITS_IN_ANCESTRY",
            "witness_commit": None,
            "files_verified": [],
            "missing_files": expected,
        }

    claim_re = re.compile(rf"(?i)\b{re.escape(claim)}\b")
    matched_commit: Optional[Dict[str, Any]] = None

    for line in log_out.splitlines():
        parts = line.split("\x1f")
        if len(parts) < 3:
            continue
        full_sha, short_sha, subject = parts[0], parts[1], parts[2]
        if claim_re.search(subject):
            # Verify the matched commit actually changed real lines (not an empty commit)
            rc_stat, numstat, _ = _run_git(workspace, ["show", "--numstat", "--pretty=format:", full_sha])
            added = 0
            touched: List[str] = []
            if rc_stat == 0 and numstat:
                for row in numstat.splitlines():
                    cols = row.split("\t")
                    if len(cols) >= 3:
                        if cols[0].isdigit():
                            added += int(cols[0])
                        touched.append(cols[2])
            matched_commit = {
                "sha": full_sha,
                "short_sha": short_sha,
                "subject": subject,
                "files_touched": touched,
                "lines_added": added,
            }
            break

    # Check expected file witnesses on disk (must exist & be non-empty)
    verified_files: List[str] = []
    missing_files: List[str] = []
    for rel in expected:
        fp = (workspace / rel).resolve()
        if fp.is_file() and fp.stat().st_size > 0:
            verified_files.append(rel)
        else:
            missing_files.append(rel)

    if matched_commit is None:
        return {
            "action": "shipped",
            "claim": claim,
            "verdict": "NOT_SHIPPED",
            "exit_code": 1,
            "reason": "NO_MATCHING_COMMIT_IN_ANCESTRY",
            "witness_commit": None,
            "files_verified": verified_files,
            "missing_files": missing_files,
        }

    if matched_commit["lines_added"] == 0 and not matched_commit["files_touched"]:
        return {
            "action": "shipped",
            "claim": claim,
            "verdict": "NOT_SHIPPED",
            "exit_code": 1,
            "reason": "EMPTY_COMMIT_NO_DIFF",
            "witness_commit": matched_commit,
            "files_verified": verified_files,
            "missing_files": missing_files,
        }

    if missing_files:
        return {
            "action": "shipped",
            "claim": claim,
            "verdict": "NOT_SHIPPED",
            "exit_code": 1,
            "reason": "EXPECTED_FILES_MISSING_OR_EMPTY",
            "witness_commit": matched_commit,
            "files_verified": verified_files,
            "missing_files": missing_files,
        }

    return {
        "action": "shipped",
        "claim": claim,
        "verdict": "SHIPPED",
        "exit_code": 0,
        "reason": "VERIFIED_BY_GIT_ANCESTRY",
        "witness_commit": matched_commit,
        "files_verified": verified_files,
        "missing_files": [],
    }


# ---------------------------------------------------------------------------
# 2. audit_commits: Catch hollow tests, skipped assertions & claim-vs-diff lies
# ---------------------------------------------------------------------------

HOLLOW_TEST_PATTERNS = [
    (re.compile(r"^\+\s*assert\s+True\b"), "HOLLOW_ASSERT_TRUE", "Added trivial `assert True` placeholder"),
    (re.compile(r"^\+\s*expect\s*\(\s*true\s*\)\s*\.toBe\s*\(\s*true\s*\)"), "HOLLOW_EXPECT_TRUE", "Added trivial `expect(true).toBe(true)` placeholder"),
    (re.compile(r"^\+\s*@(?:pytest\.mark\.skip|unittest\.skip)"), "TEST_SKIPPED", "Added test skip decorator silencing verification"),
    (re.compile(r"^\+\s*(?:it|test|describe)\.skip\s*\("), "TEST_SKIPPED", "Added JS/TS test.skip() silencing verification"),
    (re.compile(r"^\+\s*.*\|\|\s*true\b"), "FAILURE_SWALLOWED", "Added `|| true` swallowing command failure"),
]

TEST_FILE_RE = re.compile(r"(?:^|/)(?:test_[^/]+|[^/]+_test\.[^/]+|[^/]+\.(?:test|spec)\.[^/]+|tests?/[^/]+)$", re.IGNORECASE)
DOC_ONLY_RE = re.compile(r"\.(?:md|txt|rst|adoc)$", re.IGNORECASE)


def audit_commits(workspace: Path, rev_range: Optional[str] = None, max_commits: int = 10, strict: bool = False) -> Dict[str, Any]:
    """Audit commit messages against actual diffs to catch hollow tests, deleted assertions, or unbacked claims."""
    if not _is_git_repo(workspace):
        return {
            "action": "commit-audit",
            "exit_code": 1,
            "error": "NOT_A_GIT_REPO",
            "commits_audited": 0,
            "results": [],
        }

    git_args = ["log", f"-n{max_commits}", "--pretty=format:%H%x1f%h%x1f%s"]
    if rev_range:
        git_args.append(rev_range)
    rc, log_out, err = _run_git(workspace, git_args)
    if rc != 0:
        return {"action": "commit-audit", "exit_code": 1, "error": err or "GIT_LOG_FAILED", "commits_audited": 0, "results": []}

    results: List[Dict[str, Any]] = []
    unbacked_count = 0
    suspect_count = 0

    for line in log_out.splitlines():
        parts = line.split("\x1f")
        if len(parts) < 3:
            continue
        full_sha, short_sha, subject = parts[0], parts[1], parts[2]
        _, files_out, _ = _run_git(workspace, ["diff-tree", "--no-commit-id", "--name-only", "-r", "--root", full_sha])
        files = [f.strip() for f in files_out.splitlines() if f.strip()]
        _, diff_out, _ = _run_git(workspace, ["show", "--unified=0", "--pretty=format:", full_sha])

        flags: List[Dict[str, str]] = []
        added_lines = 0
        removed_asserts = 0
        added_asserts = 0
        added_test_defs = 0
        current_file = ""

        for dline in diff_out.splitlines():
            if dline.startswith("+++ b/"):
                current_file = dline[6:].strip()
                continue
            if dline.startswith("+") and not dline.startswith("+++"):
                added_lines += 1
                if re.search(r"\b(?:assert|expect\s*\(|self\.assert)", dline):
                    added_asserts += 1
                if TEST_FILE_RE.search(current_file):
                    if re.match(r"^\+\s*(?:def\s+test_|(?:it|test)\s*\()", dline):
                        added_test_defs += 1
                    for pat, code, desc in HOLLOW_TEST_PATTERNS:
                        if pat.search(dline):
                            flags.append({"code": code, "detail": f"{desc} in {current_file}"})
            elif dline.startswith("-") and not dline.startswith("---"):
                if TEST_FILE_RE.search(current_file) and re.search(r"\b(?:assert|expect\s*\(|self\.assert)", dline):
                    removed_asserts += 1

        subj_low = subject.lower()
        claims_tests = bool(re.search(r"\b(?:test|tests|testing|spec|coverage)\b", subj_low))
        claims_code = bool(re.search(r"\b(?:fix|implement|add|refactor|ship|endpoint|feature)\b", subj_low))

        if not files or added_lines == 0:
            flags.append({"code": "EMPTY_DIFF", "detail": "Commit has no added lines backing its claim"})
        if claims_tests and not any(TEST_FILE_RE.search(f) for f in files):
            flags.append({"code": "CLAIMED_TESTS_NOT_TOUCHED", "detail": "Commit message claims tests, but no test files were modified"})
        if added_test_defs > 0 and added_asserts == 0:
            flags.append({"code": "ASSERTIONLESS_TEST", "detail": f"Added {added_test_defs} test function(s) with zero assertions"})
        if removed_asserts > 0 and added_asserts == 0:
            flags.append({"code": "ASSERTIONS_DELETED", "detail": f"Deleted {removed_asserts} assertion(s) without adding replacements"})
        if claims_code and files and all(DOC_ONLY_RE.search(f) for f in files):
            flags.append({"code": "DOCS_ONLY_FOR_CODE_CLAIM", "detail": "Commit claims code implementation/fix but only touched docs"})

        # Score & verdict
        severe_codes = {"EMPTY_DIFF", "HOLLOW_ASSERT_TRUE", "HOLLOW_EXPECT_TRUE", "ASSERTIONLESS_TEST", "CLAIMED_TESTS_NOT_TOUCHED", "DOCS_ONLY_FOR_CODE_CLAIM"}
        has_severe = any(fl["code"] in severe_codes for fl in flags)
        score = max(0, 100 - len(flags) * 35)
        if has_severe:
            verdict = "UNBACKED"
            unbacked_count += 1
        elif flags:
            verdict = "SUSPECT"
            suspect_count += 1
        else:
            verdict = "BACKED"

        results.append(
            {
                "sha": short_sha,
                "subject": subject,
                "verdict": verdict,
                "score": score,
                "files_count": len(files),
                "added_lines": added_lines,
                "flags": flags,
            }
        )

    exit_code = 1 if (unbacked_count > 0 and strict) or (unbacked_count > 0 and len(results) == 1) else 0
    return {
        "action": "commit-audit",
        "exit_code": exit_code,
        "commits_audited": len(results),
        "backed_count": sum(1 for r in results if r["verdict"] == "BACKED"),
        "suspect_count": suspect_count,
        "unbacked_count": unbacked_count,
        "results": results,
    }


# ---------------------------------------------------------------------------
# 3. check_liveness: Is the agent ACTUALLY advancing or spinning in circles?
# ---------------------------------------------------------------------------

def check_liveness(workspace: Path, state_dir: Path, stall_threshold: int = 3) -> Dict[str, Any]:
    """
    Track real-world workspace fingerprint (HEAD + git diff hash + schedule completion hash).
    Detects NO_DELTA stalls and A->B->A oscillation loops independent of agent narration.
    """
    journal_path = state_dir / "liveness_journal.json"
    journal: List[Dict[str, Any]] = []
    if journal_path.is_file():
        try:
            loaded = json.loads(journal_path.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                journal = loaded
        except (OSError, json.JSONDecodeError):
            journal = []

    _, head_sha, _ = _run_git(workspace, ["rev-parse", "HEAD"])
    _, status_out, _ = _run_git(workspace, ["status", "--porcelain", "--", ".", ":!.agent_state"])
    _, diff_out, _ = _run_git(workspace, ["diff", "--", ".", ":!.agent_state"])
    sched_path = state_dir / "schedule.json"
    sched_completed = ""
    if sched_path.is_file():
        try:
            sdoc = json.loads(sched_path.read_text(encoding="utf-8"))
            sched_completed = ",".join(sorted(str(x) for x in sdoc.get("completed_task_ids", [])))
        except (OSError, json.JSONDecodeError):
            pass

    witness_blob = f"{head_sha}|{status_out}|{diff_out}|{sched_completed}".encode("utf-8", errors="ignore")
    fingerprint = hashlib.sha256(witness_blob).hexdigest()[:16]

    now = _utc_now_iso()
    entry = {"timestamp": now, "fingerprint": fingerprint, "head": head_sha[:7] if head_sha else "N/A"}
    journal.append(entry)
    journal = journal[-30:]

    state_dir.mkdir(parents=True, exist_ok=True)
    journal_path.write_text(json.dumps(journal, indent=2) + "\n", encoding="utf-8")

    # Count consecutive identical fingerprints from the tail
    consecutive_same = 1
    for prev in reversed(journal[:-1]):
        if prev.get("fingerprint") == fingerprint:
            consecutive_same += 1
        else:
            break

    # Detect A -> B -> A oscillation loop over last 4 samples
    oscillation = False
    if len(journal) >= 4:
        fps = [j["fingerprint"] for j in journal[-4:]]
        if fps[0] == fps[2] and fps[1] == fps[3] and fps[0] != fps[1]:
            oscillation = True

    if oscillation:
        return {
            "action": "liveness",
            "verdict": "STALLED",
            "exit_code": 1,
            "reason": "OSCILLATION_LOOP",
            "fingerprint": fingerprint,
            "consecutive_stalls": consecutive_same,
            "samples_recorded": len(journal),
        }

    if consecutive_same >= max(2, stall_threshold):
        return {
            "action": "liveness",
            "verdict": "STALLED",
            "exit_code": 1,
            "reason": "NO_WORLD_DELTA",
            "fingerprint": fingerprint,
            "consecutive_stalls": consecutive_same,
            "samples_recorded": len(journal),
        }

    return {
        "action": "liveness",
        "verdict": "MOVING",
        "exit_code": 0,
        "reason": "PROGRESS_OBSERVED" if len(journal) > 1 and journal[-2]["fingerprint"] != fingerprint else "BASELINE_RECORDED",
        "fingerprint": fingerprint,
        "consecutive_stalls": consecutive_same - 1,
        "samples_recorded": len(journal),
    }


# ---------------------------------------------------------------------------
# 4. arbitrate_access: Deterministic Multi-Agent File & Dependency Referee
# ---------------------------------------------------------------------------

def arbitrate_access(
    workspace: Path,
    agent_id: str,
    files: Sequence[str],
    active_leases: Dict[str, Any],
    dependencies: Optional[Sequence[str]] = None,
    require_shipped_deps: bool = False,
) -> Dict[str, Any]:
    """
    Deterministic referee answering GRANTED (exit 0) or REFUSED (exit 1) with closed-vocabulary reason codes.
    """
    req_files = sorted({f.strip().lstrip("./") for f in files if f.strip()})
    refusals: List[Dict[str, str]] = []

    # 1. Check active leases held by other agents
    for tid, lease in active_leases.items():
        holder = str(lease.get("agent_id", ""))
        if holder == agent_id:
            continue
        leased_files = {str(f).strip().lstrip("./") for f in lease.get("files", [])}
        overlap = sorted(set(req_files) & leased_files)
        if overlap:
            refusals.append(
                {
                    "code": "LEASE_HELD_BY_OTHER",
                    "detail": f"Files {', '.join(overlap)} are leased by '{holder}' (task '{tid}').",
                }
            )

    # 2. Check upstream dependencies actually shipped in git if required
    if require_shipped_deps and dependencies:
        for dep in dependencies:
            res = verify_shipped(workspace, dep)
            if res["verdict"] != "SHIPPED":
                refusals.append(
                    {
                        "code": "DEPENDENCY_NOT_SHIPPED",
                        "detail": f"Upstream dependency '{dep}' has not shipped in git ({res['reason']}).",
                    }
                )

    if refusals:
        return {
            "action": "arbitrate",
            "verdict": "REFUSED",
            "exit_code": 1,
            "agent_id": agent_id,
            "requested_files": req_files,
            "refusals": refusals,
        }

    return {
        "action": "arbitrate",
        "verdict": "GRANTED",
        "exit_code": 0,
        "agent_id": agent_id,
        "requested_files": req_files,
        "refusals": [],
    }


# ---------------------------------------------------------------------------
# 5. Human Escalation Queue (decisions needing human intervention)
# ---------------------------------------------------------------------------

def manage_escalations(
    state_dir: Path,
    sub_action: str,
    title: str = "",
    detail: str = "",
    severity: str = "HIGH",
    item_id: Optional[str] = None,
) -> Dict[str, Any]:
    esc_path = state_dir / "escalations.json"
    items: List[Dict[str, Any]] = []
    if esc_path.is_file():
        try:
            loaded = json.loads(esc_path.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                items = loaded
        except (OSError, json.JSONDecodeError):
            items = []

    if sub_action == "add":
        if not title.strip():
            raise ValueError("--title is required when adding an escalation.")
        eid = item_id or f"ESC-{len(items) + 1:02d}"
        entry = {
            "id": eid,
            "title": title.strip(),
            "detail": detail.strip(),
            "severity": severity.upper(),
            "status": "open",
            "created_at": _utc_now_iso(),
        }
        items.append(entry)
        state_dir.mkdir(parents=True, exist_ok=True)
        esc_path.write_text(json.dumps(items, indent=2) + "\n", encoding="utf-8")
        return {"action": "escalate", "sub_action": "add", "exit_code": 0, "item": entry, "open_count": sum(1 for i in items if i["status"] == "open")}

    if sub_action == "resolve":
        if not item_id:
            raise ValueError("--id is required to resolve an escalation.")
        found = False
        for it in items:
            if it.get("id") == item_id:
                it["status"] = "resolved"
                it["resolved_at"] = _utc_now_iso()
                found = True
        state_dir.mkdir(parents=True, exist_ok=True)
        esc_path.write_text(json.dumps(items, indent=2) + "\n", encoding="utf-8")
        return {"action": "escalate", "sub_action": "resolve", "exit_code": 0 if found else 1, "resolved": found, "id": item_id}

    open_items = [i for i in items if i.get("status") == "open"]
    return {
        "action": "escalate",
        "sub_action": "list",
        "exit_code": 0,
        "open_count": len(open_items),
        "total_count": len(items),
        "open_items": open_items,
    }
