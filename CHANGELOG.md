# Changelog

## 0.2.1 — 2026-09-14

Repair release based on the 0.2.0 review. Every finding is a behavior fix with
regression coverage; see docs/inheritance.md for the resulting semantics.

- Launch argv: the `codex_mcp` bridge tool is merged into the single `--tools`
  allowlist over the full launch argv. Appending a second `--tools` flag let
  Pi's last-flag-wins parser wipe the reader/writer builtin tools (F01).
- Codex launcher: `-C/--cd` is scanned read-only for scope binding and
  forwarded to Codex VERBATIM; stripping it left Codex in the launcher's
  directory while the scope pointed elsewhere (F02). Covers `-C DIR`,
  `--cd DIR`, `--cd=DIR`, `-CDIR`, last-wins duplicates, and `--` separators.
- Bridge stdio: timeouts reject through the pending entry (the old timer
  callback referenced an out-of-scope `reject` and crashed instead of ending
  the RPC), spawn failures and mid-call exits reject waiters instead of
  crashing Pi, per-connection generation isolation, shared initialize promise,
  reconnects re-handshake, and a failed call is never replayed (F03).
- Bridge HTTP: the deadline covers the whole exchange (headers, body and
  result parsing), Pi's cancellation signal propagates into the transport,
  cancellation is forwarded as `notifications/cancelled` with an explicit
  "outcome unknown" for in-flight write calls, and bodies/SSE buffers are
  bounded (F04).
- Tool metadata: full `inputSchema` is kept in memory (no disk cache) and a
  `describe` action returns it; MCP `isError` and transport failures surface
  as real tool errors via thrown errors, per Pi's extension contract (F05).
- Approval policy: a structured effective model replaces the auto-tools set —
  per-tool override > server default; child `confirm_all` (read child without
  allowlist) wins over any parent-side `auto`; `writes`/unknown modes degrade
  to confirm; unimplementable tool config (e.g. `output_token_limit`) denies
  that tool instead of being ignored. readOnlyHint affects visibility only and
  never bypasses confirmation (F06).
- Environment isolation: guard/Pi children get the scope's own base env plus
  explicitly configured `inheritance.child_env` names — never a copy of the
  daemon's environ; profile env values are re-read from the operator config at
  boot and only env NAMES are persisted; the bridge gives each MCP server only
  the base keys plus the server's declared env (F07).
- Packaging: the compatibility manifest `.codex-plugin/plugin.json` is now
  committed (it was gitignored but unconditionally required by the validator),
  clean-checkout validation uses `git archive` only, and installer/packager
  share stricter exclusion rules (F08).
- Tests: the default suite never launches a real Pi (no model calls); the
  real-Pi check is gated behind `SUBAGENT_PI_LIVE_PI=1`, boots without sending
  a prompt, and verifies the bridge receipt. A jiti-based host fixture loads
  the actual TypeScript bridge against local fake stdio/HTTP MCP servers for
  lifecycle/policy/deadline/cancel evidence, and `tsc --strict` type checking
  is wired via pinned devDependencies (F09).
- Diagnostics: Path objects are coerced to strings at the diagnostic boundary,
  so a missing skills directory can no longer break event serialization (F10).
- Required servers: every declared server keeps a disposition
  (ok/failed/disabled) with named reasons; any required failure at parse, env
  resolution or child initialization aborts spawn/respawn before any task is
  sent; readiness is a structured, non-secret JSON receipt read from a private
  pipe with agent id and generation, not a substring scan of the capped
  stderr.log (F11).
- Recursion guard: extended to the installer-generated wrapper
  (`python <install>/bin/subagent-pi mcp`), `python -m subagent_pi` forms and
  entry-script args, enforced both in the daemon parser and inside the bridge;
  renaming the server evades nothing (F12).
- Source resolution: a configured `codex_home` or scope-bound `CODEX_HOME`
  that does not exist is an error instead of a silent fallback; re-binding a
  scope no longer resets its per-scope inheritance switch; project `.agents`
  skills resolve against the worker's actual cwd.
- Bootstrap writer: the pipe fd is owned and closed by the writer thread (a
  timeout abandons the wait, never closes the fd mid-write).

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
