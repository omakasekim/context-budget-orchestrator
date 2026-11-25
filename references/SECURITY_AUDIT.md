# Built-In Security & Bug Audit Methodology

`scripts/budget_engine.py audit` integrates structured application-security and bug-hunting discipline directly into the orchestrator with zero external dependencies.

## 1. Authorized Scope First (`audit --action init-scope`)
Before scanning or testing any codebase or service, record explicit boundaries in `.agent_state/audit_scope.json`:
```bash
python3 scripts/budget_engine.py audit --action init-scope \
  --in-scope "src/,api/" \
  --out-of-scope "tests/fixtures/,vendor/" \
  --authorization "Internal authorized repository audit" \
  --workspace "." --json
```
Never test or probe targets outside authorized scope.

## 2. Static Bug & Vulnerability Scan (`audit --action scan`)
Run a zero-dependency multi-class scan covering 14 high-signal CWE classes (SQLi, Command Injection, Unsafe Deserialization, Hardcoded Secrets, SSRF, Path Traversal, XSS DOM Sinks, Weak JWT/Auth, Weak Crypto/TLS Disabled, Prototype Pollution, Permissive CORS, Debug Exposure, GraphQL Introspection, and Unsafe Eval):
```bash
python3 scripts/budget_engine.py audit --action scan --path "src" --save-graph --create-tasks --workspace "." --json
```
- `--save-graph` persists verified findings as `finding` nodes in `.agent_state/context_graph.json` linked to affected files (`AFFECTS_FILE`).
- `--create-tasks` converts findings directly into prioritized remediation tasks in `.agent_state/schedule.json` so you can delegate or execute fixes inside token-safe batches.

## 3. Seven-Question Triage Gate (`audit --action triage`)
Before reporting any finding as actionable (eliminates false positives and low-value noise), evaluate it against the 7-Question Gate:
1. **In Scope**: Confirmed inside `.agent_state/audit_scope.json`?
2. **Reproducible**: Minimal deterministic reproduction steps or unit test verified?
3. **Root Cause Pinned**: Exact `file:line` and code path identified?
4. **Real Impact**: Concrete confidentiality, integrity, availability, or correctness impact (not theoretical)?
5. **Severity Calibrated**: `CRITICAL` / `HIGH` / `MEDIUM` / `LOW` matched to exploitability & blast radius?
6. **Remediation Clear**: Minimal, non-breaking fix verified?
7. **Evidence Scrubbed**: Secrets, tokens, cookies, and PII redacted before storing or sharing?

```bash
python3 scripts/budget_engine.py audit --action triage \
  --title "Unparameterized SQL query in user lookup" \
  --severity HIGH --cwe CWE-89 --location "src/db.py:44" \
  --poc "python3 -m pytest tests/test_sqli.py" \
  --impact "Arbitrary read/write on users table" \
  --remediation "Use parameterized query bindings" \
  --in-scope-confirmed --reproducible --evidence-redacted --json
```

## 4. Evidence Hygiene & Secret Redaction (`audit --action redact`)
Scrub Bearer tokens, JWTs, AWS keys, GitHub PATs, private key blocks, cookies, and email addresses from logs or PoC transcripts before saving them to disk or pasting into reports:
```bash
cat raw_poc.log | python3 scripts/budget_engine.py audit --action redact
```
