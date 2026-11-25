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

        # GREEN
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

        # YELLOW (55% - 68% and >= 25,000 remaining)
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

        # CRITICAL_HANDOFF (> 68%)
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

        # CRITICAL_HANDOFF (< 25,000 remaining even if < 68% on a small limit)
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

        # limit=100,000 -> safe_ceiling=68,000; used=40,000 -> batch1_budget=28,000
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

        # Cycle detection
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

        # limit=50,000 -> safe_ceiling=34,000, forcing `mega` to split
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
        # Downstream task must be rewired to the last split chunk
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
        # Initialize a temporary git repo
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

        # First save sets immutable objective & constraints
        rc, saved1 = self._run_cli(
            "save-worklog",
            "--objective",
            "Ship zero-dependency auth refactor",
            "--constraints-json",
            json.dumps(["No third-party packages", "Keep CLI backwards compatible"]),
            "--completed-json",
            json.dumps([{"task_id": "t1", "summary": "Initialized config", "files_touched": ["app.py"], "verification": "unit tests ok"}]),
            "--failed-json",
            json.dumps(["Avoided monkeypatching os.environ globally"]),
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

        # Second save without --objective/--constraints-json must preserve them
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

        # Resume before drift -> no drift
        rc, res_clean = self._run_cli("resume-context", "--workspace", str(self.workspace))
        self.assertEqual(rc, 0)
        self.assertFalse(res_clean["git_drift_detected"])
        self.assertEqual(res_clean["pending_count"], 1)
        self.assertEqual(res_clean["next_batch_tasks"][0]["id"], "t2")

        # Introduce an out-of-band commit to trigger git drift detection
        (self.workspace / "app.py").write_text("VERSION = 2\n", encoding="utf-8")
        subprocess.run(["git", "add", "app.py"], cwd=str(self.workspace), check=True)
        subprocess.run(["git", "commit", "-m", "external edit"], cwd=str(self.workspace), check=True, capture_output=True)

        rc, res_drift = self._run_cli("resume-context", "--workspace", str(self.workspace))
        self.assertEqual(rc, 0)
        self.assertTrue(res_drift["git_drift_detected"])
        self.assertTrue(any("Git HEAD moved" in d for d in res_drift["drift_details"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
