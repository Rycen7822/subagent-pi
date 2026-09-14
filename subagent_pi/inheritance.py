"""Narrow Codex-source resolver: codex home, global skills, and in-memory MCP config.

Everything here reads the original files at resolution time and returns plain
in-memory structures. Nothing writes configuration, snapshots, or caches.
Secrets (env values) are resolved only in the daemon process and only for the
bound scope; diagnostics never contain values, only names and sources.
"""
from __future__ import annotations
from pathlib import Path
import re
import shutil
import tomllib

from .common import AgentError

MAX_MANAGED_SKILLS = 64
MAX_MCP_SERVERS = 32
MAX_ENV_VARS = 64
MAX_ENV_VALUE = 16384
BASE_ENV_KEYS = ('PATH', 'HOME', 'LANG', 'LC_ALL', 'TERM', 'TMPDIR', 'SHELL', 'USER', 'LOGNAME', 'CODEX_HOME')
MANAGEMENT_SKILL_NAMES = {'pi-subagents'}
# Keys that affect authorization, credentials, or execution environment.
# An unknown key from this set disables the server instead of being ignored.
MCP_STDIO_KEYS = {'command', 'args', 'env', 'env_vars', 'cwd', 'startup_timeout_sec', 'tool_timeout_sec',
                  'enabled', 'required', 'enabled_tools', 'disabled_tools', 'default_tools_approval_mode',
                  'tools', 'experimental_environment'}
MCP_HTTP_KEYS = {'url', 'auth', 'bearer_token_env_var', 'http_headers', 'env_http_headers', 'http_headers_helper',
                 'startup_timeout_sec', 'tool_timeout_sec', 'enabled', 'required', 'enabled_tools',
                 'disabled_tools', 'default_tools_approval_mode', 'tools'}
APPROVAL_MODES = {'auto', 'prompt', 'writes', 'approve'}


def redact_url(url: str) -> str:
    """Strip query and fragment; they may carry credentials."""
    for sep in ('?', '#'):
        idx = url.find(sep)
        if idx >= 0:
            url = url[:idx]
    return url


class Diagnostic:
    def __init__(self, scope: str, name, reason: str):
        # Diagnostics cross JSON boundaries (events, doctor, responses). Path
        # objects and other non-string names are coerced here, at the edge, so
        # serialization never fails on a missing directory or odd config value.
        self.scope, self.name, self.reason = scope, (name if isinstance(name, str) else str(name)), reason
    def as_dict(self):
        return {'scope': self.scope, 'name': self.name, 'reason': self.reason}


def resolve_codex_home(config, source_env: dict | None) -> tuple[Path | None, str]:
    """Explicit trusted setting -> scope-bound CODEX_HOME -> ~/.codex.

    Accepts either the full plugin config or its [inheritance] table directly.
    A configured source that does not exist is an error, never a silent
    fallback to another candidate: only an UNSET source falls back, and only
    the user default may be absent (reported as empty capability).
    """
    inh = config.get('inheritance', config) if isinstance(config, dict) else {}
    explicit = inh.get('codex_home') if isinstance(inh, dict) else None
    if explicit:
        home = Path(explicit).expanduser()
        if not home.is_dir():
            raise AgentError('inheritance_source_unreadable',
                             f'inheritance.codex_home does not exist: {home}')
        return home, 'explicit'
    if isinstance(source_env, dict) and isinstance(source_env.get('CODEX_HOME'), str) and source_env['CODEX_HOME'].strip():
        home = Path(source_env['CODEX_HOME']).expanduser()
        if not home.is_dir():
            raise AgentError('inheritance_source_unreadable',
                             f'CODEX_HOME from the scope source does not exist: {home}')
        return home, 'scope_env'
    home = Path.home() / '.codex'
    if home.is_dir():
        return home, 'user_default'
    return None, 'user_default'


def read_codex_config(codex_home: Path) -> dict:
    file = codex_home / 'config.toml'
    if not file.is_file():
        return {}
    try:
        with file.open('rb') as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        raise AgentError('inheritance_source_unreadable', f'Cannot read {file}')


def _skill_name(skill_md: Path) -> str | None:
    """Minimal frontmatter name parse for conflict detection; Pi parses the full file."""
    try:
        head = skill_md.read_text(encoding='utf-8', errors='replace')[:4096]
    except OSError:
        return None
    match = re.match(r'\ufeff?---\s*\n(.*?)\n---', head, re.DOTALL)
    if not match:
        return None
    m = re.search(r'^name:\s*["\']?([^"\'\n]+?)["\']?\s*$', match.group(1), re.MULTILINE)
    return m.group(1).strip() if m else None


def _disabled_skill_paths(codex_home: Path, raw: dict) -> set[Path]:
    disabled: set[Path] = set()
    for entry in raw.get('skills', {}).get('config', []) if isinstance(raw.get('skills'), dict) else []:
        if not isinstance(entry, dict) or entry.get('enabled') is not False:
            continue
        path = entry.get('path')
        if isinstance(path, str) and path.strip():
            p = Path(path).expanduser()
            disabled.add(p.resolve() if p.is_absolute() else (codex_home / p).resolve())
    return disabled


def collect_skills(codex_home: Path, raw: dict, project_cwd: str | None,
                   existing_skill_paths: list[str]) -> tuple[list[str], list[Diagnostic]]:
    """Resolve managed-child skill sources, original paths only.

    Sources: existing profile paths (kept as-is), project .agents/skills under the
    scope cwd, and <codex_home>/skills. Dedup by real path; name conflicts refuse
    ambiguity; disabled entries from [[skills.config]] are honored.
    """
    diagnostics: list[Diagnostic] = []
    selected: list[str] = []
    by_real: dict[Path, str] = {}
    by_name: dict[str, str] = {}
    plugin_root = Path(__file__).resolve().parent.parent
    disabled = _disabled_skill_paths(codex_home, raw)

    for raw_path in existing_skill_paths:
        selected.append(raw_path)
        real = Path(raw_path).resolve()
        by_real[real] = raw_path
        name = _skill_name(real / 'SKILL.md') if real.is_dir() else None
        if name:
            by_name.setdefault(name, raw_path)

    sources: list[tuple[str, Path]] = []
    if project_cwd:
        agents = Path(project_cwd) / '.agents' / 'skills'
        if agents.is_dir():
            sources.append(('project', agents))
    codex_skills = codex_home / 'skills'
    if codex_skills.is_dir():
        sources.append(('codex_global', codex_skills))
    else:
        diagnostics.append(Diagnostic('skills', codex_home / 'skills', 'codex global skills directory not present; treated as empty'))

    count = 0
    for source, directory in sources:
        try:
            entries = sorted(p for p in directory.iterdir() if not p.name.startswith('.'))
        except OSError as exc:
            diagnostics.append(Diagnostic('skills', directory, f'not readable: {type(exc).__name__}'))
            continue
        for entry in entries:
            skill_md = entry / 'SKILL.md'
            if not skill_md.is_file():
                continue
            real = entry.resolve()
            if real in by_real:
                continue  # same real path already provided by project/profile source
            label = entry.name
            if (entry / 'agents').is_dir():
                diagnostics.append(Diagnostic('skills', label,
                                              'codex-specific policy metadata present (agents/); it is not interpreted or enforced by Pi'))
            if real in disabled or skill_md.resolve() in disabled:
                diagnostics.append(Diagnostic('skills', label, 'disabled by codex skills.config'))
                continue
            if real.is_relative_to(plugin_root):
                diagnostics.append(Diagnostic('skills', label, 'excluded: subagent-pi management skill (recursion guard)'))
                continue
            name = _skill_name(skill_md)
            if name and name in MANAGEMENT_SKILL_NAMES:
                diagnostics.append(Diagnostic('skills', label, 'excluded: subagent-pi management skill (recursion guard)'))
                continue
            if name and name in by_name:
                diagnostics.append(Diagnostic('skills', label,
                                              f'name conflict with {by_name[name]}; refusing ambiguous skill name {name!r}'))
                continue
            if count >= MAX_MANAGED_SKILLS:
                diagnostics.append(Diagnostic('skills', label, 'skill limit exceeded; remaining codex skills skipped'))
                continue
            selected.append(str(real))
            by_real[real] = str(real)
            if name:
                by_name[name] = str(real)
            count += 1
    return selected, diagnostics


def _tool_policy(server: dict, name: str) -> tuple[dict, list[Diagnostic]]:
    """Effective policy model for one server.

    Returns (policy, diagnostics); policy = {'default': 'auto'|'confirm',
    'tools': {name: 'auto'|'confirm'}, 'denied': sorted deny list}. For each
    tool the effective mode is the per-tool approval_mode override, else the
    server default, else 'prompt'. Values this bridge cannot enforce
    ('writes', unknown strings) degrade to 'confirm' with a named diagnostic;
    a per-tool 'auto' can never lift a child-side mandatory confirmation (the
    child rule is applied separately and wins). Tool config keys this plugin
    cannot honor (for example output_token_limit) deny that tool outright
    instead of being silently ignored.
    """
    diagnostics: list[Diagnostic] = []
    mode = server.get('default_tools_approval_mode', 'prompt')
    if mode not in APPROVAL_MODES:
        diagnostics.append(Diagnostic('mcp', name, f'unknown default_tools_approval_mode {mode!r}; using confirm'))
        mode = 'prompt'
    if mode == 'writes':
        diagnostics.append(Diagnostic('mcp', name,
                                      'approval_mode writes cannot be enforced without trusting readOnlyHint; using confirm'))
    default = 'auto' if mode == 'auto' else 'confirm'
    tools: dict[str, str] = {}
    denied: set[str] = set(server.get('disabled_tools') or [])
    for tool_name, tool_cfg in (server.get('tools') or {}).items():
        if not isinstance(tool_cfg, dict):
            continue
        unknown = set(tool_cfg) - {'approval_mode'}
        if unknown:
            # Authorization/output limits we do not implement must not be
            # accepted-and-ignored: the tool is denied with a reason instead.
            denied.add(tool_name)
            diagnostics.append(Diagnostic('mcp', f'{name}.{tool_name}',
                                          f'denied: unsupported tool config keys cannot be honored: {sorted(unknown)}'))
            continue
        tmode = tool_cfg.get('approval_mode')
        if tmode is None:
            continue
        if tmode not in APPROVAL_MODES:
            denied.add(tool_name)
            diagnostics.append(Diagnostic('mcp', f'{name}.{tool_name}',
                                          f'denied: unknown approval_mode {tmode!r}'))
            continue
        if tmode == 'writes':
            diagnostics.append(Diagnostic('mcp', f'{name}.{tool_name}',
                                          'approval_mode writes cannot be enforced; using confirm'))
        tools[tool_name] = 'auto' if tmode == 'auto' else 'confirm'
    return {'default': default, 'tools': tools, 'denied': sorted(denied)}, diagnostics


def _int_field(server: dict, key: str, default: int) -> tuple[int, list[Diagnostic]]:
    value = server.get(key, default)
    diagnostics: list[Diagnostic] = []
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0 or value > 3600:
        diagnostics.append(Diagnostic('mcp', server.get('name', '?'), f'invalid {key}; using {default}s'))
        return default, diagnostics
    return value, diagnostics


def _resolve_self_paths(command: str, args: list[str], cwd: str | None,
                        codex_home: Path) -> list[Path]:
    """All paths this server's execution definition could resolve to.

    Covers the command itself, absolute/relative entry-script args (including
    the installer-generated `python <...>/bin/subagent-pi mcp` wrapper) and
    `python -m subagent_pi` module forms. No configured command is executed.
    """
    candidates: list[Path] = []
    resolved_command = shutil.which(command) if not Path(command).is_absolute() else None
    raw = Path(resolved_command or command).expanduser()
    candidates.append(raw)
    anchors = [Path(cwd) if cwd else None, codex_home]
    for arg in args or []:
        p = Path(arg).expanduser()
        if p.is_absolute():
            candidates.append(p)
        elif arg not in ('-m', '-c', '-I', '-S', '-E', '-s') and not arg.startswith('-'):
            for anchor in anchors:
                if anchor is not None:
                    candidates.append(anchor / p)
    out: list[Path] = []
    for p in candidates:
        try:
            out.append(p.resolve())
        except OSError:
            out.append(p)
    return out


def _is_self_server(entry: dict, codex_home: Path) -> bool:
    plugin_bin = Path(__file__).resolve().parent.parent / 'bin' / 'subagent-pi'
    plugin_bin_real = plugin_bin.resolve() if plugin_bin.exists() else None
    args = entry.get('args') or []
    for idx, arg in enumerate(args):
        if arg == '-m' and idx + 1 < len(args) and args[idx + 1] in ('subagent_pi', 'subagent-pi'):
            return True
    if plugin_bin_real is None:
        return False
    for resolved in _resolve_self_paths(entry.get('command', ''), args, entry.get('cwd'), codex_home):
        if resolved == plugin_bin_real:
            return True
    return False


def _enabled_tools(server: dict) -> list[str] | None:
    """Validate enabled_tools; an explicitly empty list allows no tools."""
    enabled = server.get('enabled_tools')
    if enabled is None:
        return None
    if not isinstance(enabled, list) or any(not isinstance(t, str) for t in enabled):
        raise AgentError('invalid_argument', 'enabled_tools must be a list of tool names')
    return sorted(set(enabled))

def parse_mcp_servers(codex_home: Path, raw: dict) -> tuple[list[dict], list[Diagnostic]]:
    """Convert [mcp_servers.*] TOML into normalized in-memory server configs.

    Values are NOT resolved here (no environment access). Every declared server
    keeps a disposition ('ok' | 'failed' | 'disabled') and, for failed ones, its
    reasons — a failed required server must not silently vanish. Unknown keys
    that affect execution or authorization mark the server failed with an
    explicit reason; the rest of the config continues.
    """
    diagnostics: list[Diagnostic] = []
    servers: list[dict] = []
    table = raw.get('mcp_servers')
    if table is None:
        return [], diagnostics
    if not isinstance(table, dict):
        diagnostics.append(Diagnostic('mcp', 'mcp_servers', 'not a table; ignored'))
        return [], diagnostics

    def _failed(name, transport, required, reason):
        servers.append({'name': name, 'transport': transport, 'required': required,
                        'disposition': 'failed', 'reasons': [reason]})
        diagnostics.append(Diagnostic('mcp', name, f'disabled: {reason}'))

    for name, server in table.items():
        transport = 'http' if isinstance(server, dict) and 'url' in server else 'stdio'
        required = bool(server.get('required', False)) if isinstance(server, dict) else False
        if not isinstance(server, dict):
            _failed(name, transport, required, 'server entry is not a table')
            continue
        if server.get('enabled') is False:
            servers.append({'name': name, 'transport': transport, 'required': False,
                            'disposition': 'disabled', 'reasons': ['disabled in codex config']})
            diagnostics.append(Diagnostic('mcp', name, 'disabled in codex config'))
            continue
        if len([s for s in servers if s['disposition'] == 'ok']) >= MAX_MCP_SERVERS:
            _failed(name, transport, required, 'server limit exceeded')
            continue
        entry: dict = {'name': name, 'required': required, 'disposition': 'ok', 'reasons': []}
        is_http = 'url' in server
        allowed_keys = MCP_HTTP_KEYS if is_http else MCP_STDIO_KEYS
        unknown = set(server) - allowed_keys
        if unknown:
            _failed(name, transport, required,
                    f'unsupported config keys affecting execution or auth: {sorted(unknown)}')
            continue
        try:
            policy, pdiag = _tool_policy(server, name)
            entry.update(allowed_tools=_enabled_tools(server),
                         disabled_tools=policy['denied'],
                         approval_default=policy['default'],
                         tool_approval=policy['tools'])
            diagnostics.extend(pdiag)
            timeout, tdiag = _int_field(server, 'startup_timeout_sec', 10)
            tool_timeout, ttdiag = _int_field(server, 'tool_timeout_sec', 60)
            entry['startup_timeout_sec'] = timeout
            entry['tool_timeout_sec'] = tool_timeout
            diagnostics.extend(tdiag + ttdiag)
            if is_http:
                entry['transport'] = 'http'
                url = server['url']
                if not isinstance(url, str) or not url.startswith(('http://', 'https://')):
                    raise AgentError('invalid_argument', 'url must be http(s)')
                entry['url'] = url
                if 'auth' in server and server['auth'] != 'bearer':
                    raise AgentError('invalid_argument', f"auth={server['auth']!r} (oauth/chatgpt) is not supported in managed children")
                if 'http_headers_helper' in server:
                    raise AgentError('invalid_argument', 'http_headers_helper is not supported in managed children')
                headers = server.get('http_headers') or {}
                env_headers = server.get('env_http_headers') or {}
                if not isinstance(headers, dict) or not isinstance(env_headers, dict) or \
                   any(not isinstance(k, str) or not isinstance(v, str) for k, v in {**headers, **env_headers}.items()):
                    raise AgentError('invalid_argument', 'http_headers/env_http_headers must map strings to strings')
                entry['static_headers'] = dict(headers)
                entry['env_header_names'] = dict(env_headers)
                entry['bearer_token_env_var'] = server.get('bearer_token_env_var') if isinstance(server.get('bearer_token_env_var'), str) else None
            else:
                entry['transport'] = 'stdio'
                if server.get('experimental_environment') not in (None, 'local'):
                    raise AgentError('invalid_argument', 'experimental_environment remote executor is not supported in managed children')
                command = server.get('command')
                if not isinstance(command, str) or not command.strip():
                    raise AgentError('invalid_argument', 'missing command')
                args = server.get('args', [])
                if not isinstance(args, list) or any(not isinstance(a, str) for a in args):
                    raise AgentError('invalid_argument', 'args must be a list of strings')
                env_static = server.get('env') or {}
                env_refs = server.get('env_vars') or []
                if not isinstance(env_static, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in env_static.items()):
                    raise AgentError('invalid_argument', 'env must map strings to strings')
                refs: list[str] = []
                for ref in env_refs:
                    if isinstance(ref, str):
                        refs.append(ref)
                    elif isinstance(ref, dict) and isinstance(ref.get('name'), str) and ref.get('source', 'local') in (None, 'local'):
                        refs.append(ref['name'])
                    elif isinstance(ref, dict) and ref.get('source') == 'remote':
                        raise AgentError('invalid_argument', f"env_vars source=remote ({ref.get('name')}) requires remote executor")
                    else:
                        raise AgentError('invalid_argument', 'env_vars entries must be names')
                cwd = server.get('cwd')
                if cwd is not None:
                    if not isinstance(cwd, str) or not cwd.strip():
                        raise AgentError('invalid_argument', 'cwd must be a path string')
                    cwd_path = Path(cwd).expanduser()
                    if not cwd_path.is_absolute():
                        # Codex does not document relative-cwd resolution; anchor it to the
                        # config source directory and record the decision.
                        cwd_path = (codex_home / cwd_path).resolve()
                        diagnostics.append(Diagnostic('mcp', name, f'relative cwd anchored to codex home: {cwd_path}'))
                    cwd = str(cwd_path)
                entry.update(command=command, args=args, static_env=dict(env_static),
                             env_var_names=sorted(set(refs)), cwd=cwd)
                if _is_self_server(entry, codex_home):
                    # Recursion guard by execution definition: covers the direct
                    # entrypoint, symlinks, the installer's python+script wrapper
                    # and `-m subagent_pi` forms. Renaming the server evades nothing.
                    raise AgentError('invalid_argument', 'subagent-pi management server (recursion guard)')
            servers.append(entry)
        except AgentError as exc:
            servers.append({'name': name, 'transport': transport, 'required': required,
                            'disposition': 'failed', 'reasons': [exc.message]})
            diagnostics.append(Diagnostic('mcp', name, f'disabled: {exc.message}'))
    return servers, diagnostics


def resolve_environment(servers: list[dict], env_snapshot: dict) -> tuple[list[dict], list[Diagnostic]]:
    """Fill referenced env values from the bound scope snapshot, in memory only.

    Missing values mark the server disposition='failed' with named reasons —
    required servers are never silently dropped. After collecting every
    failure, required servers abort with inheritance_required_server_failed so
    a spawn/respawn cannot start a task with a missing dependency.
    """
    diagnostics: list[Diagnostic] = []
    required_failures: list[str] = []
    for server in servers:
        if server.get('disposition') != 'ok':
            continue
        problems: list[str] = []
        out = {k: v for k, v in server.items() if k not in ('env_var_names', 'env_header_names', 'static_env', 'static_headers')}
        out['env'] = dict(server.get('static_env', {}))
        if server['transport'] == 'stdio':
            for var in server.get('env_var_names', []):
                value = env_snapshot.get(var)
                if value is None:
                    problems.append(f'missing env var {var} (scope source)')
                else:
                    out['env'][var] = value
        else:
            headers = dict(server.get('static_headers', {}))
            for header, var in server.get('env_header_names', {}).items():
                value = env_snapshot.get(var)
                if value is None:
                    problems.append(f'missing env var {var} (scope source, header {header})')
                else:
                    headers[header] = value
            out['headers'] = headers
            bearer = server.get('bearer_token_env_var')
            if bearer:
                value = env_snapshot.get(bearer)
                if value is None:
                    problems.append(f'missing env var {bearer} (scope source, bearer token)')
                else:
                    out['bearer_token'] = value
        if problems:
            server['disposition'] = 'failed'
            server['reasons'] = problems
            for p in problems:
                diagnostics.append(Diagnostic('mcp', server['name'], p))
            if server.get('required'):
                required_failures.append(server['name'])
            else:
                diagnostics.append(Diagnostic('mcp', server['name'], 'excluded: environment unavailable in this daemon generation'))
            continue
        server.update(out)
    if required_failures:
        raise AgentError('inheritance_required_server_failed',
                         'required MCP server(s) cannot start: ' + ', '.join(sorted(required_failures)))
    return servers, diagnostics


def policy_filter(servers: list[dict], access: str) -> tuple[list[dict], list[Diagnostic]]:
    """Child-limit intersection for read children.

    The parent's enabled_tools declares what children may use at all; it is not
    a read-safety endorsement of each tool. A read child WITH an explicit
    allowlist may call exactly those tools, subject to the inherited approval
    policy. A read child WITHOUT an allowlist sees only readOnly-advertised
    tools and every call confirms (confirm_all) — parent-side 'auto' can never
    relax this child rule, and readOnlyHint is a server self-report, not a
    trusted capability, so it only affects visibility.
    """
    diagnostics: list[Diagnostic] = []
    if access == 'write':
        return servers, diagnostics
    for server in servers:
        if server.get('disposition') != 'ok':
            continue
        if server.get('allowed_tools') is None:
            server['confirm_all'] = True
            diagnostics.append(Diagnostic('mcp', server['name'],
                                          'read child: server has no explicit enabled_tools allowlist; only readOnly tools are visible and every call confirms'))
    return servers, diagnostics


def capture_scope_env(codex_home: Path | None, environ: dict,
                      extra_names: list[str] | tuple[str, ...] = ()) -> dict:
    """Client-side snapshot: base keys plus every var the current config references
    plus explicitly authorized child-env names (inheritance.child_env)."""
    names = set(BASE_ENV_KEYS) | {n for n in extra_names if isinstance(n, str) and n.strip()}
    if codex_home is not None:
        try:
            servers, _ = parse_mcp_servers(codex_home, read_codex_config(codex_home))
        except AgentError:
            servers = []
        names |= referenced_env_names(servers)
    snapshot: dict[str, str] = {}
    for name in sorted(names):
        value = environ.get(name)
        if isinstance(value, str) and len(value) <= MAX_ENV_VALUE:
            snapshot[name] = value
    return {k: v for k, v in list(snapshot.items())[:MAX_ENV_VARS]}


def referenced_env_names(servers: list[dict]) -> set[str]:
    """Env var NAMES a server list references (headers' values, bearer var, args).
    Includes the base keys so callers can treat one set as the full allowlist."""
    names: set[str] = set(BASE_ENV_KEYS)
    for server in servers:
        names.update(server.get('env_var_names', []))
        names.update((server.get('env_header_names') or {}).values())
        if server.get('bearer_token_env_var'):
            names.add(server['bearer_token_env_var'])
    return names


def scope_source_snapshot(home: Path, environ: dict) -> dict:
    """Trusted client-side snapshot for scope binding: codex home resolution plus
    the minimal env capture (base keys, referenced vars, authorized child-env
    names). Called by the CLI launcher and the Codex-spawned MCP adapter; the
    values live in daemon memory only and are never model-visible."""
    from .config import load_config  # local import: config owns the state home layout
    cfg = load_config(home)
    codex_home, _ = resolve_codex_home(cfg['inheritance'], {'CODEX_HOME': environ.get('CODEX_HOME')})
    return {'env': capture_scope_env(codex_home, environ,
                                     extra_names=cfg['inheritance'].get('child_env', []))}
