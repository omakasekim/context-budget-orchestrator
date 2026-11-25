# Ground-Truth Verification & Multi-Agent Referee Guide

Never take an AI agent's word that it "shipped" a feature, wrote real tests, or is "making progress." `scripts/budget_engine.py verify` (backed by zero-dependency `scripts/truth_verifier.py`) adjudicates claims directly from real-world witnesses — git ancestry, commit diffs, filesystem artifacts, and progress fingerprints — and returns hard OS exit codes (`0` = verified/granted/moving, `1` = false claim/refused/stalled) ready for CI gates and agent hooks.

## 1. Verify Claimed Work Actually Shipped (`verify --action shipped`)
Checks reachable `git log` ancestry for a non-empty commit backing `--claim` and verifies expected files exist and are non-empty on disk:
```bash
python3 scripts/budget_engine.py verify --action shipped --claim "AUTH-1" --files "src/auth.py" --workspace "." --json
# Exit 0 -> SHIPPED   | Exit 1 -> NOT_SHIPPED (catches hallucinated/missing commits)
```

## 2. Audit Commits for Hollow Tests & Unbacked Claims (`verify --action commit-audit`)
Audits commits (`--range BASE..HEAD`) against their own diffs to flag:
- Trivial `assert True` / `expect(true).toBe(true)` placeholders
- Assertionless test functions or deleted assertions without replacements
- Newly added `@pytest.mark.skip`, `test.skip()`, or `|| true` failure swallowing
- Commits claiming `"add tests"` or `"fix"` that only edited docs or never touched test files
```bash
python3 scripts/budget_engine.py verify --action commit-audit --range "HEAD~5..HEAD" --strict --workspace "." --json
```

## 3. Liveness & Spin-Loop Detection (`verify --action liveness`)
Hashes `(git HEAD + git status + git diff + schedule completion)` into `.agent_state/liveness_journal.json`. Exits `1` (`STALLED`) when an agent makes zero real-world delta for `--stall-threshold` consecutive checks or oscillates in an `A → B → A` flip-flop loop:
```bash
python3 scripts/budget_engine.py verify --action liveness --stall-threshold 3 --workspace "." --json
```

## 4. Deterministic Multi-Agent File Referee (`verify --action arbitrate`)
Answers whether `--agent-id` is allowed to edit `--files` right now, returning `GRANTED` (exit `0`) or `REFUSED` (exit `1`) with closed-vocabulary refusal codes (`LEASE_HELD_BY_OTHER`, `DEPENDENCY_NOT_SHIPPED`):
```bash
python3 scripts/budget_engine.py verify --action arbitrate --agent-id "worker-2" --files "src/auth.py" --deps "AUTH-1" --require-shipped-deps --workspace "." --json
```

## 5. Human Escalation Queue (`verify --action escalate`)
Track unresolved blockers, caught false claims, or stalled loops needing human judgment (also surfaced in `status`):
```bash
python3 scripts/budget_engine.py verify --action escalate --sub-action add --title "AUTH-2 failed shipped check" --detail "No commit found in ancestry" --workspace "." --json
python3 scripts/budget_engine.py verify --action escalate --sub-action list --workspace "." --json
```
