#!/usr/bin/env python3
"""Exhaustive unittest suite for context-budget-orchestrator v2.0 (all 12 subcommands + hook adapter)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ENGINE_PATH = SCRIPT_DIR / "budget_engine.py"
HOOK_PATH = SCRIPT_DIR / "hook_adapter.py"
sys.path.insert(0, str(SCRIPT_DIR))

import budget_engine  # noqa: E402


class FlagshipBudgetOrchestratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run_cli(
        self,
        *cli_args: str,
        env_override: dict | None = None,
        stdin_text: str | None = None,
    ) -> tuple[int, dict]:
        env = os.environ.copy()
        if env_override:
            env.update(env_override)
        proc = subprocess.run(
            [sys.executable, str(ENGINE_PATH), *cli_args, "--json"],
            cwd=str(self.workspace),
            env=env,
            input=stdin_text,
            capture_output=True,
            text=True,
            check=False,
        )
        payload = json.loads(proc.stdout) if proc.stdout.strip() else {}
        return proc.returncode, payload

    def test_01_token_estimation_and_binary_skip(self) -> None:
        (self.workspace / "sample.py").write_text("def add(a: int, b: int) -> int:\n    return a + b\n" * 40, encoding="utf-8")
        (self.workspace / "blob.bin").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR")
        nm = self.workspace / "node_modules"
        nm.mkdir()
        (nm / "huge.js").write_text("const x = 1;\n" * 5000, encoding="utf-8")

        tokens, count, skipped = budget_engine.measure_path_tokens(self.workspace, model="claude-sonnet-4-6")
        self.assertEqual(count, 1)
        self.assertGreater(tokens, 100)
        self.assertTrue(any("blob.bin" in s for s in skipped))

    def test_02_inspect_session_provider_usage_and_rot_detection(self) -> None:
        transcript = self.workspace / "claude_session.jsonl"
        # Simulate Claude JSONL with exact usage telemetry + noisy tool outputs + edit thrash loop
        records = [
            {"role": "user", "content": "Fix auth bug"},
            {"role": "assistant", "content": '{"path": "src/auth.py"}', "usage": {"input_tokens": 25000, "cache_read_input_tokens": 115000, "output_tokens": 2000}},
            {"role": "tool", "type": "tool_result", "content": "Traceback error: failed test\n" * 200},
            {"role": "tool", "type": "tool_result", "content": "Traceback error: failed test\n" * 200},
            {"role": "tool", "type": "tool_result", "content": "Traceback error: failed test\n" * 200},
            {"role": "assistant", "content": '{"path": "src/auth.py"} {"path": "src/auth.py"} {"path": "src/auth.py"}'},
        ]
        transcript.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

        rc, data = self._run_cli(
            "inspect-session",
            "--model",
            "claude-sonnet-4-6",
            "--limit",
            "200000",
            env_override={"CLAUDE_CODE_SESSION": str(transcript)},
        )
        self.assertEqual(rc, 0)
        self.assertEqual(data["telemetry_mode"], "provider_usage_exact")
        self.assertEqual(data["used_tokens"], 142000)
        self.assertEqual(data["status"], "CRITICAL_HANDOFF")
        self.assertGreaterEqual(len(data["rot_alerts"]), 2)

    def test_03_auto_plan_static_import_discovery_and_parallel_waves(self) -> None:
        src = self.workspace / "src"
        src.mkdir()
        (src / "utils.py").write_text("def clean(x): return x.strip()\n", encoding="utf-8")
        (src / "auth.py").write_text("from src import utils\ndef verify(t): return utils.clean(t)\n", encoding="utf-8")
        (src / "billing.py").write_text("def charge(n): return n * 100\n", encoding="utf-8")

        rc, plan = self._run_cli(
            "auto-plan",
            "--path",
            "src",
            "--action",
            "Add strict typing",
            "--objective",
            "Type all src modules",
            "--workspace",
            str(self.workspace),
        )
        self.assertEqual(rc, 0)
        self.assertEqual(plan["discovered_files"], 3)
        self.assertGreaterEqual(plan["import_edges_detected"], 1)
        self.assertGreaterEqual(plan["total_parallel_waves"], 2)
        # utils.py and billing.py have no dependencies -> Wave 1 should be parallel_safe
        self.assertTrue(plan["parallel_waves"][0]["parallel_safe"])

    def test_04_plan_schedule_splitting_cycles_and_batches(self) -> None:
        files = []
        for i in range(4):
            fp = self.workspace / f"mod_{i}.py"
            fp.write_text(f"def fn_{i}():\n    return 'payload_{i}' * 10\n" * 600, encoding="utf-8")
            files.append(fp.name)

        tasks = [
            {"id": "mega", "title": "Audit 4 large modules", "files_to_read": files, "expected_output_tokens": 20000, "dependencies": []},
            {"id": "fin", "title": "Summarize", "files_to_read": [], "expected_output_tokens": 1000, "dependencies": ["mega"]},
        ]
        rc, plan = self._run_cli(
            "plan-schedule",
            "--limit",
            "50000",
            "--workspace",
            str(self.workspace),
            "--tasks-json",
            json.dumps(tasks),
        )
        self.assertEqual(rc, 0)
        self.assertGreaterEqual(len(plan["warnings"]), 1)
        ids = [t["id"] for t in plan["tasks"]]
        self.assertIn("mega__part1", ids)
        self.assertIn("mega__part2", ids)
        fin = next(t for t in plan["tasks"] if t["id"] == "fin")
        self.assertTrue(fin["dependencies"][0].startswith("mega__part"))

        # Cycle check
        cyclic = [
            {"id": "a", "title": "A", "files_to_read": [], "expected_output_tokens": 100, "dependencies": ["b"]},
            {"id": "b", "title": "B", "files_to_read": [], "expected_output_tokens": 100, "dependencies": ["a"]},
        ]
        rc_c, err_c = self._run_cli("plan-schedule", "--workspace", str(self.workspace), "--tasks-json", json.dumps(cyclic))
        self.assertEqual(rc_c, 1)
        self.assertIn("Circular dependency", err_c["error"])

    def test_05_multi_agent_leases_and_gate_check_conflict_guard(self) -> None:
        (self.workspace / "shared.py").write_text("X = 1\n", encoding="utf-8")
        (self.workspace / "other.py").write_text("Y = 2\n", encoding="utf-8")
        tasks = [
            {"id": "t1", "title": "Edit shared A", "files_to_read": ["shared.py"], "expected_output_tokens": 1000, "dependencies": []},
            {"id": "t2", "title": "Edit shared B", "files_to_read": ["shared.py"], "expected_output_tokens": 1000, "dependencies": []},
            {"id": "t3", "title": "Edit other", "files_to_read": ["other.py"], "expected_output_tokens": 1000, "dependencies": []},
        ]
        self._run_cli("plan-schedule", "--workspace", str(self.workspace), "--tasks-json", json.dumps(tasks))

        # Worker-1 claims t1
        rc, claim1 = self._run_cli("lease", "--action", "claim", "--task-id", "t1", "--agent-id", "worker-1", "--workspace", str(self.workspace))
        self.assertEqual(rc, 0)
        self.assertTrue(claim1["granted"])

        # Worker-2 cannot claim t2 because shared.py is locked
        rc_conf, err_conf = self._run_cli("lease", "--action", "claim", "--task-id", "t2", "--agent-id", "worker-2", "--workspace", str(self.workspace))
        self.assertEqual(rc_conf, 1)
        self.assertIn("File conflict", err_conf["error"])

        # gate-check also blocks worker-2 on t2 due to active file lock
        rc_g, gate_block = self._run_cli("gate-check", "--task-id", "t2", "--agent-id", "worker-2", "--used-tokens", "10000", "--workspace", str(self.workspace))
        self.assertEqual(rc_g, 0)
        self.assertEqual(gate_block["decision"], "HALT_AND_HANDOFF")
        self.assertIn("File lock conflict", gate_block["reason"])

        # Worker-2 CAN claim file-disjoint t3
        rc3, claim3 = self._run_cli("lease", "--action", "claim", "--task-id", "t3", "--agent-id", "worker-2", "--workspace", str(self.workspace))
        self.assertEqual(rc3, 0)
        self.assertTrue(claim3["granted"])

        # Complete t1 releases lock and marks task done
        rc_done, _ = self._run_cli("lease", "--action", "complete", "--task-id", "t1", "--agent-id", "worker-1", "--workspace", str(self.workspace))
        self.assertEqual(rc_done, 0)
        rc_ok, gate_ok = self._run_cli("gate-check", "--task-id", "t2", "--agent-id", "worker-2", "--used-tokens", "10000", "--workspace", str(self.workspace))
        self.assertEqual(rc_ok, 0)
        self.assertEqual(gate_ok["decision"], "PROCEED")

    def test_06_compress_output_and_verify_handoff(self) -> None:
        noisy_log = "\n".join(
            ["pytest session starts"]
            + [f"test_case_{i} PASSED" for i in range(180)]
            + ["FAILED src/auth.py:42 AssertionError: expected 200 got 401"]
            + [f"cleanup step {i} ok" for i in range(40)]
            + ["1 failed, 180 passed in 2.1s"]
        )
        rc, comp = self._run_cli("compress-output", "--max-lines", "45", stdin_text=noisy_log)
        self.assertEqual(rc, 0)
        self.assertGreater(comp["tokens_saved_pct"], 60.0)
        self.assertIn("AssertionError: expected 200 got 401", comp["compressed_text"])

        # verify-handoff catches broken syntax and passes after fix
        bad_py = self.workspace / "broken.py"
        bad_py.write_text("def oops(:\n    pass\n", encoding="utf-8")
        rc, v_bad = self._run_cli("verify-handoff", "--files", "broken.py", "--workspace", str(self.workspace))
        self.assertEqual(rc, 0)
        self.assertFalse(v_bad["handoff_ready"])
        self.assertTrue(any("syntax error" in i.lower() for i in v_bad["issues"]))

        bad_py.write_text("def ok():\n    return 42\n", encoding="utf-8")
        rc, v_ok = self._run_cli(
            "verify-handoff",
            "--files",
            "broken.py",
            "--verify-cmd",
            f"{sys.executable} -c 'import broken; assert broken.ok() == 42'",
            "--workspace",
            str(self.workspace),
        )
        self.assertEqual(rc, 0)
        self.assertTrue(v_ok["handoff_ready"])
        self.assertTrue(v_ok["verify_cmd_result"]["passed"])

    def test_07_decisions_graph_budget_pruning_resume_status_and_hook(self) -> None:
        subprocess.run(["git", "init", "-b", "main"], cwd=str(self.workspace), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "dev@example.com"], cwd=str(self.workspace), check=True)
        subprocess.run(["git", "config", "user.name", "Dev"], cwd=str(self.workspace), check=True)
        for name in ("auth_a.py", "auth_b.py", "billing_a.py", "billing_b.py"):
            (self.workspace / name).write_text(f"# {name}\nV = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=str(self.workspace), check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(self.workspace), check=True, capture_output=True)

        tasks = [
            {"id": "auth1", "title": "Auth parser", "files_to_read": ["auth_a.py"], "expected_output_tokens": 1000, "dependencies": []},
            {"id": "auth2", "title": "Auth middleware", "files_to_read": ["auth_b.py"], "expected_output_tokens": 1000, "dependencies": ["auth1"]},
            {"id": "bill1", "title": "Billing ledger", "files_to_read": ["billing_a.py"], "expected_output_tokens": 1000, "dependencies": []},
            {"id": "bill2", "title": "Billing webhook", "files_to_read": ["billing_b.py"], "expected_output_tokens": 1000, "dependencies": ["bill1"]},
        ]
        self._run_cli("plan-schedule", "--workspace", str(self.workspace), "--tasks-json", json.dumps(tasks))

        # Record architectural decision linked to auth2
        rc, dec = self._run_cli(
            "record-decision",
            "--id",
            "ADR-01",
            "--decision",
            "Enforce RS256 asymmetric JWT verification only",
            "--rationale",
            "Prevent HS256 key confusion attacks",
            "--tasks",
            "auth2",
            "--files",
            "auth_b.py",
            "--workspace",
            str(self.workspace),
        )
        self.assertEqual(rc, 0)
        self.assertEqual(dec["decision"]["id"], "ADR-01")

        rc, saved = self._run_cli(
            "save-worklog",
            "--objective",
            "Harden auth and billing",
            "--constraints-json",
            json.dumps(["Zero third-party deps"]),
            "--completed-json",
            json.dumps([
                {"task_id": "auth1", "summary": "Built parser", "files_touched": ["auth_a.py"], "verification": "green"},
                {"task_id": "bill1", "summary": "Built ledger", "files_touched": ["billing_a.py"], "verification": "green"},
            ]),
            "--failed-json",
            json.dumps(["Do not bypass clock skew in auth2", "Legacy Stripe v1 failed in bill2"]),
            "--resume-pointer",
            "Start auth2 in auth_b.py:1",
            "--used-tokens",
            "62000",
            "--limit",
            "100000",
            "--workspace",
            str(self.workspace),
        )
        self.assertEqual(rc, 0)
        self.assertIn("ADR-01", Path(saved["worklog_path"]).read_text(encoding="utf-8"))

        # Query graph with --token-budget and verify DOT + Mermaid + ADR retention + branch pruning
        rc, qg = self._run_cli(
            "query-graph",
            "--task-id",
            "auth2",
            "--depth",
            "2",
            "--token-budget",
            "1200",
            "--workspace",
            str(self.workspace),
        )
        self.assertEqual(rc, 0)
        nids = {n["id"] for n in qg["nodes"]}
        self.assertIn("task:auth2", nids)
        self.assertIn("decision:ADR-01", nids)
        self.assertNotIn("task:bill1", nids)
        self.assertIn("digraph ContextGraph", qg["dot"])
        self.assertIn("task_auth1 --> task_auth2", qg["mermaid"])

        # Status dashboard
        rc, st = self._run_cli("status", "--workspace", str(self.workspace), "--limit", "100000", "--extra-tokens", "50000")
        self.assertEqual(rc, 0)
        self.assertEqual(st["decision_count"], 1)
        self.assertEqual(st["completed_count"], 2)

        # Resume + Git drift check
        (self.workspace / "auth_b.py").write_text("V = 2\n", encoding="utf-8")
        rc, res = self._run_cli("resume-context", "--workspace", str(self.workspace))
        self.assertEqual(rc, 0)
        self.assertTrue(res["git_drift_detected"])
        self.assertEqual(res["decisions"][0]["id"], "ADR-01")

        # Hook adapter smoke test (simulates Claude Code PreToolUse JSON on stdin)
        hook_proc = subprocess.run(
            [sys.executable, str(HOOK_PATH), "--model", "claude-sonnet-4-6", "--limit", "100000"],
            input=json.dumps({"extra_tokens": 72000}),
            capture_output=True,
            text=True,
            check=True,
        )
        hook_resp = json.loads(hook_proc.stdout)
        self.assertTrue(hook_resp["continue"])
        self.assertEqual(hook_resp["status"], "CRITICAL_HANDOFF")
        self.assertIn("CRITICAL_HANDOFF", hook_resp["systemMessage"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
