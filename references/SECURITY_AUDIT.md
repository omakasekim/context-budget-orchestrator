# Built-In Defensive Security & Code Audit Guide

`scripts/budget_engine.py audit` (backed by zero-dependency `scripts/security_audit.py`) integrates enterprise static security auditing, compliance mapping, API contract review, baseline diffing, and multi-format reporting directly into the orchestrator.

## 1. Scoped Boundaries (`--scope-nl` or `init-scope`)
Define authorized boundaries using either flags or natural language:
```bash
python3 scripts/budget_engine.py audit --action init-scope \
  --scope-nl "only src/* api/* no tests/* no fixtures/*" \
  --authorization "Internal authorized repository audit" --workspace "." --json
```

## 2. 30-Rule Static CWE Scan + Custom Rules + Multi-Format Reports (`scan`)
Covers 30 high-signal defensive checks (SQLi, NoSQLi, Command Injection, LDAP, SSTI, XXE, Deserialization, Eval, Hardcoded Secrets, Weak JWT, Disabled TLS, Weak Crypto/PRNG, XSS DOM Sinks, Open Redirect, Path Traversal, SSRF, Prototype Pollution, Mass Assignment, IDOR hints, Wildcard CORS, Unsafe CSP, Insecure Cookies, GraphQL Introspection, Debug Mode, Docker root/`:latest`, Supply-Chain `curl|sh`, Secret Log Leakage, ReDoS, Unrestricted Upload) with automatic SHA-256 fingerprint deduplication and OWASP Top 10 / PCI-DSS v4 / SOC 2 / ISO 27001 mapping:

```bash
python3 scripts/budget_engine.py audit --action scan \
  --path "src" \
  --rules-file "custom_rules.json" \
  --formats "json,sarif,html,junit,csv,md" \
  --save-graph --create-tasks --workspace "." --json
```

## 3. OpenAPI / Swagger Static Security Contract Audit (`openapi`)
Audit API specs offline for cleartext `http://` servers, unauthenticated mutating/admin endpoints, credentials in URL query parameters, and missing requestBody schemas:
```bash
python3 scripts/budget_engine.py audit --action openapi \
  --spec "openapi.json" --formats "sarif,html,md" --workspace "." --json
```

## 4. Baseline Scan Diffing & PR Regression Gate (`diff` & `--retest-new`)
Compare two JSON scan baselines or scan while keeping **only** newly introduced findings:
```bash
python3 scripts/budget_engine.py audit --action diff --baseline baseline.json --current current.json --formats html,json
python3 scripts/budget_engine.py audit --action scan --path "src" --retest-new baseline.json --json
```

## 5. Seven-Question Triage Gate (`triage`) & Secret Redactor (`redact`)
- Validate findings against 7 gates (`in-scope`, `reproducible`, `root-cause pinned`, `real impact`, `severity calibrated`, `remediation clear`, `evidence redacted`).
- Scrub secrets, JWTs, Bearer headers, cookies, private keys, and emails before saving evidence:
  `cat raw.log | python3 scripts/budget_engine.py audit --action redact`
