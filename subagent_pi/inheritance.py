"""Narrow Codex-source resolver: codex home, global skills, and in-memory MCP config.

Everything here reads the original files at resolution time and returns plain
in-memory structures. Nothing writes configuration, snapshots, or caches.
Secrets (env values) are resolved only in the daemon process and only for the
bound scope; diagnostics never contain values, only names and sources.
"""
from __future__ import annotations
import os
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
MCP_HARMLESS_KEYS = set()  # display-only keys we intentionally ignore; none identified in codex 0.154.0
APPROVAL_MODES = {'auto', 'prompt', 'writes', 'approve'}


def redact_url(url: str) -> str:
    """Strip query and fragment; they may carry credentials."""
    for sep in ('?', '#'):
        idx = url.find(sep)
        if idx >= 0:
            url = url[:idx]
    return url


class Diagnostic:
    def __init__(self, scope: str, name: str, reason: str):
        self.scope, self.name, self.reason = scope, name, reason
    def as_dict(self):
        return {'scope': self.scope, 'name': self.name, 'reason': self.reason}


def resolve_codex_home(config, source_env: dict | None) -> tuple[Path | None, str]:
    """Explicit trusted setting -> scope-bound CODEX_HOME -> ~/.codex.

    Accepts either the full plugin config or its [inheritance] table directly.
    Returns (home, mode); home is None when no candidate directory exists.
    """
    inh = config.get('inheritance', config) if isinstance(config, dict) else {}
    explicit = inh.get('codex_home') if isinstance(inh, dict) else None
    candidates: list[tuple[Path, str]] = []
    if explicit:
        candidates.append((Path(explicit).expanduser(), 'explicit'))
    if isinstance(source_env, dict) and isinstance(source_env.get('CODEX_HOME'), str) and source_env['CODEX_HOME'].strip():
        candidates.append((Path(source_env['CODEX_HOME']).expanduser(), 'scope_env'))
    candidates.append((Path.home() / '.codex', 'user_default'))
    for home, mode in candidates:
        if home.is_dir():
            return home, mode
    return None, candidates[0][1] if candidates else 'user_default'


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
    elif not any(s[0] == 'codex_global' for s in sources):
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


def _tool_policy(server: dict, table: dict) -> tuple[set[str] | None, set[str], str, list[Diagnostic]]:
    diagnostics: list[Diagnostic] = []
    enabled = server.get('enabled_tools')
    if enabled is not None:
        if not isinstance(enabled, list) or any(not isinstance(t, str) for t in enabled):
            raise AgentError('invalid_argument', 'enabled_tools must be a list of tool names')
        allowed = set(enabled)
    else:
        allowed = None
    disabled = server.get('disabled_tools') or []
    if not isinstance(disabled, list) or any(not isinstance(t, str) for t in disabled):
        raise AgentError('invalid_argument', 'disabled_tools must be a list of tool names')
    denied = set(disabled)
    mode = server.get('default_tools_approval_mode', 'prompt')
    if mode not in APPROVAL_MODES:
        diagnostics.append(Diagnostic('mcp', server.get('name', '?'), f'unknown default_tools_approval_mode {mode!r}; using confirm'))
        mode = 'prompt'
    if mode == 'writes':
        diagnostics.append(Diagnostic('mcp', server.get('name', '?'),
                                      'approval_mode writes cannot be enforced without trusting readOnlyHint; using confirm'))
        mode = 'prompt'
    for tool_name, tool_cfg in (server.get('tools') or {}).items():
        if not isinstance(tool_cfg, dict):
            continue
        unknown = set(tool_cfg) - {'approval_mode', 'output_token_limit'}
        if unknown:
            diagnostics.append(Diagnostic('mcp', f"{server.get('name', '?')}.{tool_name}",
                                          f'unsupported tool config keys ignored: {sorted(unknown)}'))
            continue
        tmode = tool_cfg.get('approval_mode')
        if tmode == 'auto':
            mode = mode  # per-tool auto handled in bridge via auto_tools set
    auto_tools = {t for t, cfg in (server.get('tools') or {}).items()
                  if isinstance(cfg, dict) and cfg.get('approval_mode') == 'auto'}
    return allowed, denied, mode, diagnostics + [Diagnostic('mcp', '__auto_tools__', ','.join(sorted(auto_tools)))]


def _int_field(server: dict, key: str, default: int) -> tuple[int, list[Diagnostic]]:
    value = server.get(key, default)
    diagnostics: list[Diagnostic] = []
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0 or value > 3600:
        diagnostics.append(Diagnostic('mcp', server.get('name', '?'), f'invalid {key}; using {default}s'))
        return default, diagnostics
    return value, diagnostics


def parse_mcp_servers(codex_home: Path, raw: dict) -> tuple[list[dict], list[Diagnostic]]:
    """Convert [mcp_servers.*] TOML into normalized in-memory server configs.

    Values are NOT resolved here (no environment access). Unknown critical keys
    disable the server with an explicit reason; the rest of the config continues.
    """
    diagnostics: list[Diagnostic] = []
    servers: list[dict] = []
    table = raw.get('mcp_servers')
    if table is None:
        return [], diagnostics
    if not isinstance(table, dict):
        diagnostics.append(Diagnostic('mcp', 'mcp_servers', 'not a table; ignored'))
        return [], diagnostics
    plugin_bin = Path(__file__).resolve().parent.parent / 'bin' / 'subagent-pi'
    plugin_bin_real = plugin_bin.resolve() if plugin_bin.exists() else None
    for name, server in table.items():
        if not isinstance(server, dict):
            diagnostics.append(Diagnostic('mcp', name, 'server entry is not a table; skipped'))
            continue
        if server.get('enabled') is False:
            diagnostics.append(Diagnostic('mcp', name, 'disabled in codex config'))
            continue
        if len(servers) >= MAX_MCP_SERVERS:
            diagnostics.append(Diagnostic('mcp', name, 'server limit exceeded; remaining servers skipped'))
            continue
        is_http = 'url' in server
        allowed_keys = MCP_HTTP_KEYS if is_http else MCP_STDIO_KEYS
        unknown = set(server) - allowed_keys - MCP_HARMLESS_KEYS
        critical = {k for k in unknown if k not in MCP_HARMLESS_KEYS}
        if critical:
            diagnostics.append(Diagnostic('mcp', name, f'disabled: unsupported config keys affecting execution or auth: {sorted(critical)}'))
            continue
        entry: dict = {'name': name, 'required': bool(server.get('required', False))}
        try:
            allowed, denied, approval, extra = _tool_policy(server, server)
            entry.update(allowed_tools=sorted(allowed) if allowed is not None else None,
                         disabled_tools=sorted(denied))
            auto = extra[-1].name and extra[-1].reason
            entry['auto_approval_tools'] = [t for t in (auto or '').split(',') if t]
            entry['approval_mode'] = approval
            entry['diagnostics'] = [d.as_dict() for d in extra[:-1]]
            timeout, tdiag = _int_field(server, 'startup_timeout_sec', 10)
            tool_timeout, ttdiag = _int_field(server, 'tool_timeout_sec', 60)
            entry['startup_timeout_sec'] = timeout
            entry['tool_timeout_sec'] = tool_timeout
            diagnostics.extend(extra[:-1] + tdiag + ttdiag)
            if is_http:
                entry['transport'] = 'http'
                url = server['url']
                if not isinstance(url, str) or not url.startswith(('http://', 'https://')):
                    diagnostics.append(Diagnostic('mcp', name, 'disabled: url must be http(s)'))
                    continue
                entry['url'] = url
                if 'auth' in server and server['auth'] != 'bearer':
                    diagnostics.append(Diagnostic('mcp', name, f"disabled: auth={server['auth']!r} (oauth/chatgpt) is not supported in managed children"))
                    continue
                if 'http_headers_helper' in server:
                    diagnostics.append(Diagnostic('mcp', name, 'disabled: http_headers_helper is not supported in managed children'))
                    continue
                headers = server.get('http_headers') or {}
                env_headers = server.get('env_http_headers') or {}
                if not isinstance(headers, dict) or not isinstance(env_headers, dict) or \
                   any(not isinstance(k, str) or not isinstance(v, str) for k, v in {**headers, **env_headers}.items()):
                    diagnostics.append(Diagnostic('mcp', name, 'disabled: http_headers/env_http_headers must map strings to strings'))
                    continue
                entry['static_headers'] = dict(headers)
                entry['env_header_names'] = dict(env_headers)
                entry['bearer_token_env_var'] = server.get('bearer_token_env_var') if isinstance(server.get('bearer_token_env_var'), str) else None
                servers.append(entry)
            else:
                entry['transport'] = 'stdio'
                if server.get('experimental_environment') not in (None, 'local'):
                    diagnostics.append(Diagnostic('mcp', name, 'disabled: experimental_environment remote executor is not supported in managed children'))
                    continue
                command = server.get('command')
                if not isinstance(command, str) or not command.strip():
                    diagnostics.append(Diagnostic('mcp', name, 'disabled: missing command'))
                    continue
                args = server.get('args', [])
                if not isinstance(args, list) or any(not isinstance(a, str) for a in args):
                    diagnostics.append(Diagnostic('mcp', name, 'disabled: args must be a list of strings'))
                    continue
                env_static = server.get('env') or {}
                env_refs = server.get('env_vars') or []
                if not isinstance(env_static, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in env_static.items()):
                    diagnostics.append(Diagnostic('mcp', name, 'disabled: env must map strings to strings'))
                    continue
                refs: list[str] = []
                broken = False
                for ref in env_refs:
                    if isinstance(ref, str):
                        refs.append(ref)
                    elif isinstance(ref, dict) and isinstance(ref.get('name'), str) and ref.get('source', 'local') in (None, 'local'):
                        refs.append(ref['name'])
                    elif isinstance(ref, dict) and ref.get('source') == 'remote':
                        diagnostics.append(Diagnostic('mcp', name, f"disabled: env_vars source=remote ({ref['name']}) requires remote executor"))
                        broken = True
                        break
                    else:
                        diagnostics.append(Diagnostic('mcp', name, 'disabled: env_vars entries must be names'))
                        broken = True
                        break
                if broken:
                    continue
                cwd = server.get('cwd')
                if cwd is not None:
                    if not isinstance(cwd, str) or not cwd.strip():
                        diagnostics.append(Diagnostic('mcp', name, 'disabled: cwd must be a path string'))
                        continue
                    cwd_path = Path(cwd).expanduser()
                    if not cwd_path.is_absolute():
                        # Codex does not document relative-cwd resolution; anchor it to the
                        # config source directory and record the decision.
                        cwd_path = (codex_home / cwd_path).resolve()
                        diagnostics.append(Diagnostic('mcp', name, f'relative cwd anchored to codex home: {cwd_path}'))
                    cwd = str(cwd_path)
                resolved = command if Path(command).is_absolute() else (shutil.which(command) or command)
                try:
                    real_command = Path(resolved).resolve()
                except OSError:
                    real_command = Path(resolved)
                if plugin_bin_real and real_command == plugin_bin_real:
                    diagnostics.append(Diagnostic('mcp', name, 'excluded: subagent-pi management server (recursion guard)'))
                    continue
                entry.update(command=command, args=args, static_env=dict(env_static),
                             env_var_names=sorted(set(refs)), cwd=cwd)
                servers.append(entry)
        except AgentError as exc:
            diagnostics.append(Diagnostic('mcp', name, f'disabled: {exc.message}'))
    return servers, diagnostics


def resolve_environment(servers: list[dict], env_snapshot: dict) -> tuple[list[dict], list[Diagnostic]]:
    """Fill referenced env values from the bound scope snapshot, in memory only.

    Returns payload-ready servers plus diagnostics. Missing values are named,
    never guessed from other scopes or from the daemon environment.
    """
    diagnostics: list[Diagnostic] = []
    usable: list[dict] = []
    for server in servers:
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
            for p in problems:
                diagnostics.append(Diagnostic('mcp', server['name'], p))
            if server.get('required'):
                raise AgentError('inheritance_required_server_failed',
                                 f"required MCP server {server['name']!r} cannot start: {'; '.join(problems)}")
            diagnostics.append(Diagnostic('mcp', server['name'], 'excluded: environment unavailable in this daemon generation'))
            continue
        usable.append(out)
    return usable, diagnostics


def policy_filter(servers: list[dict], access: str) -> tuple[list[dict], list[Diagnostic]]:
    """Child-limit intersection. Read children only get explicitly allow-listed
    tools without approval, or readOnlyHint tools behind per-call confirmation."""
    diagnostics: list[Diagnostic] = []
    if access == 'write':
        return servers, diagnostics
    usable = []
    for server in servers:
        if server.get('allowed_tools') is not None:
            usable.append(server)
        else:
            diagnostics.append(Diagnostic('mcp', server['name'],
                                          'read child: server has no explicit enabled_tools allowlist; tools require per-call confirmation'))
            usable.append({**server, 'confirm_all': True})
    return usable, diagnostics


def capture_scope_env(codex_home: Path | None, environ: dict) -> dict:
    """Client-side snapshot: base keys plus every var the current config references."""
    names = set(BASE_ENV_KEYS)
    if codex_home is not None:
        try:
            raw = read_codex_config(codex_home)
            servers, _ = parse_mcp_servers(codex_home, raw)
        except AgentError:
            servers = []
        for server in servers:
            names.update(server.get('env_var_names', []))
            names.update(server.get('env_header_names', {}).values())
            if server.get('bearer_token_env_var'):
                names.add(server['bearer_token_env_var'])
    snapshot: dict[str, str] = {}
    for name in sorted(names):
        value = environ.get(name)
        if isinstance(value, str) and len(value) <= MAX_ENV_VALUE:
            snapshot[name] = value
    return {k: v for k, v in list(snapshot.items())[:MAX_ENV_VARS]}


def referenced_env_names(servers: list[dict]) -> set[str]:
    names: set[str] = set(BASE_ENV_KEYS)
    for server in servers:
        names.update(server.get('env_var_names', []))
        names.update((server.get('env_header_names') or {}).values())
        if server.get('bearer_token_env_var'):
            names.add(server['bearer_token_env_var'])
    return names
