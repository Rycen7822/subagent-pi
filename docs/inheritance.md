# Codex inheritance for managed children

The plugin opens a private Pi SDK session, loads Pi's ambient resources, and adds selected Codex skills and MCP servers. It does not modify the installed Pi or its configuration. Task ownership, ordered continuations and interruption are described in [lifecycle.md](lifecycle.md).

## Configuration and source binding

```toml
# <state-home>/config.toml
[inheritance]
enabled = true
skills = true
mcp = true
mcp_protocol_mode = "auto"
child_env = []              # explicitly authorized environment variable names
# codex_home = "/abs/path"

[profiles.reader]
ambient_extensions = true
ambient_skills = true
tools = ["read", "grep", "find", "ls"]
```

`enabled` is the master switch. A scope can independently opt out through `pi_context` with `inheritance: false`; explicit `inheritance` or `codex_home` parameters also authorize rebinding. Profiles can disable Pi's ambient extensions or skills separately. Disabling Codex inheritance does not disable Pi resources or the worker's base environment binding.

A scope's Codex source resolves from explicit `codex_home`, then the opening client's `CODEX_HOME`, then `~/.codex`. Explicit missing directories are errors; an absent default source is empty. The source path and mode are persisted. A client resolving a different source for an existing scope gets `inheritance_source_conflict` unless it explicitly rebinds.

`subagent-pi codex` recognizes `-C`, `--cd`, `--cd=DIR` and `-CDIR` before `--`, binds that workspace, and forwards arguments unchanged. `subagent-pi doctor --inheritance` reports bound sources, skills, server dispositions and missing variable names without credential values.

## Pi resources and built-in tools

Ambient loading includes global/project extensions and packages, skills, prompt templates, themes, settings and context files. `PI_CODING_AGENT_DIR` selects the same Pi configuration directory as the opening client; unset retains Pi's default. Profile environment overrides take precedence.

A profile's `tools` controls only Pi's built-ins. `extensions/managed-surface.ts` identifies them through `sourceInfo.path = "<builtin:NAME>"`, activates allowed built-ins and deactivates the rest. Extension/custom tools retain Pi's activation state, including extensions that shadow a built-in name. Name-based `--tools` or `--exclude-tools` filtering would also remove those extension tools and is not used for this policy.

Only restricting profiles load the surface extension. A missing extension prevents launch. On `session_start`, it reads back the registry and emits a report; the daemon records `tool_surface` and rejects a missing or mismatched result with `tool_surface_unavailable` or `tool_surface_unapplied`.

This is a boot-time snapshot, not continuous enforcement. Another extension may later change active tools, perform writes, start other processes or make its own model calls. `access=read` restricts controlled built-ins and inherited MCP exposure; it is not an OS sandbox or a guarantee that arbitrary Pi extensions are read-only. Writer exclusivity covers only this plugin's managed writers.

## Skills: original paths, Pi names win

Inherited sources are profile paths, project `.agents/skills`, then `<codex_home>/skills/*/SKILL.md`. Paths are referenced in place, never copied or renamed. Collection:

- Deduplicates resolved paths, including symlinks.
- Honors `[[skills.config]]` entries with `enabled = false`.
- Excludes this plugin's management skills by install path and declared name.
- Skips ambiguous declared-name collisions among inherited candidates with diagnostics.
- Accepts at most 64 new managed skills; a missing default directory is reported as empty.

Pi resolves remaining collisions with its own discovered skills. After boot, the daemon reads the live `get_commands` registry and records `inheritance_skills`: each inherited path is `loaded`, `skipped` with the winning Pi path, or `not_loaded`. A registry-query failure is recorded without failing boot. There is no semantic matching or aliasing.

The global `~/.agents/skills` directory is not scanned by this inheritance layer. Codex-specific policy metadata such as `agents/openai.yaml` is diagnosed but not interpreted or enforced. A running child retains its discovered registry; skill files themselves remain live source files.

## MCP discovery and policy

The daemon reads `[mcp_servers.*]` from the bound Codex config at each boot. The bridge exposes one `codex_mcp` tool:

| Action | Inputs | Behavior |
|---|---|---|
| `list` | none | Lists configured servers and policy without connecting. |
| `list` | `server` | Connects that server and returns a bounded visible tool catalog. |
| `describe` | `server`, `tool` | Returns the effective input schema. |
| `call` | `server`, `tool`, `args` | Applies policy and invokes once. |

Tools are addressed by `(server, tool)`, so identical tool names on different inherited servers remain distinct. The plugin cannot deduplicate servers against arbitrary Pi MCP extensions: their server registry is not exposed through the API this bridge uses. Ambient extensions may provide their own MCP integration. A competing `codex_mcp` tool name is also left to Pi's load order; the plugin does not reserve or reorder it.

Deny rules take priority. `enabled_tools` further narrows exposure; an empty list allows nothing. Write children use per-tool approval, then server approval, defaulting to confirmation. `auto` permits direct calls; `prompt`/`approve` confirm; `writes` degrades to confirmation with a diagnostic. An unknown server approval mode confirms; an unknown per-tool mode denies that tool.

Read children expose only tools explicitly advertising `readOnlyHint: true` and require confirmation for every call, even when parent policy says `auto`. The hint is a server self-report, not a sandbox. Confirmations use `pi_answer_agent`; denial sends no tool call. Transport errors and MCP `isError` results become Pi tool errors. In-flight cancellation reports an unknown outcome and never retries the call.

All proxy operations serialize through one in-memory chain and Pi's sequential tool mode. Catalogs stay in memory, invalidate on `notifications/tools/list_changed`, and expose truncation when pagination, cursor-loop or size limits stop discovery.

## Supported configuration

`subagent_pi/inheritance.py:MCP_FIELD_COMPAT` defines the accepted field surface. Unknown execution/auth fields fail the server with a named reason; required failures block boot, optional failures exclude that server.

| Fields | Handling |
|---|---|
| `command`, `args`, `cwd`, `env`, `env_vars` | stdio; local environment references only. Relative cwd anchors to Codex home with a diagnostic. Arguments and values are not shell-expanded. |
| `url`, `http_headers`, `env_http_headers`, `bearer_token_env_var`, `auth="bearer"` | Streamable HTTP with static or bound credentials. |
| `startup_timeout_sec`, `startup_timeout_ms`, `tool_timeout_sec` | Seconds accept fractions; milliseconds require integers. Explicit seconds take precedence. Defaults: startup 10 s, tool 60 s. |
| `enabled`, `required`, `enabled_tools`, `disabled_tools`, approval fields | Applied to availability, readiness, visibility and confirmation. |
| `tools.<name>.output_token_limit` | Tightens result output to a conservative four bytes per token, capped at 256 KiB. Invalid values deny the tool. |
| `supports_parallel_tool_calls`, legacy `name` | Recorded as no-effect hints. Calls remain serialized. |
| `environment_id` | Absent or `local` accepted; remote rejected. |
| `http_headers_helper`, `experimental_environment`, `omit_tools_from`, `scopes`, `oauth`, `oauth_resource` | Unsupported; no header-helper execution, remote executor or credential store. |

Unknown per-tool fields deny that tool. OAuth/ChatGPT-session authentication, sampling/elicitation and Codex sandbox semantics are not inherited. Recursion checks reject this plugin's management executable, installer wrapper and module forms; arbitrary renamed binaries or same-user code are outside that guard.

## Transport contracts

HTTP supports `auto`, `legacy_2025_06_18` and `modern_2026_07_28`, selected by configuration. Stdio defaults to legacy; per-server `env.CODEX_MCP_PROTOCOL_VERSION = "2026-07-28"` opts into modern discovery. This client-side marker is stripped before spawning the server. Unknown values fail; a global legacy override takes precedence while still stripping the marker.

Legacy connections use `initialize` and `notifications/initialized`. HTTP preserves the returned protocol version and session ID. A session-scoped HTTP 404 makes the connection stale: the sent operation is not replayed; the next explicit operation creates a fresh connection. Close attempts a bounded, best-effort session DELETE.

Modern requests carry protocol/client metadata; HTTP also sends protocol, method and tool-name headers. Discovery is side-effect-free. Recognized modern errors `-32020`, `-32021` and `-32022` retain modern mode; the last requires a common supported version. Successful discovery must contain `resultType` and compatible `supportedVersions`; malformed success never triggers fallback.

Fallback differs by transport: modern stdio falls back to legacy on other discovery failures, including timeout. HTTP auto falls back on legacy-style 400/404/405 or in-band method-not-found, while auth/rate-limit/5xx, timeouts and unrelated in-band errors remain failures. No tool call is automatically replayed in either mode.

On modern HTTP calls, valid schema `x-mcp-header` annotations mirror string, safe-integer or boolean arguments into `Mcp-Param-*` headers without altering the body. Nested plain `properties` paths are supported; dynamic schema paths, invalid tokens and duplicate header names invalidate the tool. Absent arguments produce no header. Non-ASCII, control characters, edge whitespace and Base64-sentinel-shaped values are encoded as `=?base64?<Base64 UTF-8>?=`; tool names use the same encoding. HTTP catalog validation rejects invalid plans; legacy calls do not mirror headers and stdio ignores the annotation.

HTTP JSON/SSE exchanges share one deadline across headers, body and parsing. Redirects remain within the original origin: 307/308 preserve the request; 301/302/303 become bodyless GETs. Loops and excessive hops fail before sending to the rejected target.

Each stdio connection owns exactly one process. Exit or EPIPE ends its pending requests; only a subsequent explicit operation creates and initializes a replacement. Response, timeout, cancellation and close share request cleanup. Failed cancellation notifications remain best-effort and never turn cancellation into success.

## Environment, readiness and recovery

The opening client captures only base keys, referenced MCP variables and explicitly allowed `inheritance.child_env` names. Worker base keys are `PATH`, `HOME`, `LANG`, `LC_ALL`, `TERM`, `TMPDIR`, `SHELL`, `USER`, `LOGNAME` and `PI_CODING_AGENT_DIR`. `CODEX_HOME` is a source pointer for the daemon, not a forwarded worker variable. Profile env values are read from current operator config at each boot.

Non-secret source paths, base environment values and variable names may enter the ledger. Credential values remain in daemon memory and the private bootstrap pipe; they are not written to launch snapshots, diagnostics or converted MCP files. Workers never copy the daemon's full environment. MCP stdio servers receive the narrower base set `PATH`, `HOME`, `LANG`, `LC_ALL`, `TERM`, `TMPDIR`, plus their declared environment.

After daemon restart, base keys and source paths survive, but credential snapshots do not. Missing references exclude optional servers or fail required ones until the owning client reopens the scope. Values are not borrowed from another scope. Third-party extensions and MCP servers can write files themselves; these storage guarantees describe the inheritance layer.

The bootstrap pipe carries converted configuration daemon → guard → child. The writer owns its descriptor until completion; a timeout abandons waiting without closing a descriptor another thread still uses. The bridge sends a separate structured readiness receipt with agent ID, generation and server statuses. Required servers initialize before that receipt; no task starts without matching readiness. Optional servers stay lazy.

Each spawn/respawn rereads inherited sources and rebuilds flags without growing persisted argv. A living child retains its configuration and connections; there is no watcher or immediate revocation. Close and explicitly respawn to load changes. SDK settings mutations remain local to the child; arbitrary extension side effects are outside that guarantee.
