#!/usr/bin/env python3
"""Exhaustive unittest suite for context-budget-orchestrator v3.0 (all capabilities + multi-harness)."""

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


class FlagshipV3OrchestratorTests(unittest.TestCase):
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

    def test_01_multi_harness_models_and_token_estimation(self) -> None:
        self.assertEqual(budget_engine.resolve_context_limit("deepseek/deepseek-v4"), 1_000_000)
        self.assertEqual(budget_engine.resolve_context_limit("xai/grok-4"), 256_000)
        self.assertEqual(budget_engine.resolve_context_limit("anthropic/claude-sonnet-4-6"), 200_000)
        self.assertEqual(budget_engine.resolve_context_limit("google/gemini-2.5-pro"), 1_048_576)
        self.assertEqual(budget_engine.resolve_context_limit("openai/gpt-5-codex"), 400_000)

        (self.workspace / "sample.py").write_text("def add(a: int, b: int) -> int:\n    return a + b\n" * 40, encoding="utf-8")
        (self.workspace / "blob.bin").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR")
        tokens, count, skipped = budget_engine.measure_path_tokens(self.workspace)
        self.assertEqual(count, 1)
        self.assertGreater(tokens, 100)
        self.assertTrue(any("blob.bin" in s for s in skipped))

    def test_02_inspect_session_provider_usage_and_rot_detection(self) -> None:
        transcript = self.workspace / "deepseek_session.jsonl"
        records = [
            {"role": "user", "content": "Migrate payments"},
            {"role": "assistant", "content": '{"path": "src/pay.py"}', "usage": {"prompt_tokens": 130000, "completion_tokens": 12000}},
            {"role": "tool", "type": "tool_result", "content": "Traceback error: failed\n" * 200},
            {"role": "tool", "type": "tool_result", "content": "Traceback error: failed\n" * 200},
            {"role": "tool", "type": "tool_result", "content": "Traceback error: failed\n" * 200},
            {"role": "assistant", "content": '{"path": "src/pay.py"} {"path": "src/pay.py"} {"path": "src/pay.py"}'},
        ]
        transcript.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

        rc, data = self._run_cli(
            "inspect-session",
            "--model",
            "deepseek-v3",
            "--limit",
            "200000",
            env_override={"DEEPSEEK_SESSION": str(transcript)},
        )
        self.assertEqual(rc, 0)
        self.assertEqual(data["detected_source"], "env:DEEPSEEK_SESSION")
        self.assertEqual(data["telemetry_mode"], "provider_usage_exact")
        self.assertEqual(data["used_tokens"], 142000)
        self.assertEqual(data["status"], "CRITICAL_HANDOFF")
        self.assertGreaterEqual(len(data["rot_alerts"]), 2)

    def test_03_auto_plan_imports_and_parallel_waves(self) -> None:
        src = self.workspace / "src"
        src.mkdir()
        (src / "utils.py").write_text("def clean(x): return x.strip()\n", encoding="utf-8")
        (src / "auth.py").write_text("from src import utils\ndef verify(t): return utils.clean(t)\n", encoding="utf-8")
        (src / "billing.py").write_text("def charge(n): return n * 100\n", encoding="utf-8")

        rc, plan = self._run_cli("auto-plan", "--path", "src", "--action", "Refactor", "--workspace", str(self.workspace))
        self.assertEqual(rc, 0)
        self.assertEqual(plan["discovered_files"], 3)
        self.assertGreaterEqual(plan["import_edges_detected"], 1)
        self.assertTrue(plan["parallel_waves"][0]["parallel_safe"])

    def test_04_memory_research_delegation_exec_task_and_skill_router(self) -> None:
        (self.workspace / "auth.py").write_text("def verify(x):\n    return x == 'ok'\n", encoding="utf-8")
        (self.workspace / "billing.py").write_text("def bill(x):\n    return x * 2\n", encoding="utf-8")

        # Create a mock installed skill directory to verify zero-bloat metadata routing
        mock_skills = self.workspace / ".agents" / "skills" / "jwt-auditor"
        mock_skills.mkdir(parents=True)
        (mock_skills / "SKILL.md").write_text(
            "---\nname: jwt-auditor\ndescription: Audits JWT RS256 signatures and auth middleware security.\n---\n# Body\n",
            encoding="utf-8",
        )

        tasks = [
            {"id": "t_research", "title": "Research JWT RS256 best practices", "role": "researcher", "files_to_read": ["auth.py"], "expected_output_tokens": 1000, "dependencies": []},
            {"id": "t_impl", "title": "Implement JWT auth verifier", "role": "implementer", "files_to_read": ["auth.py"], "expected_output_tokens": 1500, "dependencies": ["t_research"]},
            {"id": "t_bill", "title": "Update billing math", "role": "implementer", "files_to_read": ["billing.py"], "expected_output_tokens": 1000, "dependencies": []},
        ]
        rc, _ = self._run_cli("plan-schedule", "--objective", "Harden auth & billing", "--workspace", str(self.workspace), "--tasks-json", json.dumps(tasks))
        self.assertEqual(rc, 0)

        # Store organized research memory linked to t_impl
        rc, mem_add = self._run_cli(
            "memory",
            "--action",
            "add",
            "--kind",
            "research",
            "--title",
            "RFC 8725 JWT BCP",
            "--content",
            "Pin alg header to RS256 and validate iss/aud/exp strictly.",
            "--source",
            "https://datatracker.ietf.org/doc/html/rfc8725",
            "--tags",
            "jwt,security,auth",
            "--tasks",
            "t_impl",
            "--files",
            "auth.py",
            "--workspace",
            str(self.workspace),
        )
        self.assertEqual(rc, 0)
        self.assertEqual(mem_add["memory"]["kind"], "research")

        # Query memory
        rc, mem_q = self._run_cli("memory", "--action", "query", "--query", "RS256 jwt", "--workspace", str(self.workspace))
        self.assertEqual(rc, 0)
        self.assertEqual(mem_q["count"], 1)

        # Discover matching skills (metadata-only)
        rc, sk = self._run_cli("discover-skills", "--query", "jwt auth", "--workspace", str(self.workspace))
        self.assertEqual(rc, 0)
        self.assertTrue(any(m["name"] == "jwt-auditor" for m in sk["matches"]))

        # Delegate t_impl to specialized implementer subagent targeting OpenCode harness
        rc, deleg = self._run_cli(
            "delegate",
            "--task-id",
            "t_impl",
            "--role",
            "implementer",
            "--harness",
            "opencode",
            "--workspace",
            str(self.workspace),
        )
        self.assertEqual(rc, 0)
        self.assertTrue(Path(deleg["brief_path"]).is_file())
        self.assertIn("opencode run", deleg["dispatch_command"])
        self.assertIn("RFC 8725 JWT BCP", deleg["brief_markdown"])
        self.assertTrue(any(s["name"] == "jwt-auditor" for s in deleg["recommended_skills"]))
        self.assertIsNotNone(deleg["lease"])

        # Execute code verification via exec-task and confirm receipt persisted
        rc, ex = self._run_cli(
            "exec-task",
            "--task-id",
            "t_impl",
            "--cmd",
            f"{sys.executable} -c \"import auth; assert auth.verify('ok')\"",
            "--workspace",
            str(self.workspace),
        )
        self.assertEqual(rc, 0)
        self.assertTrue(ex["passed"])

    def test_05_multi_agent_leases_conflict_guard_and_splitting(self) -> None:
        (self.workspace / "shared.py").write_text("X = 1\n", encoding="utf-8")
        tasks = [
            {"id": "t1", "title": "Edit A", "files_to_read": ["shared.py"], "expected_output_tokens": 1000, "dependencies": []},
            {"id": "t2", "title": "Edit B", "files_to_read": ["shared.py"], "expected_output_tokens": 1000, "dependencies": []},
        ]
        self._run_cli("plan-schedule", "--workspace", str(self.workspace), "--tasks-json", json.dumps(tasks))
        rc, _ = self._run_cli("lease", "--action", "claim", "--task-id", "t1", "--agent-id", "grok-1", "--workspace", str(self.workspace))
        self.assertEqual(rc, 0)
        rc_conf, err_conf = self._run_cli("lease", "--action", "claim", "--task-id", "t2", "--agent-id", "codex-2", "--workspace", str(self.workspace))
        self.assertEqual(rc_conf, 1)
        self.assertIn("File conflict", err_conf["error"])

    def test_06_compress_verify_worklog_resume_and_hook(self) -> None:
        noisy = "\n".join(["start"] + [f"ok line {i}" for i in range(160)] + ["ERROR: auth.py:12 boom"] + ["end"] * 20)
        rc, comp = self._run_cli("compress-output", "--max-lines", "40", stdin_text=noisy)
        self.assertEqual(rc, 0)
        self.assertGreater(comp["tokens_saved_pct"], 55.0)
        self.assertIn("ERROR: auth.py:12 boom", comp["compressed_text"])

        good_py = self.workspace / "ok.py"
        good_py.write_text("def hi():\n    return 'hi'\n", encoding="utf-8")
        rc, ver = self._run_cli("verify-handoff", "--files", "ok.py", "--workspace", str(self.workspace))
        self.assertEqual(rc, 0)
        self.assertTrue(ver["handoff_ready"])

        # Save worklog increments handoff_epoch and writes WORKLOG.md + context_graph.json
        rc, saved = self._run_cli(
            "save-worklog",
            "--objective",
            "Long-horizon 4-hour migration",
            "--resume-pointer",
            "Resume at ok.py:1",
            "--used-tokens",
            "62000",
            "--limit",
            "100000",
            "--workspace",
            str(self.workspace),
        )
        self.assertEqual(rc, 0)
        self.assertEqual(saved["handoff_epoch"], 1)
        self.assertTrue(Path(saved["graph_path"]).is_file())

        rc, res = self._run_cli("resume-context", "--workspace", str(self.workspace))
        self.assertEqual(rc, 0)
        self.assertEqual(res["handoff_epoch"], 1)

        hook_proc = subprocess.run(
            [sys.executable, str(HOOK_PATH), "--model", "grok-4", "--limit", "100000"],
            input=json.dumps({"extra_tokens": 72000}),
            capture_output=True,
            text=True,
            check=True,
        )
        hook_resp = json.loads(hook_proc.stdout)
        self.assertEqual(hook_resp["status"], "CRITICAL_HANDOFF")

    def test_07_security_bug_audit_triage_redact_and_scope(self) -> None:
        # 1. Scope guard
        rc, sc = self._run_cli(
            "audit",
            "--action",
            "init-scope",
            "--in-scope",
            "src/",
            "--out-of-scope",
            "fixtures/",
            "--authorization",
            "Authorized repo audit",
            "--workspace",
            str(self.workspace),
        )
        self.assertEqual(rc, 0)
        self.assertTrue(Path(sc["scope_path"]).is_file())

        # 2. Vulnerable source file vs excluded fixture file
        src = self.workspace / "src"
        src.mkdir()
        (src / "vuln.py").write_text(
            "import subprocess, pickle\n"
            "def run(cmd, uid, blob):\n"
            "    cursor.execute(f'SELECT * FROM users WHERE id={uid}')\n"
            "    subprocess.run(cmd, shell=True)\n"
            "    return pickle.loads(blob)\n",
            encoding="utf-8",
        )
        fix = self.workspace / "fixtures"
        fix.mkdir()
        (fix / "ignored.py").write_text("eval(user_data)\n", encoding="utf-8")

        rc, scan = self._run_cli(
            "audit",
            "--action",
            "scan",
            "--path",
            ".",
            "--save-graph",
            "--create-tasks",
            "--workspace",
            str(self.workspace),
        )
        self.assertEqual(rc, 0)
        self.assertGreaterEqual(scan["total_findings"], 3)
        self.assertEqual(scan["by_severity"]["CRITICAL"], 3)
        # Excluded fixtures/ must not be scanned
        self.assertFalse(any("fixtures/" in f["file"] for f in scan["findings"]))
        self.assertEqual(scan["remediation_tasks_created"], 1)
        self.assertTrue(Path(scan["graph_path"]).is_file())

        # 3. 7-Question Triage Gate (incomplete vs complete)
        rc, need = self._run_cli("audit", "--action", "triage", "--title", "SQLi", "--severity", "CRITICAL", "--workspace", str(self.workspace))
        self.assertEqual(rc, 0)
        self.assertEqual(need["verdict"], "NEEDS_EVIDENCE")

        rc, valid = self._run_cli(
            "audit",
            "--action",
            "triage",
            "--title",
            "SQL Injection in user lookup",
            "--severity",
            "CRITICAL",
            "--cwe",
            "CWE-89",
            "--location",
            "src/vuln.py:3",
            "--poc",
            "pytest tests/test_sqli.py",
            "--impact",
            "Full database read/write",
            "--remediation",
            "Use parameterized queries",
            "--in-scope-confirmed",
            "--reproducible",
            "--evidence-redacted",
            "--workspace",
            str(self.workspace),
        )
        self.assertEqual(rc, 0)
        self.assertEqual(valid["verdict"], "VALID_FINDING")
        self.assertEqual(valid["passed_gates"], 7)

        # 4. Evidence redactor
        raw_secret = "Authorization: Bearer supersecret123\nEmail: alice@corp.io\nKey: AKIA1234567890ABCDEF\n"
        rc, red = self._run_cli("audit", "--action", "redact", stdin_text=raw_secret)
        self.assertEqual(rc, 0)
        self.assertGreaterEqual(red["redactions_applied"], 3)
        self.assertNotIn("supersecret123", red["redacted_text"])
        self.assertNotIn("AKIA1234567890ABCDEF", red["redacted_text"])
        self.assertNotIn("alice@corp.io", red["redacted_text"])

    def test_08_openapi_compliance_nl_scope_custom_rules_diff_and_reports(self) -> None:
        # 1. Custom rule + NL scope + deduplication + multi-format exports (JSON/SARIF/HTML/JUnit/CSV/MD)
        src = self.workspace / "api"
        src.mkdir()
        (src / "handler.py").write_text(
            "import hashlib\n"
            "def f1():\n"
            "    h = hashlib.md5(b'a')\n"
            "    h = hashlib.md5(b'a')\n"  # identical snippet triggers fingerprint dedupe
            "    return h\n"
            "def f3(): return LEGACY_UNSAFE_CALL()\n",
            encoding="utf-8",
        )
        skip_dir = self.workspace / "ignored_dir"
        skip_dir.mkdir()
        (skip_dir / "skip.py").write_text("eval(x)\n", encoding="utf-8")

        custom_rules = self.workspace / "custom_rules.json"
        custom_rules.write_text(
            json.dumps([{"id": "CUSTOM-LEGACY", "cwe": "CWE-477", "severity": "HIGH", "pattern": "LEGACY_UNSAFE_CALL", "title": "Legacy unsafe helper"}]),
            encoding="utf-8",
        )

        rc, scan1 = self._run_cli(
            "audit",
            "--action",
            "scan",
            "--path",
            ".",
            "--scope-nl",
            "only api/* no ignored_dir/*",
            "--rules-file",
            str(custom_rules),
            "--formats",
            "json,sarif,html,junit,csv,md",
            "--workspace",
            str(self.workspace),
        )
        self.assertEqual(rc, 0)
        self.assertEqual(scan1["rules_evaluated"], 31)
        self.assertGreaterEqual(scan1["duplicates_collapsed"], 1)
        self.assertTrue(any(f["rule_id"] == "CUSTOM-LEGACY" for f in scan1["findings"]))
        self.assertIn("owasp", scan1["compliance_summary"])
        self.assertIn("pci_dss", scan1["compliance_summary"])
        self.assertIn("soc2", scan1["compliance_summary"])
        self.assertIn("iso27001", scan1["compliance_summary"])

        # Verify all 6 exported report formats exist and parse cleanly
        reports = scan1["exported_reports"]
        for fmt in ("json", "sarif", "html", "junit", "csv", "md"):
            self.assertIn(fmt, reports)
            self.assertTrue(Path(reports[fmt]).is_file())
        sarif_doc = json.loads(Path(reports["sarif"]).read_text(encoding="utf-8"))
        self.assertEqual(sarif_doc["version"], "2.1.0")
        self.assertIn("<testsuite", Path(reports["junit"]).read_text(encoding="utf-8"))
        self.assertIn("Compliance Audit Report", Path(reports["html"]).read_text(encoding="utf-8"))

        # 2. Baseline diffing & --retest-new regression filter
        baseline_path = Path(reports["json"])
        (src / "handler.py").write_text(
            (src / "handler.py").read_text(encoding="utf-8") + "DEBUG = True\n",
            encoding="utf-8",
        )
        rc, retest = self._run_cli(
            "audit",
            "--action",
            "scan",
            "--path",
            ".",
            "--scope-nl",
            "only api/* no ignored_dir/*",
            "--rules-file",
            str(custom_rules),
            "--retest-new",
            str(baseline_path),
            "--workspace",
            str(self.workspace),
        )
        self.assertEqual(rc, 0)
        self.assertEqual(retest["total_findings"], 1)
        self.assertEqual(retest["findings"][0]["rule_id"], "DEBUG-ENABLED")

        curr_path = self.workspace / "current_scan.json"
        curr_path.write_text(json.dumps(retest), encoding="utf-8")
        rc, diff_out = self._run_cli(
            "audit",
            "--action",
            "diff",
            "--baseline",
            str(baseline_path),
            "--current",
            str(curr_path),
            "--workspace",
            str(self.workspace),
        )
        self.assertEqual(rc, 0)
        self.assertTrue(diff_out["regression_detected"])
        self.assertEqual(diff_out["new_count"], 1)

        # 3. OpenAPI static contract audit
        spec_path = self.workspace / "openapi.json"
        spec_path.write_text(
            json.dumps(
                {
                    "openapi": "3.0.3",
                    "servers": [{"url": "http://api.example.com"}],
                    "paths": {
                        "/admin/users": {
                            "post": {
                                "parameters": [{"name": "api_key", "in": "query"}],
                                "requestBody": {"content": {"application/json": {}}},
                            }
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        rc, oapi = self._run_cli(
            "audit",
            "--action",
            "openapi",
            "--spec",
            str(spec_path),
            "--workspace",
            str(self.workspace),
        )
        self.assertEqual(rc, 0)
        rule_ids = {f["rule_id"] for f in oapi["findings"]}
        self.assertIn("OPENAPI-CLEARTEXT-SERVER", rule_ids)
        self.assertIn("OPENAPI-UNAUTH-ENDPOINT", rule_ids)
        self.assertIn("OPENAPI-SECRET-IN-QUERY", rule_ids)
        self.assertIn("OPENAPI-MISSING-SCHEMA", rule_ids)


if __name__ == "__main__":
    unittest.main(verbosity=2)
