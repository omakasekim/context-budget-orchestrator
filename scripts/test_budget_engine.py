#!/usr/bin/env python3
"""Comprehensive unittest suite for context-budget-orchestrator."""

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
sys.path.insert(0, str(SCRIPT_DIR))

import budget_engine  # noqa: E402


class BudgetEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run_cli(self, *cli_args: str, env_override: dict | None = None) -> tuple[int, dict]:
        env = os.environ.copy()
        if env_override:
            env.update(env_override)
        proc = subprocess.run(
            [sys.executable, str(ENGINE_PATH), *cli_args, "--json"],
            cwd=str(self.workspace),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        payload = json.loads(proc.stdout) if proc.stdout.strip() else {}
        return proc.returncode, payload

    def test_token_estimation_and_binary_skip(self) -> None:
        code_file = self.workspace / "sample.py"
        code_file.write_text("def add(a: int, b: int) -> int:\n    return a + b\n" * 40, encoding="utf-8")

        bin_file = self.workspace / "blob.bin"
        bin_file.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR")

        ignored_dir = self.workspace / "node_modules"
        ignored_dir.mkdir()
        (ignored_dir / "huge.js").write_text("const x = 1;\n" * 5000, encoding="utf-8")

        tokens, count, skipped = budget_engine.measure_path_tokens(self.workspace, model="claude-sonnet-4-6")
        self.assertEqual(count, 1)
        self.assertGreater(tokens, 100)
        self.assertTrue(any("blob.bin" in s for s in skipped))

    def test_inspect_session_statuses_and_env_transcript(self) -> None:
        transcript = self.workspace / "session.jsonl"
        transcript.write_text('{"role":"user","content":"hello world"}\n' * 10, encoding="utf-8")

        rc, green = self._run_cli(
            "inspect-session",
            "--model",
            "claude-sonnet-4-6",
            "--limit",
            "100000",
            "--extra-tokens",
            "20000",
            env_override={"CLAUDE_CODE_SESSION": str(transcript)},
        )
        self.assertEqual(rc, 0)
        self.assertEqual(green["status"], "GREEN")
        self.assertEqual(green["detected_source"], "env:CLAUDE_CODE_SESSION")
        self.assertEqual(green["safe_ceiling_tokens"], 68000)

        rc, yellow = self._run_cli(
            "inspect-session",
            "--model",
            "claude-sonnet-4-6",
            "--limit",
            "100000",
            "--transcript",
            str(transcript),
            "--extra-tokens",
            "60000",
        )
        self.assertEqual(rc, 0)
        self.assertEqual(yellow["status"], "YELLOW")

        rc, crit = self._run_cli(
            "inspect-session",
            "--model",
            "claude-sonnet-4-6",
            "--limit",
            "100000",
            "--transcript",
            str(transcript),
            "--extra-tokens",
            "70000",
        )
        self.assertEqual(rc, 0)
        self.assertEqual(crit["status"], "CRITICAL_HANDOFF")

        rc, low_rem = self._run_cli(
            "inspect-session",
            "--limit",
            "40000",
            "--transcript",
            str(transcript),
            "--extra-tokens",
            "18000",
        )
        self.assertEqual(rc, 0)
        self.assertEqual(low_rem["status"], "CRITICAL_HANDOFF")

    def test_plan_schedule_multi_batch_dependencies_and_cycle_detection(self) -> None:
        (self.workspace / "a.py").write_text("x = 1\n" * 800, encoding="utf-8")
        (self.workspace / "b.py").write_text("y = 2\n" * 800, encoding="utf-8")
        (self.workspace / "c.py").write_text("z = 3\n" * 800, encoding="utf-8")

        tasks = [
            {"id": "t1", "title": "Refactor A", "files_to_read": ["a.py"], "expected_output_tokens": 12000, "dependencies": []},
            {"id": "t2", "title": "Refactor B", "files_to_read": ["b.py"], "expected_output_tokens": 12000, "dependencies": ["t1"]},
            {"id": "t3", "title": "Refactor C", "files_to_read": ["c.py"], "expected_output_tokens": 12000, "dependencies": ["t2"]},
        ]

        rc, plan = self._run_cli(
            "plan-schedule",
            "--limit",
            "100000",
            "--used-tokens",
            "40000",
            "--workspace",
            str(self.workspace),
            "--tasks-json",
            json.dumps(tasks),
        )
        self.assertEqual(rc, 0)
        self.assertGreaterEqual(plan["total_batches"], 2)
        batch_of = {t["id"]: t["batch"] for t in plan["tasks"]}
        self.assertLessEqual(batch_of["t1"], batch_of["t2"])
        self.assertLessEqual(batch_of["t2"], batch_of["t3"])
        self.assertTrue((self.workspace / ".agent_state" / "schedule.json").is_file())
        self.assertTrue((self.workspace / ".agent_state" / "context_graph.json").is_file())

        cyclic = [
            {"id": "x1", "title": "Cycle 1", "files_to_read": [], "expected_output_tokens": 500, "dependencies": ["x2"]},
            {"id": "x2", "title": "Cycle 2", "files_to_read": [], "expected_output_tokens": 500, "dependencies": ["x1"]},
        ]
        rc_cyc, err_cyc = self._run_cli(
            "plan-schedule",
            "--workspace",
            str(self.workspace),
            "--tasks-json",
            json.dumps(cyclic),
        )
        self.assertEqual(rc_cyc, 1)
        self.assertIn("Circular dependency", err_cyc["error"])

    def test_plan_schedule_oversized_task_auto_splitting(self) -> None:
        files = []
        for i in range(4):
            fp = self.workspace / f"module_{i}.py"
            fp.write_text(f"def fn_{i}():\n    return 'payload_{i}' * 10\n" * 600, encoding="utf-8")
            files.append(fp.name)

        tasks = [
            {
                "id": "mega",
                "title": "Audit 4 large modules",
                "files_to_read": files,
                "expected_output_tokens": 20000,
                "dependencies": [],
            },
            {
                "id": "finalize",
                "title": "Summarize audit",
                "files_to_read": [],
                "expected_output_tokens": 1000,
                "dependencies": ["mega"],
            },
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
        task_ids = [t["id"] for t in plan["tasks"]]
        self.assertIn("mega__part1", task_ids)
        self.assertIn("mega__part2", task_ids)
        fin = next(t for t in plan["tasks"] if t["id"] == "finalize")
        self.assertTrue(fin["dependencies"][0].startswith("mega__part"))

    def test_gate_check_proceed_and_halt(self) -> None:
        (self.workspace / "small.py").write_text("print('ok')\n" * 20, encoding="utf-8")
        tasks = [
            {"id": "t1", "title": "Small edit", "files_to_read": ["small.py"], "expected_output_tokens": 5000, "dependencies": []}
        ]
        rc, _ = self._run_cli(
            "plan-schedule",
            "--limit",
            "100000",
            "--workspace",
            str(self.workspace),
            "--tasks-json",
            json.dumps(tasks),
        )
        self.assertEqual(rc, 0)

        rc_ok, ok = self._run_cli(
            "gate-check",
            "--task-id",
            "t1",
            "--limit",
            "100000",
            "--used-tokens",
            "20000",
            "--workspace",
            str(self.workspace),
        )
        self.assertEqual(rc_ok, 0)
        self.assertEqual(ok["decision"], "PROCEED")
        self.assertGreater(ok["remaining_after_task"], 25000)

        rc_halt, halt = self._run_cli(
            "gate-check",
            "--task-id",
            "t1",
            "--limit",
            "100000",
            "--used-tokens",
            "65000",
            "--workspace",
            str(self.workspace),
        )
        self.assertEqual(rc_halt, 0)
        self.assertEqual(halt["decision"], "HALT_AND_HANDOFF")
        self.assertIn("safe ceiling", halt["reason"])

    def test_save_worklog_preserves_contract_and_resume_detects_git_drift(self) -> None:
        subprocess.run(["git", "init", "-b", "main"], cwd=str(self.workspace), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(self.workspace), check=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(self.workspace), check=True)
        (self.workspace / "app.py").write_text("VERSION = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "app.py"], cwd=str(self.workspace), check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(self.workspace), check=True, capture_output=True)

        tasks = [
            {"id": "t1", "title": "Setup config", "files_to_read": ["app.py"], "expected_output_tokens": 1000, "dependencies": []},
            {"id": "t2", "title": "Add CLI flag", "files_to_read": ["app.py"], "expected_output_tokens": 2000, "dependencies": ["t1"]},
        ]
        rc, _ = self._run_cli(
            "plan-schedule",
            "--workspace",
            str(self.workspace),
            "--tasks-json",
            json.dumps(tasks),
        )
        self.assertEqual(rc, 0)

        rc, saved1 = self._run_cli(
            "save-worklog",
            "--objective",
            "Ship zero-dependency auth refactor",
            "--constraints-json",
            json.dumps(["No third-party packages", "Keep CLI backwards compatible"]),
            "--completed-json",
            json.dumps([{"task_id": "t1", "summary": "Initialized config", "files_touched": ["app.py"], "verification": "unit tests ok"}]),
            "--failed-json",
            json.dumps(["Avoided monkeypatching os.environ globally for t2"]),
            "--resume-pointer",
            "Begin task t2 at app.py:1",
            "--used-tokens",
            "62000",
            "--limit",
            "100000",
            "--workspace",
            str(self.workspace),
        )
        self.assertEqual(rc, 0)
        self.assertTrue(Path(saved1["worklog_path"]).is_file())
        self.assertTrue(Path(saved1["archive_path"]).is_file())
        self.assertTrue(Path(saved1["graph_path"]).is_file())
        self.assertTrue(Path(saved1["graph_archive_path"]).is_file())
        self.assertIn("flowchart LR", Path(saved1["worklog_path"]).read_text(encoding="utf-8"))

        rc, saved2 = self._run_cli(
            "save-worklog",
            "--resume-pointer",
            "Continue task t2 at app.py:1",
            "--used-tokens",
            "65000",
            "--limit",
            "100000",
            "--workspace",
            str(self.workspace),
        )
        self.assertEqual(rc, 0)
        self.assertEqual(saved2["objective"], "Ship zero-dependency auth refactor")
        self.assertEqual(saved2["constraints"], ["No third-party packages", "Keep CLI backwards compatible"])

        rc, res_clean = self._run_cli("resume-context", "--workspace", str(self.workspace))
        self.assertEqual(rc, 0)
        self.assertFalse(res_clean["git_drift_detected"])
        self.assertEqual(res_clean["pending_count"], 1)
        self.assertEqual(res_clean["next_batch_tasks"][0]["id"], "t2")
        self.assertTrue(res_clean["graph_exists"])
        self.assertIn("app.py", res_clean["focused_subgraph"]["relevant_files"])

        (self.workspace / "app.py").write_text("VERSION = 2\n", encoding="utf-8")
        subprocess.run(["git", "add", "app.py"], cwd=str(self.workspace), check=True)
        subprocess.run(["git", "commit", "-m", "external edit"], cwd=str(self.workspace), check=True, capture_output=True)

        rc, res_drift = self._run_cli("resume-context", "--workspace", str(self.workspace))
        self.assertEqual(rc, 0)
        self.assertTrue(res_drift["git_drift_detected"])
        self.assertTrue(any("Git HEAD moved" in d for d in res_drift["drift_details"]))

    def test_context_graph_prunes_unrelated_branches(self) -> None:
        for name in ("auth_a.py", "auth_b.py", "billing_a.py", "billing_b.py", "ui_a.py"):
            (self.workspace / name).write_text(f"# {name}\nval = 1\n", encoding="utf-8")

        tasks = [
            {"id": "auth1", "title": "Auth token parser", "files_to_read": ["auth_a.py"], "expected_output_tokens": 1000, "dependencies": []},
            {"id": "auth2", "title": "Auth middleware", "files_to_read": ["auth_b.py"], "expected_output_tokens": 1000, "dependencies": ["auth1"]},
            {"id": "bill1", "title": "Billing invoice model", "files_to_read": ["billing_a.py"], "expected_output_tokens": 1000, "dependencies": []},
            {"id": "bill2", "title": "Billing webhook", "files_to_read": ["billing_b.py"], "expected_output_tokens": 1000, "dependencies": ["bill1"]},
            {"id": "ui1", "title": "Dashboard card", "files_to_read": ["ui_a.py"], "expected_output_tokens": 1000, "dependencies": []},
        ]
        rc, _ = self._run_cli("plan-schedule", "--workspace", str(self.workspace), "--tasks-json", json.dumps(tasks))
        self.assertEqual(rc, 0)

        rc, _ = self._run_cli(
            "save-worklog",
            "--objective",
            "Refactor backend modules cleanly",
            "--completed-json",
            json.dumps([
                {"task_id": "auth1", "summary": "Parsed JWT", "files_touched": ["auth_a.py"], "verification": "ok"},
                {"task_id": "bill1", "summary": "Added invoice schema", "files_touched": ["billing_a.py"], "verification": "ok"},
                {"task_id": "ui1", "summary": "Styled card", "files_touched": ["ui_a.py"], "verification": "ok"},
            ]),
            "--failed-json",
            json.dumps([
                "Do not use PyJWT sync clock in auth2",
                "Stripe v1 webhook signature failed in bill2",
            ]),
            "--resume-pointer",
            "Continue auth2 in auth_b.py",
            "--workspace",
            str(self.workspace),
        )
        self.assertEqual(rc, 0)

        # Query subgraph specifically for auth2 -> must include auth1 & auth dead-end, prune bill1/bill2/ui1
        rc, sub = self._run_cli("query-graph", "--task-id", "auth2", "--depth", "2", "--workspace", str(self.workspace))
        self.assertEqual(rc, 0)
        node_ids = {n["id"] for n in sub["nodes"]}
        self.assertIn("task:auth2", node_ids)
        self.assertIn("task:auth1", node_ids)
        self.assertNotIn("task:bill1", node_ids)
        self.assertNotIn("task:bill2", node_ids)
        self.assertNotIn("task:ui1", node_ids)
        self.assertIn("auth_a.py", sub["relevant_files"])
        self.assertIn("auth_b.py", sub["relevant_files"])
        self.assertNotIn("billing_a.py", sub["relevant_files"])
        self.assertEqual(len(sub["relevant_dead_ends"]), 1)
        self.assertIn("auth2", sub["relevant_dead_ends"][0])
        self.assertLess(sub["subgraph_tokens"], sub["full_graph_tokens"])
        self.assertGreater(sub["tokens_saved_pct"], 25.0)
        self.assertIn("task_auth1 --> task_auth2", sub["mermaid"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
