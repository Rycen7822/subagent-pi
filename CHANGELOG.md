# Changelog

## 0.2.0 — 2026-09-14

Codex inheritance for managed children (normal `pi` untouched):

- Source resolution: explicit trusted `codex_home` → scope-bound `CODEX_HOME`
  → `~/.codex`; per-scope binding persisted as non-secret fields; conflicting
  rebinds require an explicit management action; `subagent-pi codex` parses
  Codex's `-C/--cd`.
- Skills: original-path `--skill` references for project `.agents/skills`,
  profile skills, and `<codex_home>/skills`; realpath dedup, `[[skills.config]]`
  disabling, name-conflict refusal, management-skill recursion guard.
- MCP: read-only `tomllib` conversion of `[mcp_servers.*]` (stdio + streamable
  HTTP, env/header/bearer references, allow/deny lists, timeouts, approval
  modes, required semantics); unsupported auth/helper/remote forms disable the
  server with named diagnostics; in-memory only.
- Private bootstrap: anonymous pipe daemon → worker guard → Pi (`pass_fds` on
  every hop, async parent write for payloads beyond the pipe buffer), consumed
  once by `extensions/codex-mcp-bridge.ts` (zero npm dependencies, explicit
  `--extension` only, single `codex_mcp` list/call tool), verified by a
  non-secret stderr ready receipt before any task is sent.
- Read children: MCP exposure is the intersection of server policy and child
  limits; confirmations flow through the existing `pi_answer_agent` channel.
- Persistence: scopes schema v1→v2 transactional migration (codex_home,
  source mode, inheritance flag). Secrets never touch the ledger, launch.json,
  logs, events or diagnostics.
- `subagent-pi doctor --inheritance`, `docs/inheritance.md`, packaging
  exclusions for scratch/runtime files, version references derived from the
  package instead of hardcoded.

## 0.1.0 — 2026-09-14

Initial source distribution: Codex portable and compatibility plugin manifests,
12 MCP tools, shared CLI/daemon, isolated Pi RPC workers, persistent scopes/runs,
idempotent mutations, steering receipts, queued follow-ups, bounded inspection,
explicit result acknowledgement, interruption, process-group cleanup and recovery.

Includes local-marketplace installer, offline subprocess/MCP tests, and an opt-in
real Pi smoke script. No hooks, automatic model wakeups or native Codex /agents UI.
