"""Scope binding: child environment, Codex source pointer and the inheritance plan
rebuilt at every boot. Values resolve here; only the base keys are persisted."""
from __future__ import annotations
import json
from pathlib import Path

from .common import AgentError, BASE_ENV_KEYS, dumps, text
from .inheritance import (CODEX_MCP_BASELINE, SERVER_POLICY_KEYS, SERVER_RESOLVED_KEYS, Diagnostic,
    collect_skills, parse_mcp_servers, policy_filter, read_codex_config, referenced_env_names,
    resolve_codex_home, resolve_environment)

def child_env(rt, sid, spec):
    """Base environment for the guard/Pi child, built from the scope's bound
    snapshot — never a copy of the daemon's environ. Env-based model auth
    requires explicitly configured names (inheritance.child_env); profile env
    values come from the current config, never from the persisted copy.
    A daemon restart drops the in-memory snapshot, so the non-secret base keys
    reload from the ledger; other bound values stay gone until a rebind."""
    snapshot = {**persisted_base_env(rt, sid), **rt.scope_env.get(sid, {})}
    inh = rt.config['inheritance']
    allowed = set(BASE_ENV_KEYS) | {k for k in inh.get('child_env', []) if isinstance(k, str)}
    env = {k: v for k, v in snapshot.items() if k in allowed and isinstance(v, str)}
    profile = rt.config['profiles'].get(spec.get('profile'), {}) if isinstance(rt.config['profiles'], dict) else {}
    penv = profile.get('env', {}) if isinstance(profile, dict) else {}
    if isinstance(penv, dict):
        env.update({k: v for k, v in penv.items() if isinstance(k, str) and isinstance(v, str)})
    env['PI_AGENTS_MANAGED_CHILD'] = '1'
    return env

def persisted_base_env(rt, sid):
    """Base keys (PATH/HOME/...) survive a restart because they are not
    secrets; they are stored per scope so a respawned worker can still find
    its interpreter. Everything else bound to the scope stays in memory."""
    row = rt.store.one('SELECT base_env FROM scopes WHERE id=?', (sid,))
    if not row or not row['base_env']:
        return {}
    try:
        value = json.loads(row['base_env'])
    except ValueError:
        return {}
    if not isinstance(value, dict):
        return {}
    return {k: v for k, v in value.items() if k in BASE_ENV_KEYS and isinstance(v, str)}

def remember_base_env(rt, sid, snapshot):
    base = {k: v for k, v in snapshot.items() if k in BASE_ENV_KEYS and isinstance(v, str)}
    rt.store.execute('UPDATE scopes SET base_env=? WHERE id=?', (dumps(base) if base else None, sid))

def bind_scope_source(rt, sid, p, source):
    """Bind a scope in two independent layers: layer 1 (always) the worker base
    environment from the opening client plus authorized child_env names — a
    child must find its interpreter whether or not Codex inheritance is on;
    layer 2 (master switch on) the Codex source pointer. Secrets stay in
    memory in both layers."""
    inh=rt.config['inheritance']
    scope=rt.store.scope(sid)
    env = source.get('env') if isinstance(source,dict) else None
    master_enabled = bool(inh.get('enabled',True))
    explicit_home = p.get('codex_home')
    if explicit_home is not None:
        explicit_home = str(Path(text(explicit_home,'codex_home',4096)).expanduser().resolve())
        if not Path(explicit_home).is_dir(): raise AgentError('invalid_cwd','codex_home must be an existing directory')
    home=None; mode=None
    if master_enabled:
        home,mode = resolve_codex_home({**inh,'codex_home':explicit_home or inh.get('codex_home')},env)
        stored=scope['codex_home']
        if stored and home and Path(stored)!=Path(home) and explicit_home is None and p.get('inheritance') is None:
            raise AgentError('inheritance_source_conflict',
                f'Scope is bound to codex source {stored}; rebind explicitly with codex_home or inheritance parameters')
    # A credential refresh must not silently flip the per-scope switch.
    enabled=bool(scope['inheritance'])
    if p.get('inheritance') is False: enabled=False
    elif p.get('inheritance') is True: enabled=True
    # Layer-2 fields stay untouched while the master switch is off, so
    # re-enabling later does not find them clobbered by a disabled-era rebind.
    rt.store.execute('UPDATE scopes SET codex_home=?,codex_source=?,inheritance=? WHERE id=?',
        ((str(home) if home else scope['codex_home']) if master_enabled else scope['codex_home'],
         (mode if home else scope['codex_source']) if master_enabled else scope['codex_source'],
         1 if enabled else 0, sid))
    if env is not None:
        names=set(BASE_ENV_KEYS) | {k for k in inh.get('child_env',[]) if isinstance(k,str)}
        if master_enabled and home is not None:
            try:
                servers,_=parse_mcp_servers(home,read_codex_config(home))
                names |= referenced_env_names(servers)
            except AgentError:
                pass
        # Minimal per-scope snapshot: referenced names only. Secrets stay in
        # memory; only the non-secret base keys are persisted for restarts.
        rt.scope_env[sid]={k:v for k,v in env.items() if k in names}
        remember_base_env(rt, sid, rt.scope_env[sid])

def inheritance_plan(rt, a, spec, generation):
    """Recompute managed-child inheritance from the original sources at boot.
    The persisted launch argv is never touched; secrets resolve into the pipe
    payload only, diagnostics carry names, never values."""
    inh = rt.config['inheritance']
    empty = {'argv': [], 'payload': None, 'diagnostics': [], 'bridge': False, 'servers': [], 'skills': []}
    if not inh.get('enabled'):
        return {**empty, 'reason': 'inheritance disabled by config'}
    scope = rt.store.scope(a['scope'])
    if not scope['inheritance']:
        return {**empty, 'reason': 'inheritance disabled for this scope'}
    source_env = rt.scope_env.get(a['scope'])
    if scope['codex_home']:
        codex_home, mode = Path(scope['codex_home']), scope['codex_source'] or 'scope_env'
    elif source_env and isinstance(source_env.get('CODEX_HOME'), str) and source_env['CODEX_HOME'].strip():
        codex_home, mode = resolve_codex_home(rt.config['inheritance'], source_env)
    elif scope['codex_source']:
        # A restart wiped the snapshot: never fall back to another Codex home
        # (e.g. the daemon user's ~/.codex); demand an explicit rebind.
        raise AgentError('inheritance_source_unbound',
                         'Scope lost its codex source binding after a daemon restart; the owning client must re-open the scope')
    else:
        codex_home, mode = resolve_codex_home(rt.config['inheritance'], source_env)
    if codex_home is None:
        diag = Diagnostic('source', 'codex_home', 'no codex source directory found').as_dict()
        return {**empty, 'diagnostics': [diag], 'reason': 'no source'}
    raw = read_codex_config(codex_home)
    existing_skills = [spec['argv'][i + 1] for i, flag in enumerate(spec['argv']) if flag == '--skill']
    skill_paths, skill_diag = [], []
    if inh.get('skills', True):
        skill_paths, skill_diag = collect_skills(codex_home, raw, a['cwd'], existing_skills)
    servers, mcp_diag = [], []
    if inh.get('mcp', True):
        servers, mcp_diag = parse_mcp_servers(codex_home, raw, inh.get('mcp_protocol_mode', 'auto'))
        servers, env_diag = resolve_environment(servers, source_env or {})
        mcp_diag += env_diag
        servers, access_diag = policy_filter(servers, spec['access'])
        mcp_diag += access_diag
    required_broken = [s['name'] for s in servers if s.get('required') and s.get('disposition') != 'ok']
    if required_broken:
        raise AgentError('inheritance_required_server_failed',
                         'required MCP server(s) cannot start: ' + ', '.join(sorted(required_broken)))
    usable = [s for s in servers if s.get('disposition') == 'ok']
    diagnostics = [d.as_dict() for d in skill_diag + mcp_diag]
    argv = []
    inherited_skills = [p for p in skill_paths if p not in existing_skills]
    for path in inherited_skills:
        argv += ['--skill', path]
    bridge_path = Path(__file__).resolve().parent.parent / 'extensions' / 'codex-mcp-bridge.ts'
    load_bridge = bool(inh.get('mcp', True) and usable and bridge_path.exists())
    source = {'codex_home': str(codex_home), 'mode': mode}
    payload = None
    if load_bridge:
        argv += ['--extension', str(bridge_path)]
        internal = (*SERVER_RESOLVED_KEYS, *SERVER_POLICY_KEYS)
        payload = {'v': 1,
                   'agent': {'id': a['id'], 'access': spec['access'], 'generation': generation},
                   'source': source,
                   'mcp': {'servers': [{k: v for k, v in s.items() if k not in internal} for s in usable]}}
    return {'argv': argv, 'payload': payload, 'diagnostics': diagnostics,
            'bridge': load_bridge, 'servers': [s['name'] for s in usable], 'source': source,
            'skills': inherited_skills}

def doctor(rt):
    inh=rt.config['inheritance']
    report={'baseline':CODEX_MCP_BASELINE,
            'config':{k:inh.get(k) for k in ('enabled','skills','mcp','codex_home','mcp_protocol_mode')},'scopes':[],
            'note':'Environment variable and header values are never shown; only names and sources.'}
    for s in rt.store.all('SELECT * FROM scopes ORDER BY created DESC LIMIT 100'):
        entry={'scope':s['id'],'label':s['label'],'cwd':s['cwd'],
               'inheritance_enabled':bool(s['inheritance']) and bool(inh.get('enabled',True)),
               'codex_home':s['codex_home'],'source_mode':s['codex_source'],
               'bound_env_names':sorted(rt.scope_env.get(s['id'],{}))}
        if inh.get('enabled') and s['inheritance'] and s['codex_home']:
            home=Path(s['codex_home'])
            try:
                raw=read_codex_config(home)
                skills,skill_diag=collect_skills(home,raw,s['cwd'],[])
                servers,mcp_diag=parse_mcp_servers(home,raw)
                try:
                    servers,env_diag=resolve_environment(servers,rt.scope_env.get(s['id'],{}))
                    mcp_diag+=env_diag
                    servers,acc_diag=policy_filter(servers,'write')
                    mcp_diag+=acc_diag
                except AgentError as exc:
                    mcp_diag.append(Diagnostic('mcp','required',exc.message))
                entry.update(inherited_skills=[{'path':path,'name':Path(path).name} for path in skills],
                             mcp_servers=[{'name':x['name'],'transport':x['transport'],
                                           'disposition':x.get('disposition'),'required':x.get('required',False),
                                           'reasons':x.get('reasons',[])} for x in servers],
                             diagnostics=[d.as_dict() for d in skill_diag+mcp_diag])
            except AgentError as exc:
                entry['error']=exc.as_dict()
        report['scopes'].append(entry)
    return report
