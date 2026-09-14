# Codex Inheritance for Managed Children (0.2.0)

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

Booted children receive the converted configuration through an anonymous pipe
(fd number in `PI_AGENTS_BOOTSTRAP_FD`), passed daemon → worker guard → Pi
with `pass_fds` on every hop; the daemon writes the payload asynchronously
after the child starts, so payloads larger than the pipe buffer cannot
deadlock; the child reads it to EOF, closes the fd immediately, and answers
with a non-secret stderr receipt (`subagent-pi-bridge ready servers=N`) that
the daemon verifies before any task is sent — a successful `get_state` alone
never proves the bridge loaded.

The bridge (`extensions/codex-mcp-bridge.ts`, loaded only via explicit
`--extension` on managed children) exposes one tool, `codex_mcp`:

- `action = "list"`: connects to configured servers on demand (bounded
  concurrency) and lists their tools (names and short descriptions only; no
  full schemas are injected into the context).
- `action = "call"`: invokes `server` + `tool` with `args`. Connections live in
  memory for the worker's lifetime (stateful servers keep their state between
  calls); metadata caches invalidate on `tools/list_changed`; pagination,
  per-tool timeouts and cancellation are handled; a failed call is never
  retried automatically (side effects); results support text and
  `structuredContent`; other content types are reported as unsupported without
  creating files; server errors are surfaced as errors, not successes.

Read-only children (`access = "read"`) see the intersection of the server
policy and the child limit: tools inside an explicit `enabled_tools` allowlist
are callable; other tools are callable only if they advertise
`readOnlyHint = true` and every call is confirmed through
`pi_answer_agent`; everything else is invisible. The tool policy is not an OS
sandbox and does not recreate Codex sandboxing.

## Secrets

- The bound scope keeps a minimal env snapshot in daemon memory: base keys
  (`PATH`, `HOME`, `LANG`, `LC_ALL`, `TERM`, `TMPDIR`, `SHELL`, `USER`,
  `LOGNAME`, `CODEX_HOME`) plus exactly the variables the current codex config
  references. It is never persisted, logged, or included in events.
- Persistence stores non-secret source fields only (codex_home, mode, enabled
  flag, variable names in diagnostics).
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

## Known boundaries (unsupported in 0.2.0)

- OAuth / ChatGPT-session authenticated MCP servers, dynamic
  `http_headers_helper`, remote executor stdio, sampling/elicitation, and
  Codex sandbox semantics are not inherited; affected servers are disabled
  with named diagnostics rather than partially faked.
- The official Codex user skill location `~/.agents/skills` is not scanned;
  this plugin inherits `<codex_home>/skills` (and the project `.agents`
  skills) by design.
- Renaming the subagent-pi binary itself would evade the recursion guard;
  renaming servers or skills in the configs does not.
