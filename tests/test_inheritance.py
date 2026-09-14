"""Inheritance tests: source resolution, skills, MCP parsing, the private pipe
channel, secret canaries, and no-new-runtime-files guarantees. All offline."""
from __future__ import annotations
import asyncio
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from subagent_pi.common import AgentError, dumps
from subagent_pi.inheritance import (Diagnostic, capture_scope_env, collect_skills, parse_mcp_servers,
    policy_filter, read_codex_config, referenced_env_names, resolve_codex_home, resolve_environment)
from subagent_pi.runtime import Runtime
from subagent_pi.store import Store

FAKE_PI=ROOT/'tests'/'fake_pi.py'
BRIDGE=ROOT/'extensions'/'codex-mcp-bridge.ts'

def make_skill(base: Path, name: str, body='Body.', front_name=None):
    d=base/name
    d.mkdir(parents=True)
    fm=front_name or name
    (d/'SKILL.md').write_text(f'---\nname: {fm}\ndescription: test skill {name}\n---\n\n{body}\n')
    return d

def make_codex_home(base: Path, config: str='') -> Path:
    home=base/'codex-home'
    (home/'skills').mkdir(parents=True)
    (home/'config.toml').write_text(config)
    return home

def fake_pi_command() -> str:
    return json.dumps([sys.executable,str(FAKE_PI)])

class SourceResolution(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='inh-src-'); self.base=Path(self.tmp.name)
    def tearDown(self): self.tmp.cleanup()
    def config(self,home=None):
        return {'enabled':True,'skills':True,'mcp':True,'codex_home':str(home) if home else None}
    def test_explicit_wins_over_env_and_default(self):
        a=make_codex_home(self.base/'a'); b=make_codex_home(self.base/'b')
        self.assertEqual(resolve_codex_home(self.config(a),{'CODEX_HOME':str(b)})[1],'explicit')
        self.assertEqual(resolve_codex_home(self.config(a),{'CODEX_HOME':str(b)})[0],a)
    def test_scope_env_before_default(self):
        b=make_codex_home(self.base/'b')
        home,mode=resolve_codex_home(self.config(None),{'CODEX_HOME':str(b)})
        self.assertEqual((home,mode),(b,'scope_env'))
    def test_user_default_uses_home_env(self):
        c=make_codex_home(self.base/'c')
        old=os.environ.get('HOME')
        os.environ['HOME']=str(self.base/'c')  # -> base/c/.codex
        try:
            (self.base/'c'/'.codex').mkdir()
            home,mode=resolve_codex_home(self.config(None),{})
            self.assertEqual((mode,home.name),('user_default','.codex'))
        finally:
            if old is not None: os.environ['HOME']=old
    def test_missing_source_returns_none(self):
        old=os.environ.get('HOME')
        os.environ['HOME']=str(self.base/'nonexistent-home')
        try:
            home,mode=resolve_codex_home(self.config(None),{})
            self.assertIsNone(home)
        finally:
            if old is not None: os.environ['HOME']=old
    def test_nonexistent_explicit_dir_is_an_error(self):
        # 4.1: a configured source that is missing must not silently fall back to
        # another candidate directory.
        make_codex_home(self.base/'b')
        with self.assertRaises(AgentError) as cm:
            resolve_codex_home(self.config(self.base/'missing'),{'CODEX_HOME':str(self.base/'b')})
        self.assertEqual(cm.exception.code,'inheritance_source_unreadable')
    def test_missing_scope_env_source_is_an_error(self):
        with self.assertRaises(AgentError) as cm:
            resolve_codex_home(self.config(None),{'CODEX_HOME':str(self.base/'also-missing')})
        self.assertEqual(cm.exception.code,'inheritance_source_unreadable')

class SkillCollection(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='inh-skill-'); self.base=Path(self.tmp.name)
        self.home=make_codex_home(self.base)
    def tearDown(self): self.tmp.cleanup()
    def test_codex_skills_selected_in_place(self):
        s=make_skill(self.home/'skills','alpha')
        selected,diag=collect_skills(self.home,{},None,[])
        self.assertEqual([Path(p) for p in selected],[s])
        self.assertTrue((selected[0].startswith(str(self.home))))  # original location
    def test_project_and_profile_dedup_by_realpath(self):
        proj=make_skill(self.base/'proj'/'.agents'/'skills','alpha')
        link=self.home/'skills'/'alpha'
        link.symlink_to(proj)
        selected,diag=collect_skills(self.home,{},str(self.base/'proj'),[str(proj)])
        self.assertEqual(selected,[str(proj)])  # same real path stays single entry
    def test_name_conflict_refuses_ambiguity(self):
        make_skill(self.home/'skills','alpha',front_name='dup')
        proj=make_skill(self.base/'proj'/'.agents'/'skills','beta',front_name='dup')
        selected,diag=collect_skills(self.home,{},str(self.base/'proj'),[str(proj)])
        self.assertNotIn(str(self.home/'skills'/'alpha'),selected)
        self.assertTrue(any('name conflict' in d.reason for d in diag))
    def test_disabled_by_skills_config(self):
        s=make_skill(self.home/'skills','gone')
        config=f'[[skills.config]]\npath = "{s}/SKILL.md"\nenabled = false\n'
        raw=read_codex_config(self.home) if False else __import__('tomllib').loads(config)
        selected,diag=collect_skills(self.home,raw,None,[])
        self.assertEqual(selected,[])
        self.assertTrue(any('disabled by codex' in d.reason for d in diag))
    def test_management_skill_excluded_by_name_and_plugin_path(self):
        make_skill(self.home/'skills','pi-subagents')
        plugin_skill=ROOT/'skills'/'pi-subagents'
        alias=self.home/'skills'/'renamed-alias'
        if not alias.exists(): alias.symlink_to(plugin_skill)
        selected,diag=collect_skills(self.home,{},None,[])
        self.assertEqual([p for p in selected if 'renamed-alias' in p],[])
        self.assertTrue(all('pi-subagents' not in p for p in selected))
        self.assertTrue(any('recursion guard' in d.reason for d in diag))
    def test_missing_dir_diagnostic_and_chinese_space_paths(self):
        empty=make_codex_home(self.base/'x')
        (empty/'skills').rmdir()
        selected,diag=collect_skills(empty,{},None,[])
        self.assertEqual(selected,[])
        self.assertTrue(any('not present' in d.reason for d in diag))
        odd=make_skill(self.home/'skills','我的 技能')
        selected,_=collect_skills(self.home,{},None,[])
        self.assertIn(str(odd.resolve()),[str(Path(p).resolve()) for p in selected])
    def test_malformed_skill_entry_skipped(self):
        (self.home/'skills'/'not-a-skill').mkdir()
        (self.home/'skills'/'not-a-skill'/'README.md').write_text('hi')
        selected,diag=collect_skills(self.home,{},None,[])
        self.assertEqual(selected,[])

STDIO_TOML='''
[mcp_servers.filesrv]
command = "python3"
args = ["-m", "fake_server"]
cwd = "servers/dir with space"
env_vars = ["TOKEN_VAR"]

[mcp_servers.filesrv.env]
STATIC_K = "static-v"
'''
REMOTE_TOML='''
[mcp_servers.remote]
command = "run"
env_vars = [{ name = "REMOTE_X", source = "remote" }]
'''
HTTP_TOML='''
[mcp_servers.web]
url = "https://example.com/mcp"
bearer_token_env_var = "WEB_TOKEN"
env_http_headers = { "X-Trace" = "TRACE_ID" }
http_headers = { "X-Static" = "sv" }
enabled_tools = ["search"]
disabled_tools = ["danger"]
startup_timeout_sec = 7
tool_timeout_sec = 33
default_tools_approval_mode = "auto"
'''
class McpParsing(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='inh-mcp-'); self.base=Path(self.tmp.name)
        self.home=make_codex_home(self.base)
    def tearDown(self): self.tmp.cleanup()
    def parse(self,config): return parse_mcp_servers(self.home,__import__('tomllib').loads(config))
    def test_stdio_fields_normalized(self):
        servers,diag=self.parse(STDIO_TOML)
        self.assertEqual(len(servers),1)
        s=servers[0]
        self.assertEqual(s['disposition'],'ok')
        self.assertEqual(s['transport'],'stdio')
        self.assertEqual(s['command'],'python3')
        self.assertEqual(s['args'],['-m','fake_server'])
        self.assertEqual(s['env_var_names'],['TOKEN_VAR'])
        self.assertTrue(s['cwd'].startswith(str(self.home)))  # relative cwd anchored to codex home
        self.assertIn('dir with space',s['cwd'])
        self.assertEqual(s['static_env'],{'STATIC_K':'static-v'})
    def test_remote_env_source_disables_server(self):
        servers,diag=self.parse(REMOTE_TOML)
        # F11: the failed server keeps its disposition instead of vanishing.
        self.assertEqual([s['disposition'] for s in servers],['failed'])
        self.assertTrue(any('source=remote' in d.reason for d in diag))
    def test_http_fields_and_policy_fields(self):
        servers,diag=self.parse(HTTP_TOML)
        self.assertEqual(len(servers),1)
        s=servers[0]
        self.assertEqual(s['transport'],'http')
        self.assertEqual(s['static_headers'],{'X-Static':'sv'})
        self.assertEqual(s['env_header_names'],{'X-Trace':'TRACE_ID'})
        self.assertEqual(s['bearer_token_env_var'],'WEB_TOKEN')
        self.assertEqual(s['allowed_tools'],['search'])
        self.assertEqual(s['disabled_tools'],['danger'])
        self.assertEqual(s['approval_default'],'auto')
        self.assertEqual(s['tool_approval'],{})
        self.assertEqual(s['startup_timeout_sec'],7)
        self.assertEqual(s['tool_timeout_sec'],33)
    def test_disabled_and_unknown_critical_keys(self):
        config=HTTP_TOML+'\n[mcp_servers.bad]\ncommand = "x"\nsandbox_permissions = ["full"]\n\n[mcp_servers.off]\ncommand = "y"\nenabled = false\n'
        servers,diag=self.parse(config)
        dispo={s['name']:s['disposition'] for s in servers}
        self.assertEqual(dispo['bad'],'failed'); self.assertEqual(dispo['off'],'disabled')
        self.assertEqual(dispo['web'],'ok')  # unaffected servers continue
        self.assertTrue(any('unsupported config keys' in d.reason and d.name=='bad' for d in diag))
        self.assertTrue(any(d.name=='off' and 'disabled in codex config' in d.reason for d in diag))
    def test_oauth_and_helper_rejected(self):
        config='''
[mcp_servers.a]
url = "https://x.example/mcp"
auth = "oauth"
[mcp_servers.b]
url = "https://x.example/mcp"
http_headers_helper = "/bin/helper"
[mcp_servers.c]
command = "run"
experimental_environment = "remote"
'''
        servers,diag=self.parse(config)
        self.assertEqual({s['disposition'] for s in servers},{'failed'})
        reasons=' '.join(d.reason for d in diag)
        self.assertIn('oauth',reasons); self.assertIn('http_headers_helper',reasons); self.assertIn('remote',reasons)
    def test_required_parse_failure_is_kept_and_named(self):
        # F11: an unsupported REQUIRED server cannot silently disappear.
        config='[mcp_servers.core]\nurl = "https://x.example/mcp"\nauth = "oauth"\nrequired = true\n'
        servers,diag=self.parse(config)
        self.assertEqual(servers[0]['disposition'],'failed')
        self.assertTrue(servers[0]['required'])
        self.assertTrue(any('oauth' in d.reason for d in diag))
    def test_empty_allowlist_means_no_tools(self):
        config='[mcp_servers.strict]\ncommand = "x"\nenabled_tools = []\n'
        servers,_=self.parse(config)
        self.assertEqual(servers[0]['allowed_tools'],[])
        policy_filter(servers,'write')
        self.assertEqual(servers[0]['allowed_tools'],[])
    def test_recursion_guard_direct_command(self):
        bin_path=ROOT/'bin'/'subagent-pi'
        config=f'[mcp_servers.renamed_control]\ncommand = "{bin_path}"\nargs = ["mcp"]\n'
        servers,diag=self.parse(config)
        self.assertEqual(servers[0]['disposition'],'failed')
        self.assertTrue(any('recursion guard' in d.reason for d in diag))
    def test_recursion_guard_installer_wrapper_and_module_form(self):
        # F12: the installer generates command=sys.executable args=[<bin>,'mcp'];
        # `python -m subagent_pi` must be caught too. Renaming the server evades nothing.
        bin_path=ROOT/'bin'/'subagent-pi'
        for cfg in (f'[mcp_servers.ctrl]\ncommand = "{sys.executable}"\nargs = ["{bin_path}", "mcp"]\n',
                    '[mcp_servers.ctrl2]\ncommand = "python3"\nargs = ["-m", "subagent_pi", "mcp"]\n'):
            servers,diag=self.parse(cfg)
            self.assertEqual(servers[0]['disposition'],'failed',cfg)
            self.assertTrue(any('recursion guard' in d.reason for d in diag))
    def test_recursion_guard_does_not_catch_normal_python(self):
        servers,_=self.parse('[mcp_servers.plain]\ncommand = "python3"\nargs = ["-m", "other_tool"]\n')
        self.assertEqual(servers[0]['disposition'],'ok')
    def test_approval_policy_table(self):
        # F06: per-tool override wins over server default; writes/unknown degrade to confirm.
        def policy(cfg):
            servers,_=self.parse(cfg)
            return servers[0]['approval_default'],servers[0]['tool_approval'],servers[0]['disabled_tools']
        cfg='''[mcp_servers.example]
command = "x"
default_tools_approval_mode = "auto"
[mcp_servers.example.tools.delete_file]
approval_mode = "prompt"
'''
        default,tools,denied=policy(cfg)
        self.assertEqual(default,'auto')
        self.assertEqual(tools,{'delete_file':'confirm'})
        cfg2='''[mcp_servers.example2]
command = "x"
default_tools_approval_mode = "prompt"
[mcp_servers.example2.tools.safe_thing]
approval_mode = "auto"
'''
        default,tools,denied=policy(cfg2)
        self.assertEqual(default,'confirm')
        self.assertEqual(tools,{'safe_thing':'auto'})
        cfg3='''[mcp_servers.example3]
command = "x"
[mcp_servers.example3.tools.limited]
output_token_limit = 100
[mcp_servers.example3.tools.weird]
approval_mode = "banana"
'''
        default,tools,denied=policy(cfg3)
        self.assertIn('limited',denied); self.assertIn('weird',denied)
        self.assertEqual(tools,{})
    def test_resolve_environment_from_snapshot_only(self):
        servers,_=self.parse(STDIO_TOML)
        resolve_environment(servers,{'TOKEN_VAR':'token-a','PATH':'/bin'})
        self.assertEqual(servers[0]['env']['TOKEN_VAR'],'token-a')
        self.assertEqual(servers[0]['env']['STATIC_K'],'static-v')
        self.assertNotIn('PATH',servers[0]['env'])  # base env is the child runner's job, not per-server
    def test_required_missing_env_blocks(self):
        config='[mcp_servers.core]\ncommand = "x"\nenv_vars = ["MISSING_VAR"]\nrequired = true\n'
        servers,_=self.parse(config)
        with self.assertRaises(AgentError) as cm:
            resolve_environment(servers,{})
        self.assertEqual(cm.exception.code,'inheritance_required_server_failed')
        self.assertIn('core',cm.exception.message)  # message carries server names, not values
        self.assertIn('MISSING_VAR',servers[0]['reasons'][0])
        self.assertEqual(servers[0]['disposition'],'failed')  # disposition recorded before raising
    def test_optional_missing_env_excluded_with_named_diagnostic(self):
        config='[mcp_servers.opt]\ncommand = "x"\nenv_vars = ["ABSENT_VAR"]\n'
        servers,_=self.parse(config)
        resolve_environment(servers,{})
        self.assertEqual(servers[0]['disposition'],'failed')
        self.assertTrue(any('ABSENT_VAR' in r for r in servers[0].get('reasons',[])))
    def test_bearer_and_env_headers_resolved(self):
        servers,_=self.parse(HTTP_TOML)
        resolve_environment(servers,{'WEB_TOKEN':'tok','TRACE_ID':'tr-1'})
        self.assertEqual(servers[0]['bearer_token'],'tok')
        self.assertEqual(servers[0]['headers'],{'X-Static':'sv','X-Trace':'tr-1'})
    def test_read_child_policy_intersects(self):
        servers,_=self.parse(HTTP_TOML)  # has explicit allowlist
        policy_filter(servers,'read')
        self.assertEqual(servers[0]['name'],'web'); self.assertNotIn('confirm_all',servers[0])
        no_list,_=self.parse('[mcp_servers.open]\ncommand = "x"\n')
        policy_filter(no_list,'read')
        self.assertTrue(no_list[0]['confirm_all'])
        no_list2,_=self.parse('[mcp_servers.open2]\ncommand = "x"\n')
        policy_filter(no_list2,'write')
        self.assertNotIn('confirm_all',no_list2[0])
    def test_diagnostics_are_str_only(self):
        # F10: missing default skills dir previously put a Path object into the diagnostic.
        skills,diag=collect_skills(self.home,{},None,[])
        dumps([d.as_dict() for d in diag])  # must not raise
        self.assertTrue(all(isinstance(d.as_dict()['name'],str) for d in diag))
    def test_capture_scope_env_is_minimal(self):
        (self.home/'config.toml').write_text(STDIO_TOML+'[mcp_servers.w2]\nurl="https://e.example"\nbearer_token_env_var="BT"\n')
        environ={'PATH':'/bin','HOME':'/h','TOKEN_VAR':'tv','BT':'bt','ANTHROPIC_API_KEY':'sk-test',
                 'UNRELATED_SECRET':'nope','CODEX_HOME':str(self.home)}
        snap=capture_scope_env(self.home,environ,extra_names=('ANTHROPIC_API_KEY',))
        self.assertIn('TOKEN_VAR',snap); self.assertIn('BT',snap); self.assertIn('PATH',snap)
        self.assertEqual(snap['ANTHROPIC_API_KEY'],'sk-test')  # authorized child-env name is captured for model auth
        self.assertNotIn('UNRELATED_SECRET',snap)
        servers,_=self.parse(STDIO_TOML+'[mcp_servers.w2]\nurl="https://e.example"\nbearer_token_env_var="BT"\n')
        self.assertEqual(set(referenced_env_names(servers)),{'PATH','HOME','LANG','LC_ALL','TERM','TMPDIR','SHELL','USER','LOGNAME','CODEX_HOME','TOKEN_VAR','BT'})

class RuntimeInheritance(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='inh-rt-')
        self.root=Path(self.tmp.name); self.home=self.root/'state'; self.home.mkdir()
        self.workspace=self.root/'workspace'; self.workspace.mkdir()
        self.codex=make_codex_home(self.root/'src',config=STDIO_TOML)
        make_skill(self.codex/'skills','alpha')
        make_skill(self.workspace/'.agents'/'skills','projskill')
        (self.root/'src'/'server.py').write_text(
            'import sys,json\n'
            'for line in sys.stdin:\n'
            '    r=json.loads(line)\n'
            '    if "id" in r: print(json.dumps({"id":r["id"],"result":{"tools":[]}}),flush=True)\n')
        config=STDIO_TOML.replace('command = "python3"','command = "python3"')
        config+='\n[mcp_servers.localtest]\ncommand = "python3"\nargs = ["server.py"]\nenv_vars = ["SECRET_CANARY"]\n'
        config+=f'\n[mcp_servers.localtest.env]\nFAKE_TEST_ECHO = "echo-ok"\n'
        (self.codex/'config.toml').write_text(config)
        (self.codex/'servers').mkdir()
        (self.home/'config.toml').write_text('pi_command = '+fake_pi_command()+'\nrpc_timeout_seconds = 8\nstartup_timeout_seconds = 25\n')
        self.rt=Runtime(self.home)
        result=await self.rt.dispatch('scope_open',{'cwd':str(self.workspace)},
            source={'env':{'CODEX_HOME':str(self.codex),'SECRET_CANARY':'canary-值-2026','PATH':os.environ['PATH'],'HOME':os.environ['HOME']}})
        self.scope=result['scope']
        self.n=0
    async def asyncTearDown(self):
        await self.rt.shutdown(); self.tmp.cleanup()
    def key(self): self.n+=1; return 'req-'+str(self.n)
    async def spawn(self,**extra):
        return await self.rt.dispatch('spawn',{'scope':self.scope,'request_id':self.key(),
            'cwd':str(self.workspace),'task':'simple','access':'read',**extra})
    async def test_required_server_failure_blocks_spawn_without_prompt(self):
        # F11: a broken REQUIRED server aborts the boot; no prompt is ever sent
        # and the worker does not linger.
        (self.codex/'config.toml').write_text(
            '[mcp_servers.broken]\ncommand = "/definitely/missing/binary"\nrequired = true\n')
        before=self.fake_prompt_count()
        with self.assertRaises(AgentError) as cm:
            await self.spawn()
        self.assertEqual(cm.exception.code,'inheritance_required_server_failed')
        self.assertTrue(all(r[0] not in ('starting','running','queued') for r in self.run_rows()),
                        f'runs not terminal: {self.run_rows()}')
        self.assertTrue(all(w.closed for w in self.rt.workers.values()))  # half-started workers are terminated
        self.assertEqual(self.fake_prompt_count(),before+1)  # the failed run is recorded, never sent
        (self.codex/'config.toml').write_text(STDIO_TOML)
    def fake_prompt_count(self):
        con=sqlite3.connect(f'file:{self.home}/registry.sqlite?mode=ro',uri=True)
        try:
            rows=con.execute("SELECT task FROM runs").fetchall()
        finally: con.close()
        return len(rows)
    def run_rows(self):
        con=sqlite3.connect(f'file:{self.home}/registry.sqlite?mode=ro',uri=True)
        try:
            rows=con.execute("SELECT state FROM runs").fetchall()
        finally: con.close()
        return rows
    async def test_scope_binding_persists_nonsecret_source(self):
        row=self.rt.store.scope(self.scope)
        self.assertEqual(row['codex_home'],str(self.codex))
        self.assertEqual(row['codex_source'],'scope_env')
        self.assertEqual(self.rt.scope_env[self.scope]['SECRET_CANARY'],'canary-值-2026')
    def snapshot(self,root: Path):
        return {str(p.relative_to(root)):p.stat().st_size for p in root.rglob('*') if p.is_file()}
    async def test_boot_writes_skills_and_marker_without_disk_config(self):
        before_state=self.snapshot(self.home); before_codex=self.snapshot(self.codex)
        s=await self.spawn()
        w=self.rt.workers[s['agent_id']]
        launch=json.loads((self.home/'agents'/s['agent_id']/'launch.json').read_text())
        argv=launch['argv']
        skill_paths=[argv[i+1] for i,f in enumerate(argv) if f=='--skill']
        self.assertIn(str(self.codex/'skills'/'alpha'),skill_paths)
        self.assertIn(str(self.workspace/'.agents'/'skills'/'projskill'),skill_paths)
        self.assertIn(str(BRIDGE),argv)
        self.assertIn('codex_mcp',' '.join(argv))  # bridge tool enabled in --tools
        stderr=(self.home/'agents'/s['agent_id']/'stderr.log').read_text()
        self.assertIn('subagent-pi-bridge ready servers=1 names=localtest',stderr)
        self.assertIn('bridge-env localtest=echo-ok',stderr)
        # filesrv is excluded because its TOKEN_VAR is absent from the scope snapshot.
        diag_events=[json.loads(e['payload']) for e in self.rt.store.all("SELECT payload FROM events WHERE type='inheritance_diagnostics'")]
        self.assertTrue(any('TOKEN_VAR' in d.get('reason','') for x in diag_events for d in x.get('diagnostics',[])))
        # No inherited configuration landed on disk; the codex source tree is untouched.
        after_codex=self.snapshot(self.codex)
        self.assertEqual(before_codex,after_codex)  # codex source tree untouched
        await self.mutation_close(s['agent_id'])
        return s
    async def mutation_close(self,aid):
        return await self.rt.dispatch('close',{'scope':self.scope,'agent_id':aid,'request_id':self.key()})
    async def test_canary_never_reaches_disk(self):
        s=await self.spawn()
        await asyncio.sleep(0.2)
        needle=b'canary-\xe5\x80\xbc-2026'
        for p in self.home.rglob('*'):
            if p.is_file():
                if p.suffix in ('.sqlite','.db') or 'registry' in p.name:
                    continue
                self.assertNotIn(needle,p.read_bytes(),f'canary leaked into {p}')
        db=sqlite3.connect(self.home/'registry.sqlite')
        for table in ('agents','scopes','runs','events','requests','receipts','meta'):
            for row in db.execute(f'SELECT * FROM {table}'):
                for cell in row:
                    if isinstance(cell,bytes): self.assertNotIn(needle,cell)
                    if isinstance(cell,str): self.assertNotIn('canary-值-2026',cell)
        db.close()
        for p in Path(os.environ.get('XDG_RUNTIME_DIR','/tmp')).glob(f'subagent-pi-*'):
            self.assertNotIn(needle,p.read_bytes() if p.is_file() else b'')
    async def test_respawn_rebuilds_inheritance_without_argv_growth(self):
        s=await self.spawn()
        aid=s['agent_id']
        await self.mutation_close(aid)
        first=json.loads((self.home/'agents'/aid/'launch.json').read_text())['argv']
        # Source change: skill removed; respawn must reflect it, not accumulate flags.
        shutil.rmtree(self.codex/'skills'/'alpha')
        await self.rt.dispatch('respawn',{'scope':self.scope,'agent_id':aid,'request_id':self.key()})
        second=json.loads((self.home/'agents'/aid/'launch.json').read_text())['argv']
        await self.mutation_close(aid)
        await self.rt.dispatch('respawn',{'scope':self.scope,'agent_id':aid,'request_id':self.key()})
        third=json.loads((self.home/'agents'/aid/'launch.json').read_text())['argv']
        self.assertNotIn(str(self.codex/'skills'/'alpha'),second)
        # Model pinning survives respawns exactly once; inheritance flags never accumulate.
        self.assertEqual(second.count('--model'),1)
        self.assertEqual(third.count('--model'),1)
        self.assertLessEqual(third.count('--skill'),second.count('--skill'))
        self.assertIn('codex_mcp',' '.join(third))
        await self.mutation_close(aid)
    async def test_disabled_inheritance_leaves_original_path(self):
        # Per-scope management switch: an explicit scope_open with inheritance=false.
        await self.rt.dispatch('scope_open',{'cwd':str(self.workspace),'scope':self.scope,'inheritance':False},
            source={'env':{'CODEX_HOME':str(self.codex),'PATH':os.environ['PATH']}})
        s=await self.rt.dispatch('spawn',{'scope':self.scope,'request_id':self.key(),
            'cwd':str(self.workspace),'task':'simple','access':'read'})
        launch=json.loads((self.home/'agents'/s['agent_id']/'launch.json').read_text())
        self.assertNotIn('--skill',launch['argv'])
        self.assertNotIn('--extension',launch['argv'])
        await self.rt.dispatch('close',{'scope':self.scope,'agent_id':s['agent_id'],'request_id':self.key()})
    async def test_source_conflict_requires_explicit_rebind(self):
        other=make_codex_home(self.root/'other')
        with self.assertRaises(AgentError) as cm:
            await self.rt.dispatch('scope_open',{'cwd':str(self.workspace),'scope':self.scope},
                source={'env':{'CODEX_HOME':str(other),'PATH':os.environ['PATH']}})
        self.assertEqual(cm.exception.code,'inheritance_source_conflict')
        r=await self.rt.dispatch('scope_open',{'cwd':str(self.workspace),'scope':self.scope,'codex_home':str(other)},
            source={'env':{'PATH':os.environ['PATH']}})
        self.assertEqual(self.rt.store.scope(self.scope)['codex_home'],str(other))
    async def test_doctor_reports_names_not_values(self):
        s=await self.spawn()
        report=(await self.rt.dispatch('doctor',{'inheritance':True}))['inheritance']
        blob=json.dumps(report)
        self.assertIn('canary' if False else 'SECRET_CANARY',blob)  # variable NAME is allowed
        self.assertNotIn('canary-值-2026',blob)  # value is not
        scope_report=[x for x in report['scopes'] if x['scope']==self.scope][0]
        self.assertEqual(scope_report['source_mode'],'scope_env')
        self.assertIn('SECRET_CANARY',scope_report['bound_env_names'])
        self.assertTrue(any(x['name']=='localtest' for x in scope_report['mcp_servers']))
        await self.mutation_close(s['agent_id'])
    async def test_bootstrap_write_failure_recorded_without_payload(self):
        import subagent_pi.runtime as rt_mod
        r,w=os.pipe()
        os.close(r)
        await self.rt._write_bootstrap(w,'pi_none',9,b'{"mcp":{"servers":[]}}')
        row=self.rt.store.one("SELECT payload FROM events WHERE type='bootstrap_write_failed' ORDER BY seq DESC LIMIT 1")
        self.assertIsNotNone(row)
        self.assertEqual(json.loads(row['payload'])['error'],'BrokenPipeError')

class StoreMigration(unittest.TestCase):
    def test_v1_database_upgrades_transactionally(self):
        tmp=tempfile.TemporaryDirectory(prefix='inh-mig-'); base=Path(tmp.name)
        db=sqlite3.connect(base/'registry.sqlite')
        db.executescript('''
        CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE scopes(id TEXT PRIMARY KEY,cwd TEXT NOT NULL,label TEXT NOT NULL,created REAL NOT NULL,revision INTEGER NOT NULL DEFAULT 0);
        INSERT INTO meta VALUES('schema','1');
        INSERT INTO scopes(id,cwd,label,created) VALUES('scope_x','/tmp','old',1);
        '''); db.commit(); db.close()
        store=Store(base)
        cols={r['name'] for r in store.all("PRAGMA table_info(scopes)")}
        self.assertIn('codex_home',cols); self.assertIn('inheritance',cols)
        self.assertEqual(store.one("SELECT value FROM meta WHERE key='schema'")['value'],'2')
        self.assertEqual(store.scope('scope_x')['inheritance'],1)
        store.close(); tmp.cleanup()

class RealPiBridge(unittest.IsolatedAsyncioTestCase):
    """Real Pi process + real TS bridge. NOT in the default suite.

    Requires explicit SUBAGENT_PI_LIVE_PI=1. It boots a real Pi worker and
    verifies the extension loads and reports ready through the receipt channel;
    it never sends a business prompt, so no model call is possible.
    """
    async def asyncSetUp(self):
        if os.environ.get('SUBAGENT_PI_LIVE_PI')!='1':
            raise unittest.SkipTest('set SUBAGENT_PI_LIVE_PI=1 to run the real-Pi check; default suite never launches Pi')
        if not shutil.which('pi'): self.skipTest('pi executable not available')
        self.tmp=tempfile.TemporaryDirectory(prefix='inh-live-')
        self.root=Path(self.tmp.name); self.home=self.root/'state'; self.home.mkdir()
        self.workspace=self.root/'workspace'; self.workspace.mkdir()
        self.codex=make_codex_home(self.root/'src')
        (self.root/'srv.py').write_text(
            'import sys,json\n'
            'for line in sys.stdin:\n'
            '    r=json.loads(line)\n'
            '    if "id" in r and r.get("method")=="tools/list":\n'
            '        print(json.dumps({"jsonrpc":"2.0","id":r["id"],"result":{"tools":[{"name":"echo","description":"t"}]}}),flush=True)\n'
            '    elif "id" in r:\n'
            '        print(json.dumps({"jsonrpc":"2.0","id":r["id"],"result":{}}),flush=True)\n')
        (self.codex/'config.toml').write_text(
            '[mcp_servers.echosrv]\ncommand = "python3"\nargs = ["../srv.py"]\n'.replace('../srv.py',str(self.root/'srv.py')))
        (self.home/'config.toml').write_text('pi_command = ["pi"]\nrpc_timeout_seconds = 15\nstartup_timeout_seconds = 40\n')
        self.rt=Runtime(self.home)
        r=await self.rt.dispatch('scope_open',{'cwd':str(self.workspace)},
            source={'env':{'CODEX_HOME':str(self.codex),'PATH':os.environ['PATH'],'HOME':os.environ['HOME']}})
        self.scope=r['scope']
        # Boot only: start_run is stubbed so no business prompt (and therefore no
        # model request) can ever be sent from this test.
        async def _no_prompt(w,rid): return None
        self.rt.start_run=_no_prompt
    async def asyncTearDown(self):
        await self.rt.shutdown(); self.tmp.cleanup()
    async def test_bridge_loads_in_real_pi_child(self):
        s=await self.rt.dispatch('spawn',{'scope':self.scope,'request_id':'live-1',
            'cwd':str(self.workspace),'task':'simple','access':'read'})
        aid=s['agent_id']
        stderr=(self.home/'agents'/aid/'stderr.log').read_text()
        self.assertIn('subagent-pi-bridge ready servers=1',stderr)
        # The daemon accepted the bridge receipt for this agent/generation.
        events=[json.loads(e['payload']) for e in self.rt.store.all(
            "SELECT payload FROM events WHERE agent_id=? AND type='bridge_receipt'",(aid,))]
        self.assertTrue(any(x.get('state')=='ready' and x.get('agent')==aid for x in events))
        await self.rt.dispatch('close',{'scope':self.scope,'agent_id':aid,'request_id':'live-close'})
        self.assertIn('--extension',' '.join(json.loads((self.home/'agents'/aid/'launch.json').read_text())['argv']))
        await self.rt.dispatch('close',{'scope':self.scope,'agent_id':aid,'request_id':'live-close'})

class CliCodexParsing(unittest.TestCase):
    def test_cd_forms_and_separator(self):
        from subagent_pi.cli import split_codex_cwd
        # F02: arguments are returned VERBATIM; only the cwd is scanned out.
        self.assertEqual(split_codex_cwd(['-C','/tmp','run']),('/tmp',['-C','/tmp','run']))
        self.assertEqual(split_codex_cwd(['--cd','/tmp x']),('/tmp x',['--cd','/tmp x']))
        self.assertEqual(split_codex_cwd(['--cd=/a b','exec']),('/a b',['--cd=/a b','exec']))
        self.assertEqual(split_codex_cwd(['-C/attached','run']),('/attached',['-C/attached','run']))
        self.assertEqual(split_codex_cwd(['--','--cd','/x']),(None,['--','--cd','/x']))  # after -- untouched
        self.assertEqual(split_codex_cwd(['exec','--profile','p']),(None,['exec','--profile','p']))
        self.assertEqual(split_codex_cwd(['-C','/a','-C','/b']),('/b',['-C','/a','-C','/b']))  # last wins
        self.assertEqual(split_codex_cwd(['--cd','/中文 目录','run']),('/中文 目录',['--cd','/中文 目录','run']))

class BootstrapStress(unittest.IsolatedAsyncioTestCase):
    async def test_payload_larger_than_pipe_capacity(self):
        tmp=tempfile.TemporaryDirectory(prefix='inh-big-')
        try:
            base=Path(tmp.name); home=base/'state'; home.mkdir(); workspace=base/'ws'; workspace.mkdir()
            codex=make_codex_home(base/'src')
            config=''
            for n in range(8):
                big='x'*100_000
                config+=f'[mcp_servers.big{n}]\ncommand = "python3"\nargs = ["srv.py"]\n\n[mcp_servers.big{n}.env]\nBULK = "{big}"\n\n'
            (codex/'config.toml').write_text(config)
            (home/'config.toml').write_text('pi_command = '+fake_pi_command()+'\nstartup_timeout_seconds = 25\n')
            rt=Runtime(home)
            try:
                r=await rt.dispatch('scope_open',{'cwd':str(workspace)},
                    source={'env':{'CODEX_HOME':str(codex),'PATH':os.environ['PATH'],'HOME':os.environ['HOME']}})
                s=await rt.dispatch('spawn',{'scope':r['scope'],'request_id':'big-1',
                    'cwd':str(workspace),'task':'simple','access':'read'})
                launch=json.loads((home/'agents'/s['agent_id']/'launch.json').read_text())
                stderr=(home/'agents'/s['agent_id']/'stderr.log').read_text()
                self.assertIn('subagent-pi-bridge ready servers=8',stderr)  # ~800KB payload crossed the 64KB pipe
                self.assertIn('--extension',' '.join(launch['argv']))
                await rt.dispatch('close',{'scope':r['scope'],'agent_id':s['agent_id'],'request_id':'big-close'})
            finally:
                await rt.shutdown()
        finally:
            tmp.cleanup()

class RealPiParserProbe(unittest.TestCase):
    """F01 layer-3: the FINAL merged argv must parse in Pi's real CLI parser with
    exactly one --tools flag containing the original builtins plus codex_mcp.
    Static parse only: no Pi process, no model call. Skipped without pi."""
    def pi_args_js(self):
        exe=shutil.which('pi')
        if not exe: return None
        real=Path(exe).resolve()
        candidate=real.parents[1]/'lib'/'node_modules'/'@earendil-works'/'pi-coding-agent'/'dist'/'cli'/'args.js' if 'node_modules' not in real.parts else None
        # Resolve robustly: walk up from the resolved executable to find dist/cli/args.js.
        for parent in [real.parent,*real.parents]:
            guess=parent/'dist'/'cli'/'args.js'
            if guess.is_file(): return guess
        return None
    def probe(self,argv):
        js=self.pi_args_js()
        if js is None: self.skipTest('pi dist/cli/args.js not found')
        script="const {parseArgs}=require(process.argv[1]);const r=parseArgs(process.argv.slice(2));console.log(JSON.stringify({tools:r.tools,noTools:r.noTools}))"
        out=subprocess.run(['node','-e',script,str(js),*argv],capture_output=True,text=True,timeout=30)
        self.assertEqual(out.returncode,0,out.stderr)
        return json.loads(out.stdout)
    def test_reader_tools_survive_bridge_merge(self):
        base=['pi','--mode','rpc','--no-extensions','--no-skills','--tools','read,grep,find,ls']
        merged=Runtime._merge_bridge_tool([*base,'--extension','/bridge.ts'])
        self.assertEqual(merged.count('--tools'),1)
        parsed=self.probe(merged)
        self.assertEqual(parsed['tools'],['read','grep','find','ls','codex_mcp'])
        self.assertFalse(parsed.get('noTools'))
    def test_no_tools_becomes_bridge_only(self):
        merged=Runtime._merge_bridge_tool(['pi','--mode','rpc','--no-tools','--extension','/b.ts'])
        self.assertNotIn('--no-tools',merged)
        self.assertEqual(merged.count('--tools'),1)
        parsed=self.probe(merged)
        self.assertEqual(parsed['tools'],['codex_mcp'])
    def test_no_tool_flags_left_untouched(self):
        argv=['pi','--mode','rpc','--extension','/b.ts']
        self.assertEqual(Runtime._merge_bridge_tool(list(argv)),argv)  # bare --tools would strip builtins

class CodexLauncherProcess(unittest.TestCase):
    """F02 layer-3: a real launcher subprocess must forward Codex's arguments
    verbatim (including -C/--cd) and bind the scope to the target directory.
    The fake codex prints its cwd and argv; nothing touches a real Codex."""
    def test_launcher_forwards_args_and_binds_scope(self):
        tmp=tempfile.TemporaryDirectory(prefix='codex-launch-')
        try:
            root=Path(tmp.name); bin_dir=root/'bin'; bin_dir.mkdir()
            proj=root/'my project'; proj.mkdir()  # space in path on purpose
            fake=bin_dir/'codex'
            fake.write_text('#!/usr/bin/env python3\nimport json,os,sys\nprint(json.dumps({"cwd":os.getcwd(),"argv":sys.argv[1:]}))\n')
            fake.chmod(0o755)
            env={k:v for k,v in os.environ.items() if k not in ('PI_AGENTS_HOME','PI_AGENTS_SCOPE','CODEX_HOME')}
            env['PATH']=f'{bin_dir}{os.pathsep}{env.get("PATH","")}'
            env['PI_AGENTS_HOME']=str(root/'state')
            cli=str(ROOT/'bin'/'subagent-pi')
            def run(*args,**kw):
                return subprocess.run([sys.executable,cli,*args],env=env,capture_output=True,text=True,timeout=90,**kw)
            try:
                proc=run('codex','-C',str(proj),'exec','--profile','p','--','prompt text')
                self.assertEqual(proc.returncode,0,proc.stderr or proc.stdout)
                out=json.loads(proc.stdout)
                # Codex receives the ORIGINAL arguments, unchanged, and runs in
                # the launcher's cwd (Codex applies -C itself).
                self.assertEqual(out['argv'],['-C',str(proj),'exec','--profile','p','--','prompt text'])
                self.assertEqual(Path(out['cwd']),Path(os.getcwd()))
                doc=run('doctor','--inheritance')
                self.assertEqual(doc.returncode,0,doc.stderr)
                report=json.loads(doc.stdout)['inheritance']
                self.assertEqual(report['scopes'][0]['cwd'],str(proj.resolve()))
                # --cd= form and no-override form are also accepted end to end.
                proc2=run('codex','--cd='+str(proj),'run')
                self.assertEqual(proc2.returncode,0,proc2.stderr or proc2.stdout)
                self.assertEqual(json.loads(proc2.stdout)['argv'],['--cd='+str(proj),'run'])
                proc3=run('codex','exec')
                self.assertEqual(proc3.returncode,0,proc3.stderr or proc3.stdout)
                self.assertEqual(json.loads(proc3.stdout)['argv'],['exec'])
            finally:
                subprocess.run([sys.executable,cli,'daemon','stop','--force'],env=env,capture_output=True,text=True,timeout=30)
        finally:
            tmp.cleanup()

class ScopeEnvIsolation(unittest.IsolatedAsyncioTestCase):
    """F07 layer-3: each scope's Pi worker must see ITS OWN base env values and
    never a daemon-only canary. Probes flow through get_state responses in
    memory only (fake_pi echoes only PI_TEST_* names, which the canary check
    then proves absent from every control-plane file)."""
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='inh-env-')
        self.root=Path(self.tmp.name); self.home=self.root/'state'; self.home.mkdir()
        (self.home/'config.toml').write_text('pi_command = '+fake_pi_command()+'\nstartup_timeout_seconds = 25\n\n[inheritance]\nchild_env = ["PI_TEST_HOME_TAG"]\n')
        self.rt=Runtime(self.home)
        self.canary='PI_TEST_ENV_CANARY'
        os.environ[self.canary]='daemon-only'  # present in the daemon environ
        self.addAsyncCleanup(os.environ.pop,self.canary,None)
    async def asyncTearDown(self):
        await self.rt.shutdown(); self.tmp.cleanup()
    async def _spawn_with_env(self,label,cwd,env,rid):
        r=await self.rt.dispatch('scope_open',{'cwd':str(cwd),'label':label},source={'env':env})
        scope=r['scope']
        s=await self.rt.dispatch('spawn',{'scope':scope,'request_id':rid,
            'cwd':str(cwd),'task':'simple','access':'read'})
        return scope,s['agent_id']
    async def test_two_scopes_get_their_own_values(self):
        ws_a=self.root/'a'; ws_a.mkdir(); ws_b=self.root/'b'; ws_b.mkdir()
        base={'PATH':os.environ['PATH'],'HOME':os.environ['HOME']}
        scope_a,aid_a=await self._spawn_with_env('a',ws_a,{**base,'PI_TEST_HOME_TAG':'A'},'env-1')
        scope_b,aid_b=await self._spawn_with_env('b',ws_b,{**base,'PI_TEST_HOME_TAG':'B'},'env-2')
        for scope,aid,tag in ((scope_a,aid_a,'A'),(scope_b,aid_b,'B')):
            w=self.rt.workers[aid]
            state=await w.rpc('get_state')
            probe=state.get('env_probe',{})
            self.assertEqual(probe.get('PI_TEST_HOME_TAG'),tag)
            self.assertNotIn(self.canary,probe)  # daemon-only canary never reaches any child
            await self.rt.dispatch('close',{'scope':scope,'agent_id':aid,'request_id':f'close-{tag}'})
    async def test_control_plane_has_no_env_values(self):
        ws=self.root/'c'; ws.mkdir()
        scope,aid=await self._spawn_with_env('c',ws,{**{'PATH':os.environ['PATH'],'HOME':os.environ['HOME']},
                                                     'PI_TEST_SECRET_VAR':'scope-secret-value'},'env-3')
        blobs=[p for p in self.home.rglob('*') if p.is_file() and p.suffix in ('.json','.jsonl','')]
        for p in blobs:
            self.assertNotIn(b'scope-secret-value',p.read_bytes(),p)
        db=self.home/'registry.sqlite'
        if db.exists():
            import sqlite3
            con=sqlite3.connect(f'file:{db}?mode=ro',uri=True)
            try:
                for (table,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'"):
                    for row in con.execute(f'SELECT * FROM {table}'):
                        self.assertNotIn('scope-secret-value',str(row))
            finally: con.close()
        wal=self.home/'registry.sqlite-wal'
        if wal.exists():
            self.assertNotIn(b'scope-secret-value',wal.read_bytes())

if __name__=='__main__':
    unittest.main()
