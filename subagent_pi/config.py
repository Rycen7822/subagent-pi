from __future__ import annotations
import os
from pathlib import Path
import shutil
import tomllib
from .common import AgentError, text

DEFAULT = {
    'max_resident_agents': 4, 'max_agents_per_scope': 16,
    'rpc_timeout_seconds': 20, 'startup_timeout_seconds': 30,
    'default_run_timeout_seconds': 1800, 'max_wait_seconds': 600,
    'event_max_count_per_agent': 20000,
    'profiles': {
        'default': {'extensions': [], 'skills': [], 'ambient_extensions': False,
                    'ambient_skills': False, 'tools': ['read','bash','edit','write','grep','find','ls']},
        'reader': {'extensions': [], 'skills': [], 'ambient_extensions': False,
                   'ambient_skills': False, 'tools': ['read','grep','find','ls']},
    }
}

def load_config(home: Path):
    result = dict(DEFAULT)
    result['profiles'] = {k:dict(v) for k,v in DEFAULT['profiles'].items()}
    file = home/'config.toml'
    if file.exists():
        with file.open('rb') as f: raw = tomllib.load(f)
        unknown = set(raw) - set(DEFAULT) - {'pi_command'}
        if unknown: raise AgentError('invalid_config', f'Unknown config keys: {sorted(unknown)}')
        for key,value in raw.items():
            if key == 'profiles':
                for name, profile in value.items():
                    result['profiles'][name] = {**result['profiles'].get(name,result['profiles']['default']),**profile}
            else: result[key] = value
    result.setdefault('pi_command', [os.environ.get('PI_AGENTS_PI', 'pi')])
    for k in DEFAULT:
        if k == 'profiles': continue
        v = result[k]
        if isinstance(v,bool) or not isinstance(v,int) or not 1 <= v <= 10_000_000:
            raise AgentError('invalid_config', f'{k} must be a positive bounded integer')
    for name,profile in result['profiles'].items():
        if not isinstance(profile,dict): raise AgentError('invalid_config',f'Profile {name} must be a table')
        for key in ('ambient_extensions','ambient_skills'):
            if key in profile and not isinstance(profile[key],bool):
                raise AgentError('invalid_config',f'{name}.{key} must be a TOML boolean, not text')
    command = result['pi_command']
    if not isinstance(command,list) or not command or any(not isinstance(x,str) or not x or '\x00' in x for x in command):
        raise AgentError('invalid_config','pi_command must be a nonempty argv list')
    return result

def launch_spec(config, profile_name, model, cwd, access):
    if profile_name not in config['profiles']: raise AgentError('profile_not_found',f'Unknown profile: {profile_name}')
    p = config['profiles'][profile_name]
    unknown = set(p) - {'extensions','skills','ambient_extensions','ambient_skills','tools','model','provider','thinking','env'}
    if unknown: raise AgentError('invalid_config',f'Unknown profile keys: {sorted(unknown)}')
    executable = shutil.which(config['pi_command'][0])
    if not executable: raise AgentError('pi_not_found','Pi executable not found; set pi_command in config.toml or PI_AGENTS_PI')
    tools = p.get('tools',[])
    if not isinstance(tools,list) or any(t not in {'read','bash','edit','write','grep','find','ls'} for t in tools):
        raise AgentError('invalid_config','tools must be a list of Pi builtin names')
    if access == 'read':
        tools = [t for t in tools if t in {'read','grep','find','ls'}]
        if p.get('ambient_extensions') or p.get('extensions'):
            raise AgentError('invalid_config','read profile must disable all extensions; tool limits are not an OS sandbox')
    env = p.get('env',{})
    if not isinstance(env,dict) or any(not isinstance(k,str) or not isinstance(v,str) for k,v in env.items()):
        raise AgentError('invalid_config','profile env must contain string values')
    # Keep only explicitly provided environment in the snapshot; inherited credentials are never serialized.
    argv = [executable,*config['pi_command'][1:],'--mode','rpc']
    if not p.get('ambient_extensions',False): argv.append('--no-extensions')
    if not p.get('ambient_skills',False): argv.append('--no-skills')
    argv += ['--tools',','.join(tools)] if tools else ['--no-tools']
    for flag,key in [('--extension','extensions'),('--skill','skills')]:
        paths = p.get(key,[])
        if not isinstance(paths,list): raise AgentError('invalid_config',f'{key} must be a list')
        for raw in paths:
            resolved = Path(text(raw,key)).expanduser().resolve()
            if not resolved.exists(): raise AgentError('invalid_config',f'Missing {key} path: {resolved}')
            argv += [flag,str(resolved)]
    actual_model = model or p.get('model')
    if actual_model: argv += ['--model',text(actual_model,'model',512)]
    if p.get('provider'): argv += ['--provider',text(p['provider'],'provider',128)]
    if p.get('thinking'): argv += ['--thinking',text(p['thinking'],'thinking',32)]
    return {'argv':argv,'cwd':cwd,'profile':profile_name,'access':access,'model':actual_model,'env':env,
            'ambient_extensions':p.get('ambient_extensions',False),'ambient_skills':p.get('ambient_skills',False)}
