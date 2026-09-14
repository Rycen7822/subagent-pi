# Changelog

## 0.2.5 — 2026-09-15

Three P1 fixes, each red-verified against 04a46db.

- P1-A (x-mcp-header): the annotation is a SCHEMA declaration on plain-typed
  properties, not an argument. Discovery parses inputSchema.properties into an
  in-memory extraction plan, validating per the 2026-07-28 rules: non-empty
  HTTP token, case-insensitively unique, no control characters, plain
  string/integer/boolean types only, no array/items/oneOf/anyOf/allOf/not/
  conditional/$ref dynamic paths. Modern HTTP tools/call mirrors declared
  arguments as Mcp-Param-* headers with the body unchanged; an absent
  argument produces no header; unsafe integers and control-character values
  are refused before sending. Tools with invalid annotations are excluded
  from the catalog and rejected by describe/call without failing the server;
  stdio and legacy connections ignore the annotation. The previous
  arguments-based check was removed.
- P1-B (proxy serialization): codex_mcp is registered with
  executionMode "sequential" AND serializes in memory via a promise chain —
  concurrent sibling calls never overlap server-side; a cancelled first call
  never poisons the chain. supports_parallel_tool_calls diagnostics now state
  the enforced behavior.
- P1-C (stdio generations): a StdioConnection maps to exactly one handshake
  generation. When its process dies (exit or EPIPE), the connection is dead:
  the in-flight/pending operation fails, tools/call is never replayed, and the
  next explicit operation builds a NEW process whose first RPC is initialize.
  No respawn ever happens inside a dead connection.
- startup_timeout: sec wins over ms when both are present (current Codex
  semantics), correcting 0.2.4's "ms wins".

New regressions (all localhost fakes, zero model calls): schema-annotation
header mirroring against a strict modern server, concurrent proxy calls with
server-side start/end ordering, confirm-pending process death (exit and EPIPE)
with per-process first-RPC checks. Existing lifecycle tests updated for the
serialized proxy.

## 0.2.4 — 2026-09-15

Two MCP/Codex compatibility P1 fixes, each verified red against 6741777.

- P1-A (protocol eras): the HTTP transport now has an explicit protocol
  compatibility layer — `legacy_2025_06_18`, `modern_2026_07_28`, `auto`
  (configured once via `[inheritance] mcp_protocol_mode`, never filled in by
  the model; stdio stays legacy because managed children never forward the
  upstream env opt-in). Legacy now NEGOTIATES the initialize version and sends
  the negotiated `MCP-Protocol-Version` on every later request; a 404 on a
  session-scoped request marks the connection stale, returns "outcome is
  unknown" for a sent call, never replays it, and the next explicit operation
  re-initializes a fresh session. `auto` probes with `server/discover` and
  falls back to the legacy handshake ONLY on proof of legacy-only (HTTP 404/405
  or JSON-RPC -32601 on the side-effect-free discover). Modern mode is
  stateless: no handshake, every request self-describes via `_meta`
  (protocolVersion/clientInfo/clientCapabilities) plus `MCP-Protocol-Version`,
  `Mcp-Method` and (for tools/call) `Mcp-Name` headers. `x-mcp-header` argument
  mirroring is refused (model-controlled values must not become HTTP headers).
  Legacy sessions are closed with a best-effort DELETE (documented boundary).
- P1-B (config compatibility): the two hardcoded stdio/http key whitelists are
  replaced by one declarative classification table for the current Codex
  `RawMcpServerConfig` surface (mapped / accepted_no_effect /
  explicitly_unsupported / unknown_fail_closed), reported by doctor as
  `baseline`. `startup_timeout_ms` maps with Codex precedence over
  `startup_timeout_sec` (no truncation); `supports_parallel_tool_calls` and the
  legacy `name` label are accepted without effect; `environment_id` is local-
  only; `omit_tools_from`, `scopes`, `oauth`, `oauth_resource` fail/exclude
  with precise diagnostics instead of disabling servers for unknown reasons;
  tool-level `output_token_limit` is enforced at the proxy serialization
  boundary as a tighten-only 4 bytes/token budget. Genuinely unknown fields
  still fail closed.

New conformance fixtures (localhost fakes, zero model calls): a strict modern
server that rejects requests missing the required headers/_meta, a legacy
server with session expiry, and a legacy-only discovery endpoint; plus the
Codex config field matrix and required/optional unsupported-field matrix.

## 0.2.3 — 2026-09-14

- P1 (receipt fd ownership): the receipt read end now has exactly one closer.
  `_read_receipt` takes ownership of the fd on entry and closes it exactly
  once on every path (success, malformed receipt, required-server failure,
  timeout, cancellation, transport-creation failure); `boot_worker` transfers
  ownership BEFORE awaiting, so its failure cleanup can no longer close an fd
  number that another connection reclaimed during `terminate`. Previously a
  boot failure could double-close the receipt fd and kill an unrelated agent's
  RPC pipe or CLI/MCP IPC socket in the shared daemon (reproduced with a real
  socketpair reclaiming the freed number: peer EPIPE / writer EBADF).
  Regressions: boot-level test re-occupies the freed receipt fd with a live
  socketpair inside the cleanup window and asserts it stays functional, plus
  fd-closed-exactly-once tests for the timeout, cancellation and
  transport-creation-failure paths. Red verified against a0cf6cb.

## 0.2.2 — 2026-09-14

Five P1 fixes found by the d7ff1e9 review; each has a regression that was
verified to FAIL against the unfixed code (red/green) and PASS after the fix.

- P1-A (HTTP exchange lifecycle): the deadline, the caller's cancellation and
  connection close now cover the WHOLE exchange — send, headers, body and
  parsing — through one AbortController armed until settlement. `close()`
  aborts every in-flight exchange and rejects new ones until an explicit
  reconnect re-handshakes. Timeout and user cancellation stay distinguishable
  and keep an explicit "server outcome is unknown" for calls that were sent.
- P1-B (read-child policy): the parent's `enabled_tools` is no longer treated
  as child authorization. A read child sees only tools explicitly declared
  `readOnly` (deny rules still win; an explicit allowlist can only shrink the
  surface), always confirms before calling, and a confirmation can never
  upgrade the worker. Write children keep the per-tool/server policy.
- P1-C (stdio async EPIPE): all writes go through one controlled `sendFrame`;
  the stdin socket gets a real error listener installed before the first
  write, which settles this connection's pending requests with a
  deterministic transport error and marks the connection for reconnection.
  Best-effort cancellation notices cannot crash the worker or turn a
  cancellation into a success; stale bytes from a replaced process are
  dropped by generation.
- P1-D (tool discovery): `action=list` with a server connects that one server
  and returns its VISIBLE tool names + short descriptions (parent deny/allow
  and child access rules applied), with bounded pagination and an honest
  `truncated` flag (size caps drop whole entries instead of cutting JSON).
  The catalog invalidates on `tools/list_changed`; an in-flight crawl no
  longer repopulates a cache that was invalidated mid-fetch.
- P1-E (base environment vs inheritance): binding the worker base environment
  (PATH/HOME/authorized child_env names) now happens on EVERY scope bind,
  independent of the inheritance master switch; when the switch is off, no
  Codex source is read at all (verified with a FIFO config that would block).
  A scope whose source binding was lost in a daemon restart fails with
  `inheritance_source_unbound` instead of silently falling back to the daemon
  user's ~/.codex.

New test layers (no model calls): bridge-host red/green matrix against real
fake stdio/HTTP MCP servers (25 regressions), and a full CLI -> daemon ->
guard -> fake-Pi subprocess chain for the environment binding.

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
