# Codex Inheritance for Managed Children

Managed subagents started by this plugin run a normal Pi session — Pi's own
global/project configuration loads exactly as it does for a `pi` you start
yourself — and then inherit your Codex-side global skills and MCP servers on top
of it. Normal `pi` sessions are never affected: inheritance is added only to
children the daemon boots, only while it is enabled, and only from the sources
described here.

## Pi's own configuration loads first

A managed child is a normal Pi session with a private session file, so it loads
the same configuration any Pi start would:

- extensions and packages (global `~/.pi/agent/extensions`, packages, project
  extensions), including MCP-capable extensions a user installed;
- skills from Pi's own sources (agent dir, project, packages, `settings.skills`)
  and prompt templates, themes, context files and Pi settings;
- whatever tools those extensions register.

Two profile keys exist only as an explicit opt-out; both default to `true`:

```toml
[profiles.reader]
ambient_extensions = true   # false re-adds --no-extensions
ambient_skills = true       # false re-adds --no-skills
tools = ["read", "grep", "find", "ls"]
```

`tools` is the child's BUILT-IN tool surface, applied by the shipped
`extensions/managed-surface.ts` on `session_start`: it activates the profile's
built-ins AND deactivates any built-in the profile does not list, so a built-in
this plugin does not know about cannot quietly appear in a `reader` child. The
extension is passed only for profiles that actually restrict the surface (a
profile that allows every built-in needs no plan); if it is missing from the
installation, a restricting profile refuses to start instead of launching an
unbounded child.

Built-in identity comes from Pi, not from the tool name: Pi reports
`sourceInfo.path = "<builtin:NAME>"` for its own tools, and an extension file for
its own. A tool an extension registers under a built-in name (`bash`, say) keeps
Pi's state — the extension tool stays registered and active, while the real
built-in of that name is gone from the registry anyway. Extension and custom
tools are never touched by this plugin: with `PI_AGENTS_CHILD_BUILTINS` unset the
extension leaves the surface exactly as Pi computed it, and it never makes a tool
write-capable.

Both obvious CLI alternatives are deliberately NOT used, because they filter the
same registry BY NAME and would therefore also drop an extension tool that
shadows a built-in:

- `--tools` is an allowlist over built-in, extension and custom tools;
- `--exclude-tools` is a denylist over built-in, extension and custom tools.

Because the profile's surface is applied inside Pi, the daemon does not take the
argv on trust: the extension reports what Pi's live registry says after the
change (`subagent-pi-surface applied ok=... allowed=... builtins=...
expected=... unidentified=...`), the daemon records it as a `tool_surface` event,
and a restricted launch fails (`tool_surface_unavailable` when no report
arrives, `tool_surface_unapplied` when the applied set differs) rather than
pretending the restriction is in force. Built-in restrictions are only claimed
for the child that proved them.

Access modes are policy, not a sandbox:

- `access=write`: the profile's built-ins plus Pi's own extension tools.
- `access=read`: the profile's read built-ins (`read`, `grep`, `find`, `ls` by
  default) plus Pi's own extension tools. It restricts the built-ins this
  plugin controls and narrows inherited MCP to read-only tools, but it does NOT
  restrict what Pi's own extensions, their tools, or Pi's own skills can do —
  extensions are code that runs in the child. A read child is therefore not a
  read-only child, and writer exclusivity (`access=write`) is the only
  confinement this plugin enforces. Earlier versions rejected
  `read` + extensions outright and claimed a read-only tool surface; that claim
  is gone because it was never true once Pi's configuration loads.

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

## Skills: Pi names win, original paths only

When booting a managed child, the daemon appends original `--skill` paths
alongside Pi's own discovered skills:

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
missing paths are errors.

Any remaining name collision between an inherited skill and a skill Pi loaded
from the user's own Pi configuration is resolved BY PI, at the real loading
boundary: Pi registers the skills it discovered before the CLI `--skill` paths
and keeps the first registration for a name (verified against Pi 0.85.1), so
the Pi skill is the one that exists in the session. Nothing is copied, renamed
or edited: the Pi skill file stays where the user put it and the Codex skill
file is simply never registered.

Because that decision happens inside Pi, the daemon reads it back instead of
predicting it: after the boot handshake it asks the live child for
`get_commands` and stores one `inheritance_skills` event per boot. The event
lists every inherited `--skill` path with `state` `loaded`, `skipped` or
`not_loaded` — a `skipped` entry also carries the `name` and the `kept` path of
the Pi skill that won — plus `pi_skills` for the entries Pi provided on its own.
If Pi cannot answer (`get_commands` unavailable), the event records the error
and the boot still succeeds. No BM25, embedding or alias heuristics are used
anywhere: identity is the name Pi resolved, and the name Pi resolved is the one
in its registry. Codex-specific policy metadata (for example
`agents/openai.yaml` invocation policy) is not interpreted: if a skill relies
on host-specific behavior, it will load but its policy is not enforced by Pi —
check such skills before delegating them.

Skills are referenced in place, not frozen: a running child keeps what it
discovered at boot; re-reading a file later shows the current content.

## MCP: no Pi-side registry to deduplicate against

Pi 0.85.1 has no MCP subsystem at all: `pi --help` has no MCP flag, Pi's
settings have no MCP key, a Pi package manifest declares only
`extensions`/`skills`/`prompts`/`themes`, and the documented position is
explicitly "No MCP" (MCP belongs in an extension or package a user installs).
MCP servers therefore exist in a managed child ONLY through the bridge this
plugin boots.

That has one consequence worth stating plainly: the "Pi already has a server
with this name, keep Pi's and skip the Codex one" rule from the requirements
CANNOT be implemented, and it is NOT implemented. Pi exposes no MCP server
registry, no server identity and no configuration format for one — `get_commands`
lists commands, and the extension API's `getAllTools()` reports tool names plus
their owning extension file, never an MCP server. Deduplicating by server name
would require guessing a private naming convention inside somebody's MCP
extension, or inventing a Pi MCP config format, and this plugin does neither.
The blocker is a missing upstream mechanism, not a deferred task here: if Pi
ever exposes a server registry, the dedup belongs next to `parse_mcp_servers`.

What IS guaranteed is the part Pi cannot break: inherited MCP is addressed by
`(server, tool)`, so two servers that both offer a tool named `search` stay
distinct in the catalog and dispatch to their own server. Same-named tools in
different servers are never treated as a conflict. In practice a Codex server
name can also not collide with a Pi-side server, because by construction there
is no Pi-side server.

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

## MCP protocol compatibility (0.2.6)

The HTTP transport supports two protocol eras, selected ONCE per deployment via
`[inheritance] mcp_protocol_mode = "auto" | "legacy_2025_06_18" |
"modern_2026_07_28"` (default `auto`) — never per call by the model. stdio
follows the current Codex per-server opt-in: a server whose `env` carries
`CODEX_MCP_PROTOCOL_VERSION = "2026-07-28"` runs the modern stateless stdio
lifecycle (no initialize handshake; every request self-describes via `_meta`).
The marker is a CLIENT-side selection signal: the parent consumes it and never
forwards it to the server process, exactly as Codex does. An unknown marker
value fails the server closed (optional servers are excluded, required servers
block readiness, and the process is never started). A global
`legacy_2025_06_18` override keeps such servers on the legacy handshake while
still stripping the marker.

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
side-effect-free `server/discover` and classifies the outcome by the bounded
JSON-RPC error BODY through one shared modern-error classifier (-32020
HeaderMismatch, -32021 MissingRequiredClientCapability, -32022
UnsupportedProtocolVersion — all modern, no initialize fallback; -32022
additionally requires a common `data.supported` version, otherwise a clear
incompatibility is reported without falling back or replaying). HTTP 200
in-band JSON-RPC errors are classified by structured code too, and a successful
discover must be a real DiscoverResult (`resultType` + `supportedVersions` with
a common modern version); a malformed one is a protocol error, never a
fallback. An unrecognized or legacy-style 400, a 404 or a 405 prove legacy-only
and trigger the handshake. 401/403/429/5xx are errors, never downgrade triggers.
`x-mcp-header` is honored as a SCHEMA
annotation: a tool may declare that a plain string/integer/boolean argument is
mirrored into an `Mcp-Param-*` header on modern calls (body unchanged; absent
arguments produce no header; runtime values are type-checked against the
declared schema before anything is sent). Annotations may sit on nested
properties chains (`arguments.parent.child`); an annotation under a dynamic
position (items/oneOf/anyOf/allOf/not/if/then/else/$ref/…) invalidates just
that tool. Header values follow the 2026-07-28 encoding: plain visible ASCII
travels as-is; non-ASCII, control characters, edge whitespace and
sentinel-shaped values are sent as `=?base64?<Base64 UTF-8>?=` — the same
encoder applies to `Mcp-Name`. Annotations are validated at discovery (HTTP
token, case-insensitive uniqueness, bounded walker depth/nodes); a tool with an
invalid annotation is excluded and rejected by describe/call without failing
the server. stdio and legacy connections ignore the annotation entirely.

## Codex MCP config compatibility (0.2.6)

`subagent_pi/inheritance.py` keeps ONE declarative table
(`MCP_FIELD_COMPAT`) classifying every current Codex `RawMcpServerConfig`
field; the doctor reports the supported surface as `baseline` instead of a
frozen upstream version string.

- mapped: transport fields (`command/args/env/env_vars/cwd`,
  `url/auth/bearer_token_env_var/http_headers/env_http_headers`),
  `startup_timeout_sec`/`startup_timeout_ms` (sec wins when both present,
  current Codex semantics; floating-point seconds are accepted),
  `tool_timeout_sec` (float accepted), `enabled`, `required`,
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

## Approval policy and read children

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
  `LOGNAME`, `PI_CODING_AGENT_DIR`, `CODEX_HOME`), exactly the variables the
  current codex config references, and explicitly configured
  `inheritance.child_env` names (for model-auth env vars).
  `PI_CODING_AGENT_DIR` is a non-secret LOCATION the child Pi resolves itself
  (it decides where Pi's own configuration comes from), which is why it is
  forwarded and persisted with the other base keys while `CODEX_HOME` is not:
  the daemon resolves the Codex source itself. Secret values are never persisted, logged, or included
  in events. The base keys are the one exception: they are non-secret, so they
  are stored per scope in the ledger (`scopes.base_env`) and reloaded after a
  daemon restart — without that, a respawned worker would start with no `PATH`.
- The guard and Pi worker do NOT inherit the daemon's environ: their base
  environment is built from the scope snapshot above, so session B never sees
  session A's credentials and a managed child opens the same Pi configuration
  directory the client that opened the scope used (unset stays Pi's default;
  `[profiles.x.env]` still wins). This base binding happens on EVERY worker boot,
  independent of the inheritance master switch — with inheritance disabled a
  worker still gets its own PATH/HOME (and never triggers any Codex source
  access). The bridge gives each MCP stdio server only the base keys `PATH`,
  `HOME`, `LANG`, `LC_ALL`, `TERM`, `TMPDIR` plus that server's declared `env`
  values — a deliberately narrower set than the worker's own.
- Persistence stores non-secret source fields only (codex_home, mode, enabled
  flag, variable NAMES in diagnostics, env NAMES in launch.json, base env
  values); profile env VALUES are re-read from the operator config at every
  boot and never enter the ledger, launch.json, requests or events.
- After a daemon restart the scope's source path is still known and the base
  keys reload from the ledger, but other parent env values are gone:
  env-referencing servers are excluded with named diagnostics, and required
  ones fail with `inheritance_required_server_failed` until the owning client
  re-binds the scope (next `pi_context` from Codex or `subagent-pi codex`
  re-captures it). Values are never borrowed from another scope or from the
  daemon environment.
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

## Known boundaries

- MCP server-name deduplication against Pi's own MCP set is impossible while
  Pi has no MCP concept (see above); only the `(server, tool)` addressing
  guarantee is provided. Do not read the rest of this document as claiming
  otherwise.
- The proxy tool name `codex_mcp` is not reserved against Pi's own extensions:
  if a user-installed extension registers a tool with that name, Pi's registry
  keeps the load-order winner and this plugin neither detects nor reorders it.
  That is a tool-name collision inside Pi, not server-name dedup, and it is a
  reason to keep ambient extensions that provide MCP out of managed children
  (`ambient_extensions = false`) if you rely on the inherited bridge.
- A `read` child's built-in surface is bounded by the surface extension (which
  deactivates every built-in the profile does not list, including built-ins this
  plugin does not know) and verified from the child's live registry before the
  boot counts. That verification is a snapshot at `session_start`: a later
  handler from another extension could re-activate a tool, and Pi's own
  extensions can do anything their code allows. Read access is a managed
  tool-surface policy, never a sandbox.
- Extensions/custom tools keep Pi's activation state, so an extension can expose
  its own write-capable tool to a `read` child. The plugin does not filter
  extension tools by name; configure such extensions out
  (`ambient_extensions = false`) if that matters.

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
