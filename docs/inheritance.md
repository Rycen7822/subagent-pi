# Codex Inheritance for Managed Children (0.2.5)

Managed subagents started by this plugin can use your Codex-side global skills
and MCP servers. Normal `pi` sessions are never affected: inheritance is added
only to children the daemon boots, only while it is enabled, and only from the
sources described here.

## Enable / disable

Default: enabled for scopes bound through this plugin.

```toml
# <state-home>/config.toml  (PI_AGENTS_HOME)
[inheritance]
enabled = true            # master switch
skills = true             # inherit Codex global skills
mcp = true                # inherit Codex global MCP servers
# codex_home = "/abs/path"  # optional explicit trusted source (must exist)
```

Disable for one scope explicitly (management action, also re-verifies the
source): call `pi_context` again with `inheritance: false` (or `true` to force
it on). A scope can also be rebound to another source with
`codex_home: "/abs/path"` in `pi_context`.

## Source resolution (codex_home)

Resolved once per scope, when a trusted client opens it, in this order:

1. `inheritance.codex_home` from the plugin config (explicit, trusted).
2. `CODEX_HOME` from the environment of the process that opened the scope
   (the Codex-spawned MCP adapter, `subagent-pi codex`, or the CLI).
3. `~/.codex` (user default).

The binding (absolute path + mode: `explicit`/`scope_env`/`user_default`) is
persisted with the scope (non-secret). A second client resolving a different
source for the same scope is rejected with `inheritance_source_conflict`
unless the client passes `codex_home`/`inheritance` explicitly.
`subagent-pi codex` recognizes Codex's `-C/--cd` (including `--cd=DIR`) before
`--` and binds the scope to that directory; forms it cannot parse are reported
instead of guessed. `subagent-pi doctor --inheritance` shows each scope's
source, selected skills/servers, exclusion reasons and missing credential
names — never values.

## Skills: original paths only

When booting a managed child, the daemon appends original `--skill` paths
(Pi keeps discovery off via `--no-skills` and loads exactly these):

- project `.agents/skills` under the scope root (existing convention, in place);
- profile skills (unchanged behavior);
- `<codex_home>/skills/*` — directories with a `SKILL.md`.

Rules: deduplicated by real path (symlinks are followed for identity, no new
links are created); `[[skills.config]]` entries with `enabled = false` in
`<codex_home>/config.toml` are honored; the plugin's own management/delegation
skills are excluded (recursion guard, by real path inside the plugin install
and by declared name); skills with the same name from different sources are
refused with a diagnostic instead of an arbitrary pick; a missing default
directory is an empty set with a short diagnostic, while user-configured
missing paths are errors. Codex-specific policy metadata (for example
`agents/openai.yaml` invocation policy) is not interpreted: if a skill relies
on host-specific behavior, it will load but its policy is not enforced by Pi —
check such skills before delegating them.

Skills are referenced in place, not frozen: a running child keeps what it
discovered at boot; re-reading a file later shows the current content.

## MCP: read-only TOML, in-memory conversion

The daemon parses `[mcp_servers.*]` from `<codex_home>/config.toml` with
`tomllib` at boot time and keeps the result in memory. Nothing is written: no
converted `mcp.json`, no snapshots, no per-child caches.

Supported per server (verified against Codex 0.154.0 semantics):

- stdio: `command`, `args`, `cwd`, `env`, `env_vars` (plain names and
  `source = "local"` objects; `source = "remote"` disables the server with a
  diagnostic).
- streamable HTTP: `url`, `http_headers`, `env_http_headers`,
  `bearer_token_env_var`.
- `enabled`, `required`, `enabled_tools`, `disabled_tools`,
  `startup_timeout_sec` (default 10), `tool_timeout_sec` (default 60),
  `default_tools_approval_mode` and `tools.<name>.approval_mode`
  (`auto` = call directly; `prompt`/`approve` = confirm through the normal
  `pi_answer_agent` channel; `writes` cannot be enforced without trusting
  `readOnlyHint`, so it degrades to confirmation with a diagnostic; unset =
  confirm).
- An explicitly empty `enabled_tools = []` allows no tools; an absent list
  allows all of the server's tools. `disabled_tools` is applied after
  `enabled_tools`.
- Relative `cwd` is anchored to the codex home (Codex does not document the
  rule; the decision is recorded as a diagnostic). No shell is involved;
  `$VAR`/`${VAR}`/`~` inside args or env values are passed through verbatim.
- Unknown keys that affect execution or authorization disable that server with
  a named diagnostic; other servers continue. Rejected examples:
  `auth = "oauth"` / `"chatgpt"`, `http_headers_helper`,
  `experimental_environment = "remote"`, `tools.<name>.output_token_limit`.
- `required = true` plus a failed dependency aborts the spawn/respawn with
  `inheritance_required_server_failed`; optional failures exclude only that
  server and are reported.

Name conflicts with the plugin's own management server are impossible to
inherit: a server whose resolved command is this plugin's executable is
excluded by its execution definition (renaming the server in the config does
not bypass this; renaming the binary itself is out of scope).

## The private channel and the child extension

## MCP protocol compatibility (0.2.4)

The HTTP transport supports two protocol eras, selected ONCE per deployment via
`[inheritance] mcp_protocol_mode = "auto" | "legacy_2025_06_18" |
"modern_2026_07_28"` (default `auto`) — never per call by the model. stdio
servers stay on legacy: modern requires the upstream `CODEX_MCP_PROTOCOL_VERSION`
env opt-in, which managed children never forward, so a modern-only stdio server
fails loudly instead of silently misbehaving.

Legacy 2025-06-18: `initialize` NEGOTIATES the protocol version (the server's
returned version is used on every later request via the `MCP-Protocol-Version`
header) and carries `Mcp-Session-Id`. A 404 on a session-scoped request marks
the connection stale: the sent request is NEVER replayed, a sent `tools/call`
reports "outcome is unknown", and the next explicit operation re-initializes a
fresh session. Closing the worker sends a best-effort session DELETE; an
unconfirmed DELETE is a documented boundary.

Modern 2026-07-28: stateless — no handshake and no session id. Every request
self-describes through `_meta` (`io.modelcontextprotocol/protocolVersion`,
`clientInfo`, `clientCapabilities`) plus the `MCP-Protocol-Version` and
`Mcp-Method` headers (`Mcp-Name` on `tools/call`). `auto` probes once with the
side-effect-free `server/discover` and falls back to the legacy handshake ONLY
on proof of legacy-only (HTTP 404/405 or JSON-RPC -32601 on that probe); a
generic 4xx/5xx is an error, never a downgrade trigger. `x-mcp-header` is honored as a SCHEMA
annotation: a tool may declare that a plain string/integer/boolean argument is
mirrored into an `Mcp-Param-*` header on modern calls (body unchanged; absent
arguments produce no header; unsafe integers and control-character values are
refused). Annotations are validated at discovery (HTTP token, case-insensitive
uniqueness, no dynamic paths); a tool with an invalid annotation is excluded
and rejected by describe/call without failing the server. stdio and legacy
connections ignore the annotation.

## Codex MCP config compatibility (0.2.4)

`subagent_pi/inheritance.py` keeps ONE declarative table
(`MCP_FIELD_COMPAT`) classifying every current Codex `RawMcpServerConfig`
field; the doctor reports the supported surface as `baseline` instead of a
frozen upstream version string.

- mapped: transport fields (`command/args/env/env_vars/cwd`,
  `url/auth/bearer_token_env_var/http_headers/env_http_headers`),
  `startup_timeout_sec`/`startup_timeout_ms` (sec wins when both present,
  current Codex semantics), `tool_timeout_sec`, `enabled`, `required`,
  `enabled_tools`, `disabled_tools`, `default_tools_approval_mode`,
  `tools.<n>.approval_mode`.

The `codex_mcp` proxy tool is registered with sequential execution and an
in-memory promise chain, so inherited MCP calls serialize conservatively no
matter what a server declares in `supports_parallel_tool_calls` (recorded as a
no-effect hint).
- accepted_no_effect: `supports_parallel_tool_calls` (the proxy tool executes
  calls sequentially — do not advertise parallel safety) and the legacy
  per-server `name` label; recorded as diagnostics, never fatal.
- explicitly_unsupported: `http_headers_helper`, `experimental_environment`
  (remote executor), `omit_tools_from` (ToolExposureSurface cannot be mapped
  without guessing), `scopes`/`oauth`/`oauth_resource` (no token or credential
  store is ever created or copied). required servers fail the boot; optional
  ones are excluded with named diagnostics.
- conditional_local: `environment_id` — absent or `local` works; anything else
  is unsupported (no remote executor in children).
- unknown_fail_closed: any field not in the table disables that server, so a
  future upstream field fails the compatibility matrix test loudly instead of
  being accepted or dropped silently.

Tool-level `output_token_limit` is mapped, not denied: the bridge enforces it
at the proxy result serialization boundary as a tighten-only byte budget
(conservative 4 bytes/token), capped at the global result limit.

## Tool discovery (unknown tool names)

The single `codex_mcp` tool supports the whole chain without prior knowledge
of tool names:

1. `action=list` (no server): configured servers and their policy; nothing is
   connected.
2. `action=list` + `server`: connects THAT server only and lists its visible
   tools (names, short descriptions, read-only flag) with bounded pagination
   and an honest `truncated` flag when a bound stopped the crawl.
3. `action=describe` + `server` + `tool`: the full, effective `inputSchema`.
4. `action=call` + `server` + `tool` + `args`: runs the same effective policy
   check against current metadata before executing.

The catalog lives in memory only, is filtered by deny/allow and the child
access rule at every level, and is invalidated when the server sends
`notifications/tools/list_changed`.

Booted children receive the converted configuration through an anonymous pipe
(fd number in `PI_AGENTS_BOOTSTRAP_FD`), passed daemon → worker guard → Pi
with `pass_fds` on every hop; the daemon writes the payload asynchronously in
a thread that owns and closes the fd (a timeout abandons the wait, never
closes the fd mid-write). The bridge exposes one tool, `codex_mcp`, with:

- `action = "list"`: configured servers and policy only — no connections.
- `action = "describe"`: the full `inputSchema` of one tool, kept in process
  memory (no disk cache). Deny/allow filtering applies to list/describe/call.
- `action = "call"`: invokes `server` + `tool` with `args`.

Connections live in memory for the worker's lifetime (stateful servers keep
their state between calls). The supported lifecycle subset is initialize
(protocol 2025-06-18), `tools/list` with pagination bounds and cursor-loop
protection, `tools/call`, `notifications/initialized`,
`notifications/cancelled` and `notifications/tools/list_changed`, over
newline-delimited stdio JSON-RPC or the streamable-HTTP transport (JSON or
SSE responses, `mcp-session-id`). Anything outside this subset is rejected
explicitly instead of half-implemented.

After registering the tool, the bridge writes a STRUCTURED receipt — JSON,
non-secret, carrying the agent id, generation and per-server status — to the
fd in `PI_AGENTS_BRIDGE_RECEIPT_FD`. The daemon parses it exactly from the
private stream (never a substring scan of the size-capped stderr.log).
REQUIRED servers are initialized eagerly before the receipt; the daemon does
not send a task until the receipt says ready for this exact agent generation.
Optional servers connect lazily on first use.

Stdio transport failures are lifecycle events, not host crashes: the stdin
socket carries a real error handler installed before the first write, so an
asynchronous EPIPE (server closed its read end) settles that connection's
pending requests with a deterministic transport error and marks the connection
for reconnection on the next explicit operation. A cancellation notice whose
send fails is swallowed; it never crashes the worker nor turns the
cancellation into a success.

## Approval policy and read-only children

The effective approval for a tool is resolved top-down: deny (disabled_tools
or unimplementable tool config) highest, then the per-tool `approval_mode`
override, then the server `default_tools_approval_mode`, then "confirm".
`writes` and unknown values degrade to confirm with a named diagnostic.

A read child's exposure is computed from the CHILD access, independent of the
parent-side policy: only tools explicitly declared `readOnly` are visible
(parent deny rules still win, and a parent `enabled_tools` allowlist can only
SHRINK the surface — an empty allowlist allows nothing), and EVERY call
confirms first. The parent's `enabled_tools` is a parent-side declaration, not
child authorization; parent-side `auto` can never waive the child
confirmation, and a confirmation dialog never upgrades the worker. Write
children keep the per-tool/server policy from the parent config.
`readOnlyHint` is a server self-report: it affects the managed tool surface
only and is not a sandbox claim. Confirmations flow through the normal
`pi_answer_agent` channel; a denied or cancelled confirmation sends no
`tools/call` at all.

The tool policy is not an OS sandbox and does not recreate Codex sandboxing.
MCP `isError` results and transport failures surface as real tool errors via
Pi's error mechanism; a failed call is never retried automatically, and a call
cancelled in flight is reported with an explicit "outcome unknown" rather
than pretending it did not run.

## Secrets

- The bound scope keeps a minimal env snapshot in daemon memory: base keys
  (`PATH`, `HOME`, `LANG`, `LC_ALL`, `TERM`, `TMPDIR`, `SHELL`, `USER`,
  `LOGNAME`, `CODEX_HOME`), exactly the variables the current codex config
  references, and explicitly configured `inheritance.child_env` names (for
  model-auth env vars). It is never persisted, logged, or included in events.
- The guard and Pi worker do NOT inherit the daemon's environ: their base
  environment is built from the scope snapshot above, so session B never sees
  session A's credentials. This base binding happens on EVERY scope bind,
  independent of the inheritance master switch — with inheritance disabled a
  worker still gets its own PATH/HOME (and never triggers any Codex source
  access). The bridge gives each MCP stdio server only the
  same base keys plus that server's declared `env` values.
- Persistence stores non-secret source fields only (codex_home, mode, enabled
  flag, variable NAMES in diagnostics, env NAMES in launch.json); profile env
  VALUES are re-read from the operator config at every boot and never enter
  the ledger, launch.json, requests or events.
- After a daemon restart the scope's source path is still known, but parent
  env values are gone: env-referencing servers are excluded with named
  diagnostics, and required ones fail with
  `inheritance_required_server_failed` until the owning client re-binds the
  scope (next `pi_context` from Codex or `subagent-pi codex` re-captures it).
  Values are never borrowed from another scope or from the daemon environment.
- Diagnostics report variable/header names and sources, never values; URLs are
  redacted of query strings.
- Third-party MCP servers may write their own files when they run; the
  inheritance layer cannot prevent that and does not claim to. The plugin's
  own inheritance path adds no runtime files beyond the ledger entries it
  already used (launch.json keeps only the non-secret launch description).

## Lifecycle

- New worker (spawn, respawn, post-crash boot): re-reads the original sources;
  inherited `--skill`/`--extension` flags are rebuilt each time and are never
  appended to the persisted argv, so respawns cannot accumulate flags and
  deleted skills disappear.
- Living worker (send/steer/follow-up): keeps its in-memory configuration and
  connections; config file changes are not watched.
- To pick up config changes: `close` (or `respawn` after exit). There is no
  watcher and no immediate revocation of an already-running child.
- Model pinning, run/request identities, single-session writer, receipts and
  result-hash acknowledgement are unchanged.

## Known boundaries (unsupported in 0.2.2)

- OAuth / ChatGPT-session authenticated MCP servers, dynamic
  `http_headers_helper`, remote executor stdio, sampling/elicitation, and
  Codex sandbox semantics are not inherited; affected servers are disabled
  with named diagnostics rather than partially faked.
- The official Codex user skill location `~/.agents/skills` is not scanned;
  this plugin inherits `<codex_home>/skills` (and the project `.agents`
  skills) by design.
- Codex-specific skill policy metadata (`agents/openai.yaml`) is not
  interpreted or enforced by Pi; such skills load with a diagnostic saying so.
- Renaming the subagent-pi binary itself would evade the recursion guard;
  renaming servers or skills in the configs does not. The guard is a managed
  tool-surface restriction, not a sandbox against arbitrary same-user
  processes.
