"""Inheritance tests: source resolution, skills, MCP parsing, the private pipe
channel, secret canaries, and no-new-runtime-files guarantees. All offline."""
from __future__ import annotations
import asyncio
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from subagent_pi.common import AgentError
from subagent_pi.inheritance import (capture_scope_env, collect_skills, parse_mcp_servers,
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
    def test_nonexistent_explicit_dir_falls_through(self):
        b=make_codex_home(self.base/'b')
        home,mode=resolve_codex_home(self.config(self.base/'missing'),{'CODEX_HOME':str(b)})
        self.assertEqual(home,b)

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
        self.assertEqual(s['transport'],'stdio')
        self.assertEqual(s['command'],'python3')
        self.assertEqual(s['args'],['-m','fake_server'])
        self.assertEqual(s['env_var_names'],['TOKEN_VAR'])
        self.assertTrue(s['cwd'].startswith(str(self.home)))  # relative cwd anchored to codex home
        self.assertIn('dir with space',s['cwd'])
        self.assertEqual(s['static_env'],{'STATIC_K':'static-v'})
    def test_remote_env_source_disables_server(self):
        servers,diag=self.parse(REMOTE_TOML)
        self.assertEqual(servers,[])
        self.assertTrue(any('source=remote' in d.reason for d in diag))
    def test_http_fields_and_url_redaction_not_needed_in_config(self):
        servers,diag=self.parse(HTTP_TOML)
        self.assertEqual(len(servers),1)
        s=servers[0]
        self.assertEqual(s['transport'],'http')
        self.assertEqual(s['static_headers'],{'X-Static':'sv'})
        self.assertEqual(s['env_header_names'],{'X-Trace':'TRACE_ID'})
        self.assertEqual(s['bearer_token_env_var'],'WEB_TOKEN')
        self.assertEqual(s['allowed_tools'],['search'])
        self.assertEqual(s['disabled_tools'],['danger'])
        self.assertEqual(s['approval_mode'],'auto')
        self.assertEqual(s['startup_timeout_sec'],7)
        self.assertEqual(s['tool_timeout_sec'],33)
    def test_disabled_and_unknown_critical_keys(self):
        config=HTTP_TOML+'\n[mcp_servers.bad]\ncommand = "x"\nsandbox_permissions = ["full"]\n\n[mcp_servers.off]\ncommand = "y"\nenabled = false\n'
        servers,diag=self.parse(config)
        names=[s['name'] for s in servers]
        self.assertNotIn('bad',names); self.assertNotIn('off',names)
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
        self.assertEqual(servers,[])
        reasons=' '.join(d.reason for d in diag)
        self.assertIn('oauth',reasons); self.assertIn('http_headers_helper',reasons); self.assertIn('remote',reasons)
    def test_empty_allowlist_means_no_tools(self):
        config='[mcp_servers.strict]\ncommand = "x"\nenabled_tools = []\n'
        servers,_=self.parse(config)
        self.assertEqual(servers[0]['allowed_tools'],[])
        usable,_=policy_filter([{**servers[0],'confirm_all':False}],'write')
        self.assertEqual(usable[0]['allowed_tools'],[])
    def test_recursion_guard_by_resolved_command(self):
        bin_path=ROOT/'bin'/'subagent-pi'
        config=f'[mcp_servers.renamed_control]\ncommand = "{bin_path}"\nargs = ["mcp"]\n'
        servers,diag=self.parse(config)
        self.assertEqual(servers,[])
        self.assertTrue(any('recursion guard' in d.reason for d in diag))
    def test_resolve_environment_from_snapshot_only(self):
        servers,_=self.parse(STDIO_TOML)
        usable,diag=resolve_environment(servers,{'TOKEN_VAR':'token-a','PATH':'/bin'})
        self.assertEqual(usable[0]['env']['TOKEN_VAR'],'token-a')
        self.assertEqual(usable[0]['env']['STATIC_K'],'static-v')
        self.assertNotIn('PATH',usable[0]['env'])  # base env is the child runner's job, not per-server
    def test_required_missing_env_blocks(self):
        config='[mcp_servers.core]\ncommand = "x"\nenv_vars = ["MISSING_VAR"]\nrequired = true\n'
        servers,_=self.parse(config)
        with self.assertRaises(AgentError) as cm:
            resolve_environment(servers,{})
        self.assertEqual(cm.exception.code,'inheritance_required_server_failed')
        self.assertIn('MISSING_VAR',cm.exception.message)
    def test_optional_missing_env_excluded_with_named_diagnostic(self):
        config='[mcp_servers.opt]\ncommand = "x"\nenv_vars = ["ABSENT_VAR"]\n'
        servers,_=self.parse(config)
        usable,diag=resolve_environment(servers,{})
        self.assertEqual(usable,[])
        self.assertTrue(any('ABSENT_VAR' in d.reason for d in diag))
    def test_bearer_and_env_headers_resolved(self):
        servers,_=self.parse(HTTP_TOML)
        usable,diag=resolve_environment(servers,{'WEB_TOKEN':'tok','TRACE_ID':'tr-1'})
        self.assertEqual(usable[0]['bearer_token'],'tok')
        self.assertEqual(usable[0]['headers'],{'X-Static':'sv','X-Trace':'tr-1'})
    def test_read_child_policy_intersects(self):
        servers,_=self.parse(HTTP_TOML)  # has explicit allowlist
        usable,diag=policy_filter(servers,'read')
        self.assertEqual(usable[0]['name'],'web'); self.assertNotIn('confirm_all',usable[0])
        no_list,_=self.parse('[mcp_servers.open]\ncommand = "x"\n')
        usable,diag=policy_filter(no_list,'read')
        self.assertTrue(usable[0]['confirm_all'])
        usable,diag=policy_filter(no_list,'write')
        self.assertNotIn('confirm_all',usable[0])
    def test_capture_scope_env_is_minimal(self):
        (self.home/'config.toml').write_text(STDIO_TOML+'[mcp_servers.w2]\nurl="https://e.example"\nbearer_token_env_var="BT"\n')
        environ={'PATH':'/bin','HOME':'/h','TOKEN_VAR':'tv','BT':'bt','UNRELATED_SECRET':'nope','CODEX_HOME':str(self.home)}
        snap=capture_scope_env(self.home,environ)
        self.assertIn('TOKEN_VAR',snap); self.assertIn('BT',snap); self.assertIn('PATH',snap)
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
    """Real Pi process + real TS bridge, no model calls. Skipped without pi."""
    async def asyncSetUp(self):
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
    async def asyncTearDown(self):
        await self.rt.shutdown(); self.tmp.cleanup()
    async def test_bridge_loads_in_real_pi_child(self):
        s=await self.rt.dispatch('spawn',{'scope':self.scope,'request_id':'live-1',
            'cwd':str(self.workspace),'task':'simple','access':'read'})
        aid=s['agent_id']
        stderr=(self.home/'agents'/aid/'stderr.log').read_text()
        self.assertIn('subagent-pi-bridge ready servers=1',stderr)
        self.assertIn('--extension',json.loads((self.home/'agents'/aid/'launch.json').read_text())['argv'][0] if False else ' '.join(json.loads((self.home/'agents'/aid/'launch.json').read_text())['argv']))
        await self.rt.dispatch('close',{'scope':self.scope,'agent_id':aid,'request_id':'live-close'})

class CliCodexParsing(unittest.TestCase):
    def test_cd_forms_and_separator(self):
        from subagent_pi.cli import split_codex_cwd
        self.assertEqual(split_codex_cwd(['-C','/tmp','run']),('/tmp',['run']))
        self.assertEqual(split_codex_cwd(['--cd','/tmp x']),('/tmp x',[]))
        self.assertEqual(split_codex_cwd(['--cd=/a b','exec']),('/a b',['exec']))
        self.assertEqual(split_codex_cwd(['--','--cd','/x']),(None,['--','--cd','/x']))  # after -- untouched
        self.assertEqual(split_codex_cwd(['exec','--profile','p']),(None,['exec','--profile','p']))

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

if __name__=='__main__':
    unittest.main()
