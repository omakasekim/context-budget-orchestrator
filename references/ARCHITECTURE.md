# Context Budget Orchestrator — Architecture Specification

## 1. Why Graph-First Orchestration Outperforms Built-In Compaction

Built-in CLI compaction mechanisms (summarizing linear chat history when nearing 90%+ utilization) suffer from four structural flaws:
1. **Late Triggering**: Quality degrades past ~68% context fill due to attention dilution ("lost in the middle") well before emergency compaction fires.
2. **Lossy Prose Summaries**: Free-form summaries drop exact file paths, architectural invariants, and failed approaches, causing agents to repeat dead ends.
3. **No Static Dependency or Parallel Awareness**: Agents read unrelated files and cannot safely dispatch file-disjoint parallel subagents without stepping on edits.
4. **Noisy Tool Outputs**: Raw stack traces and multi-thousand-line test runs blow out the window in a single turn.

`context-budget-orchestrator` solves all four with a zero-dependency deterministic control plane stored inside `<workspace>/.agent_state/`:

```text
.agent_state/
├── context_graph.json      # Canonical typed knowledge & execution graph (schema v2)
├── schedule.json           # Batch bin-packing, parallel waves, decisions, completion ledger
├── leases.json             # TTL-expiring multi-agent task & file-conflict locks
├── WORKLOG.md              # Human-readable projection with embedded Mermaid DAG
└── archive/
    ├── context_graph_<ts>.json
    └── WORKLOG_<ts>.md
```

## 2. Typed Context Graph Schema (`context_graph.json`)

| Node Type | Purpose | Retention Priority under `--token-budget` |
| --- | --- | --- |
| `objective` | Immutable overarching user goal | 0 (Always retained) |
| `constraint` | Non-negotiable guardrails | 1 |
| `decision` | Architectural Decision Records (ADRs / invariants) | 2 |
| `checkpoint` | Latest git anchor (`branch`, `HEAD`, `status`) & `resume_pointer` | 3 |
| `task` | Scheduled/completed subtask with batch, parallel wave, summary, verification | 4 |
| `dead_end` | Failed approaches linked to specific tasks/files so they are never repeated | 5 |
| `file` | Read/touched workspace files with `last_touched_by` attribution | 6 |

### Edge Relations
- `DEPENDS_ON`: `task -> upstream_task` (traversed backwards during `query-graph` and `resume-context`)
- `READS` / `TOUCHES`: `task -> file`
- `GOVERNS` / `GOVERNS_TASK` / `GOVERNS_FILE`: `decision -> objective|task|file`
- `WARNS_TASK` / `WARNS_FILE` / `WARNS_GLOBAL`: `dead_end -> task|file|objective`
- `CONSTRAINS`: `constraint -> objective`
- `IMPLEMENTS`: `task -> objective`

## 3. Token Accounting & Hard Thresholds

- **Exact Provider Telemetry First**: When parsing Claude Code, Gemini CLI, or OpenAI Codex JSONL transcripts, `inspect-session` automatically extracts exact provider-reported `usage` / `usageMetadata` fields (`input_tokens + cache_read_input_tokens + cache_creation_input_tokens + output_tokens`, `totalTokenCount`, or `total_tokens`) when present.
- **Calibrated Offline Estimator**: Falls back to `tiktoken` (`o200k_base` / `cl100k_base`) if installed, or `ceil(max(len(text) / 3.6, symbol_and_whitespace_tokens))` with zero external packages.
- **Budget Cutoffs**:
  - `safe_ceiling_tokens = int(context_limit * 0.68)`
  - `GREEN`: `< 55%` utilization and `>= 25,000` remaining tokens
  - `YELLOW`: `55%–68%` utilization and `>= 25,000` remaining tokens
  - `CRITICAL_HANDOFF`: `> 68%` utilization or `< 25,000` remaining tokens
  - Overhead multiplier: `1.35x` on `(file_tokens + expected_output_tokens)` to reserve room for tool calls, diffs, and reasoning.

## 4. Parallel Wave & Multi-Agent Lease Algorithm

1. Tasks are topologically sorted with cycle detection (Kahn's algorithm).
2. Oversized tasks exceeding `safe_ceiling_tokens` are automatically split into sequential `<id>__part1`, `<id>__part2`, ... chunks and downstream dependents are rewired to the final chunk.
3. Tasks are packed into session batches (`Batch 1` capped at `max(10000, safe_ceiling - used_tokens)`, `Batch 2+` at `safe_ceiling`).
4. Independently, `compute_parallel_waves()` assigns `parallel_wave` numbers so tasks in the same wave have **zero dependency edges between them AND zero overlapping files**, enabling safe parallel subagent dispatch backed by `lease --action claim` file-lock enforcement.
