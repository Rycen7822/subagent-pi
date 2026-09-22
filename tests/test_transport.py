from __future__ import annotations
import asyncio
import contextlib
import json
import os
import re
from pathlib import Path
import signal
import shutil
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from subagent_pi.client import request
from subagent_pi import PROTOCOL_VERSION
from subagent_pi.common import AgentError, group_members, socket_path

class McpHarness:
    """MCP stdio boundary over a real daemon, shared by the fake-Pi IPC tests and
    the real-Pi lifecycle tests (which replace pi_command, HOME and the agent dir
    themselves, then run the daemon at the same protocol boundary)."""
    READ_TIMEOUT=8
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='subagent-pi-ipc-')
        self.root=Path(self.tmp.name); self.home=self.root/'state'; self.home.mkdir()
        self.workspace=self.root/'workspace'; self.workspace.mkdir()
        (self.home/'config.toml').write_text('pi_command = '+json.dumps([sys.executable,str(ROOT/'tests/fake_pi.py')])+'\nrpc_timeout_seconds=8\n[inheritance]\nenabled = false\n')
        self.mcp=None; self.stderr_task=None; self.reqid=0
    async def asyncTearDown(self):
        if self.mcp and self.mcp.returncode is None:
            self.mcp.stdin.close()
            try: await asyncio.wait_for(self.mcp.wait(),3)
            except asyncio.TimeoutError: self.mcp.kill(); await self.mcp.wait()
        if self.stderr_task: await asyncio.gather(self.stderr_task,return_exceptions=True)
        with contextlib.suppress(AgentError): await request(self.home,'shutdown',{'force':True},autostart=False)
        until=asyncio.get_running_loop().time()+8
        while socket_path(self.home).exists() and asyncio.get_running_loop().time()<until: await asyncio.sleep(.05)
        self.tmp.cleanup()
    async def open_scope(self):
        return (await request(self.home,'scope_open',{'cwd':str(self.workspace)}))['scope']
    async def spawn(self,sid,task='delay=0.2|transport'):
        return await request(self.home,'spawn',{'scope':sid,'request_id':'spawn-'+str(self.reqid),'cwd':str(self.workspace),'task':task,'access':'read'})
    async def start_mcp(self):
        env=os.environ.copy(); env['PI_AGENTS_HOME']=str(self.home); env.pop('PI_AGENTS_SCOPE',None)
        env.pop('CODEX_THREAD_ID',None); env.pop('CODEX_SESSION_ID',None)
        env.update(getattr(self,'mcp_env',{}))
        self.mcp=await asyncio.create_subprocess_exec(sys.executable,str(ROOT/'bin/subagent-pi'),'mcp',
            stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,env=env,limit=8*1024*1024)
        self.stderr_task=asyncio.create_task(self.mcp.stderr.read())
    async def send(self,method,params=None,rid=None):
        self.reqid+=1; rid=rid or self.reqid
        self.mcp.stdin.write((json.dumps({'jsonrpc':'2.0','id':rid,'method':method,'params':params or {}})+'\n').encode()); await self.mcp.stdin.drain(); return rid
    async def receive(self): return json.loads(await asyncio.wait_for(self.mcp.stdout.readline(),self.READ_TIMEOUT))
    async def rpc(self,method,params=None):
        rid=await self.send(method,params); value=await self.receive(); self.assertEqual(value['id'],rid); return value
    async def initialize(self):
        await self.start_mcp(); return await self.rpc('initialize',{'protocolVersion':'2025-06-18','capabilities':{},'clientInfo':{'name':'test','version':'1'}})
    def unpack(self,response): return json.loads(response['result']['content'][0]['text'])
    async def tool(self,name,args):
        response=await self.rpc('tools/call',{'name':name,'arguments':args})
        value=self.unpack(response)
        self.assertFalse(response['result'].get('isError'),value)
        return value


class TransportTests(McpHarness, unittest.IsolatedAsyncioTestCase):
    async def check_blocked_stdin_cleanup(self,by_idle):
        config=self.home/'config.toml'
        config.write_text(config.read_text().replace('rpc_timeout_seconds=8','rpc_timeout_seconds=1'))
        sid=await self.open_scope()
        run=await request(self.home,'spawn',{'scope':sid,'request_id':'blocked','task':'model=silent|delay=120|work' if by_idle else 'UI_CONFIRM',
            'access':'read','idle_timeout_seconds':8 if by_idle else 60})
        if not by_idle:
            result=await request(self.home,'wait',{'scope':sid,'run_ids':[run['run_id']],'timeout_seconds':4})
            self.assertEqual(result['reason'],'needs_input')
        owner=json.loads((self.home/'agents'/run['agent_id']/'owner.json').read_text())
        pgid=owner['guard_pid']; os.killpg(pgid,signal.SIGSTOP)
        try:
            # Three valid frames exceed the pipe + asyncio write-buffer capacity.
            for i in range(3):
                params={'scope':sid,'agent_id':run['agent_id'],'request_id':f'blocked-{i}',
                        'mode':'steer','message':'x'*60000}
                with self.assertRaises(AgentError) as error:
                    await request(self.home,'send',params,timeout=3)
                self.assertEqual(error.exception.code,'rpc_timeout')
            # UI replies use raw(), so they need the same bounded write guarantee.
            if not by_idle:
                with self.assertRaises(AgentError) as error:
                    await request(self.home,'answer',{'scope':sid,'agent_id':run['agent_id'],'request_id':'answer',
                        'ui_request_id':'ui-1','answer':True},timeout=3)
                self.assertEqual(error.exception.code,'rpc_timeout')
            if by_idle:
                # A pending question makes wait return immediately; the loop below
                # observes terminal state, rather than pretending a question is done.
                until=asyncio.get_running_loop().time()+8
                while asyncio.get_running_loop().time()<until:
                    state=(await request(self.home,'list',{'scope':sid}))['agents'][0]['state']
                    if state=='dormant': break
                    await asyncio.sleep(.05)
                self.assertEqual(state,'dormant')
            else:
                closed=await request(self.home,'close',{'scope':sid,'agent_id':run['agent_id'],
                                                       'request_id':'close'},timeout=5)
                self.assertEqual(closed['cleanup'],'verified')
            done=await request(self.home,'wait',{'scope':sid,'run_ids':[run['run_id']],'timeout_seconds':0})
            self.assertEqual(done['runs'][0]['state'],'timed_out' if by_idle else 'interrupted')
            self.assertEqual(group_members(pgid),[])
            # Retrying an uncertain mutation must read its stored error, not send it again.
            with self.assertRaises(AgentError) as replay:
                await request(self.home,'send',params,timeout=3)
            self.assertEqual(replay.exception.code,'rpc_timeout')
        finally:
            with contextlib.suppress(ProcessLookupError): os.killpg(pgid,signal.SIGCONT)

    async def test_backpressured_child_can_be_closed_after_rpc_timeout(self):
        await self.check_blocked_stdin_cleanup(False)

    async def test_backpressured_model_is_stopped_by_inactivity(self):
        await self.check_blocked_stdin_cleanup(True)

    async def test_long_wait_wakes_on_later_completion_or_question(self):
        await self.initialize()
        await self.tool('pi_context',{'cwd':str(self.workspace)})
        for task,reason in [('done','completed'),('UI_CONFIRM','needs_input')]:
            with self.subTest(task=task):
                run=await self.tool('pi_spawn_agent',{'task':'delay=0.5|'+task,'access':'read','request_id':task})
                args={'run_ids':[run['run_id']]}
                if task=='UI_CONFIRM': args['timeout_seconds']=3600
                waitid=await self.send('tools/call',{'name':'pi_wait_agent','arguments':args})
                pingid=await self.send('ping')
                responses={}
                for _ in range(2):
                    response=await self.receive(); responses[response['id']]=response
                self.assertEqual(responses[pingid]['result'],{})
                result=self.unpack(responses[waitid])
                self.assertEqual(result['reason'],reason)
                self.assertFalse(result['timed_out'])
                if task=='UI_CONFIRM': self.assertEqual(result['questions'][0]['method'],'confirm')
                else: self.assertIn('Completed: done',result['runs'][0]['result']['text'])
                await self.tool('pi_close_agent',{'agent_id':run['agent_id'],'request_id':'close-'+task})

    async def test_ipc_autostart_single_daemon(self):
        a,b=await asyncio.gather(request(self.home,'ping',{}),request(self.home,'ping',{}))
        self.assertEqual(a['pid'],b['pid'])
    async def test_cli_spawn_then_mcp_management_same_ledger(self):
        sid=await self.open_scope(); await self.initialize()
        env=os.environ.copy(); env['PI_AGENTS_HOME']=str(self.home)
        proc=await asyncio.create_subprocess_exec(sys.executable,str(ROOT/'bin/subagent-pi'),'spawn','--scope',sid,'--cwd',str(self.workspace),'--access','read','--task','CLI work','--request-id','cli-work',stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,env=env)
        stdout,stderr=await proc.communicate(); self.assertEqual(proc.returncode,0,stderr.decode())
        a=json.loads(stdout)
        listing=await self.tool('pi_list_agents',{'scope':sid})
        self.assertEqual(listing['agents'][0]['id'],a['agent_id'])
        done=await self.tool('pi_wait_agent',{'scope':sid,'run_ids':[a['run_id']],'timeout_seconds':4})
        self.assertFalse(done['timed_out'])
    async def test_mcp_initialize_and_list(self):
        init=await self.initialize(); self.assertEqual(init['result']['protocolVersion'],'2025-06-18')
        result=await self.rpc('tools/list'); tools=result['result']['tools']
        self.assertEqual(len(tools),11)
        self.assertTrue(all('_op' not in t for t in tools))
    async def test_named_agents_keep_labels_across_runs_and_results(self):
        await self.initialize()
        scope=await self.tool('pi_context',{'cwd':str(self.workspace)})
        agents=[]
        for index,name in enumerate(('git-stats-fix','测试审查')):
            spawned=await self.tool('pi_spawn_agent',{'name':name,'task':name,'access':'read','request_id':f'named-{index}'})
            self.assertEqual(spawned['name'],name); agents.append(spawned)
        expected={a['agent_id']:a['name'] for a in agents}
        done=await self.tool('pi_wait_agent',{'run_ids':[a['run_id'] for a in agents],'mode':'all','timeout_seconds':4})
        self.assertEqual({r['agent_id']:r['name'] for r in done['runs']},expected)
        for a in agents:
            result=await self.tool('pi_agent_result',{'run_id':a['run_id']})
            self.assertEqual(result['run']['name'],a['name'])
            self.assertFalse(result['acknowledged'])
        listing=await self.tool('pi_list_agents',{})
        self.assertEqual({r['agent_id']:r['name'] for r in listing['outstanding']['runs']},expected)
        resumed=await self.tool('pi_context',{'cwd':str(self.workspace),'scope':scope['scope']})
        self.assertEqual({r['agent_id']:r['name'] for r in resumed['outstanding']['runs']},expected)
        conflict=await self.rpc('tools/call',{'name':'pi_spawn_agent','arguments':{'name':agents[0]['name'],
            'task':'duplicate','access':'read','request_id':'duplicate'}})
        self.assertEqual(self.unpack(conflict)['error']['code'],'name_conflict')
        follow=await self.tool('pi_send_input',{'agent_id':agents[0]['agent_id'],'message':'follow-up',
            'mode':'send','request_id':'next'})
        self.assertEqual(follow['name'],agents[0]['name'])
        self.assertNotIn('execution',follow)
        next_result=await self.tool('pi_wait_agent',{'run_ids':[follow['run_id']],'timeout_seconds':4})
        self.assertNotEqual(follow['run_id'],agents[0]['run_id'])
        self.assertEqual(next_result['runs'][0]['name'],agents[0]['name'])

    async def test_continuation_receipt_is_not_a_consumption_guarantee(self):
        await self.initialize()
        await self.tool('pi_context',{'cwd':str(self.workspace)})
        agent=await self.tool('pi_spawn_agent',{'name':'reviewer','task':'delay=1|NO_CONSUME',
            'access':'read','request_id':'spawn'})
        args={'agent_id':agent['agent_id'],'message':'Inspect only','mode':'steer','request_id':'correction'}
        receipt=await self.tool('pi_send_input',args)
        self.assertEqual(receipt['name'],'reviewer')
        self.assertEqual(receipt['run_id'],agent['run_id'])
        self.assertEqual(receipt['execution'],'after_current_sdk_call')
        self.assertEqual(receipt['delivery'],'queued')
        replay=await self.tool('pi_send_input',args)
        self.assertTrue(replay['replayed']); self.assertEqual(replay['receipt_id'],receipt['receipt_id'])
        follow=await self.tool('pi_send_input',{'agent_id':agent['agent_id'],'message':'Next task',
            'mode':'follow_up','request_id':'next'})
        self.assertEqual(follow['name'],'reviewer'); self.assertNotEqual(follow['run_id'],agent['run_id'])
        self.assertEqual(follow['state'],'queued'); self.assertNotIn('execution',follow)
        done=await self.tool('pi_wait_agent',{'run_ids':[agent['run_id'],follow['run_id']],
            'mode':'all','timeout_seconds':4})
        self.assertFalse(done['timed_out'])
        inspected=await self.tool('pi_inspect_agent',{'agent_id':agent['agent_id']})
        matching=[r for r in inspected['receipts'] if r['id']==receipt['receipt_id']]
        self.assertEqual([r['state'] for r in matching],['not_consumed'])

    async def test_mcp_scope_reused_per_workspace(self):
        await self.initialize()
        a=await self.tool('pi_context',{'cwd':str(self.workspace)})
        b=await self.tool('pi_context',{'cwd':str(self.workspace)})
        self.assertEqual(a['scope'],b['scope'])
    async def test_bound_scope_defaults_and_explicit_resume_are_connection_local(self):
        await self.initialize()
        first=await self.tool('pi_context',{'cwd':str(self.workspace)})
        run=await self.tool('pi_spawn_agent',{'task':'bound scope','access':'read','request_id':'bound-spawn'})
        self.assertEqual(run['scope'],first['scope']); self.assertEqual(run['cwd'],str(self.workspace))
        self.assertEqual(run['name'],run['agent_id'])
        retry=await self.tool('pi_spawn_agent',{'scope':first['scope'],'cwd':str(self.workspace),'task':'bound scope','access':'read','request_id':'bound-spawn'})
        self.assertEqual(retry['run_id'],run['run_id'])
        done=await self.tool('pi_wait_agent',{'run_ids':[run['run_id']],'timeout_seconds':4})
        result=done['runs'][0]['result']
        self.assertEqual(result['text'],'Completed: bound scope')
        self.assertFalse(result['has_more']); self.assertFalse(done['runs'][0]['ack'])
        await self.tool('pi_ack_result',{'request_id':'ack-bound','run_id':run['run_id'],'result_sha256':result['result_sha256']})
        await self.tool('pi_close_agent',{'agent_id':run['agent_id'],'request_id':'stop-bound'})
        self.mcp.stdin.close(); await self.mcp.wait(); await self.stderr_task
        await self.initialize()
        unbound=await self.rpc('tools/call',{'name':'pi_list_agents','arguments':{}})
        self.assertTrue(unbound['result']['isError'])
        fresh=await self.tool('pi_context',{'cwd':str(self.workspace)})
        self.assertNotEqual(fresh['scope'],first['scope'])
        resumed=await self.tool('pi_context',{'cwd':str(self.workspace),'scope':first['scope']})
        self.assertEqual(resumed['scope'],first['scope'])
        self.assertEqual((await self.tool('pi_list_agents',{}))['agents'][0]['id'],run['agent_id'])

    async def test_wait_all_returns_questions_and_stops_without_waiting_for_other_work(self):
        await self.initialize()
        await self.tool('pi_context',{'cwd':str(self.workspace)})
        slow=await self.tool('pi_spawn_agent',{'task':'delay=30|slow','access':'read','request_id':'slow'})
        asking=await self.tool('pi_spawn_agent',{'name':'permission-check','task':'UI_CONFIRM','access':'read','request_id':'ask'})
        remaining=await self.tool('pi_spawn_agent',{'task':'delay=30|remaining','access':'read','request_id':'remaining'})
        ids=[slow['run_id'],asking['run_id'],remaining['run_id']]
        alert=await self.tool('pi_wait_agent',{'run_ids':ids,'mode':'all','timeout_seconds':3600})
        self.assertEqual(alert['reason'],'needs_input'); self.assertFalse(alert['timed_out'])
        question=alert['questions'][0]
        self.assertEqual(question['agent_id'],asking['agent_id']); self.assertEqual(question['method'],'confirm')
        self.assertEqual(question['name'],'permission-check')
        await self.tool('pi_answer_agent',{'agent_id':asking['agent_id'],'ui_request_id':question['id'],'answer':False,'request_id':'answer'})
        await self.tool('pi_wait_agent',{'run_ids':[asking['run_id']],'timeout_seconds':4})
        stopped=await self.tool('pi_close_agent',{'agent_id':slow['agent_id'],'request_id':'stop'})
        self.assertEqual(stopped['cleanup'],'verified')
        alert=await self.tool('pi_wait_agent',{'run_ids':ids,'mode':'all','timeout_seconds':3600})
        self.assertEqual(alert['reason'],'failed_or_stopped')
        self.assertIn(alert['runs'][0]['state'],('cancelled','interrupted'))
        self.assertEqual(alert['runs'][2]['state'],'running')

    async def test_mcp_invalid_arguments_are_tool_errors(self):
        await self.initialize()
        r=await self.rpc('tools/call',{'name':'pi_spawn_agent','arguments':{'task':'missing fields'}})
        self.assertTrue(r['result']['isError'])
        self.assertIn('invalid_argument',r['result']['content'][0]['text'])
    async def test_mcp_protocol_errors_and_ping(self):
        await self.initialize()
        self.mcp.stdin.write(b'not json\n'); await self.mcp.stdin.drain()
        e=await self.receive(); self.assertEqual(e['error']['code'],-32700)
        r=await self.rpc('ping'); self.assertEqual(r['result'],{})
        r=await self.rpc('no/such/method'); self.assertEqual(r['error']['code'],-32601)
    async def test_mcp_cancel_wait_then_ping_without_killing_agent(self):
        await self.initialize()
        sid=(await self.tool('pi_context',{'cwd':str(self.workspace)}))['scope']
        a=await self.tool('pi_spawn_agent',{'scope':sid,'cwd':str(self.workspace),'task':'delay=1|keep-running','access':'read','request_id':'cancel-spawn'})
        waitid=await self.send('tools/call',{'name':'pi_wait_agent','arguments':{'scope':sid,'run_ids':[a['run_id']],'timeout_seconds':3600}})
        await asyncio.sleep(.05)
        self.mcp.stdin.write((json.dumps({'jsonrpc':'2.0','method':'notifications/cancelled','params':{'requestId':waitid}})+'\n').encode()); await self.mcp.stdin.drain()
        self.assertEqual((await self.rpc('ping'))['result'],{})
        state=await self.tool('pi_list_agents',{'scope':sid})
        self.assertEqual(state['agents'][0]['state'],'running')
        done=await self.tool('pi_wait_agent',{'scope':sid,'run_ids':[a['run_id']],'timeout_seconds':4})
        self.assertEqual(done['runs'][0]['state'],'completed')
    async def test_mcp_exit_does_not_stop_worker(self):
        await self.initialize()
        sid=(await self.tool('pi_context',{'cwd':str(self.workspace)}))['scope']
        a=await self.tool('pi_spawn_agent',{'scope':sid,'cwd':str(self.workspace),'task':'delay=0.4|survive-adapter','access':'read','request_id':'survive'})
        self.mcp.stdin.close(); await self.mcp.wait()
        done=await request(self.home,'wait',{'scope':sid,'run_ids':[a['run_id']],'timeout_seconds':4})
        self.assertEqual(done['runs'][0]['state'],'completed')
    async def test_durable_unacknowledged_result_after_daemon_restart(self):
        sid=await self.open_scope(); a=await self.spawn(sid)
        await request(self.home,'wait',{'scope':sid,'run_ids':[a['run_id']],'timeout_seconds':4})
        first=await request(self.home,'result',{'scope':sid,'run_id':a['run_id']})
        await request(self.home,'shutdown',{'force':True})
        until=asyncio.get_running_loop().time()+8
        while socket_path(self.home).exists() and asyncio.get_running_loop().time()<until: await asyncio.sleep(.05)
        second=await request(self.home,'result',{'scope':sid,'run_id':a['run_id']})
        self.assertEqual(first['result_sha256'],second['result_sha256']); self.assertFalse(second['acknowledged'])
        listing=await request(self.home,'list',{'scope':sid})
        self.assertEqual(listing['outstanding']['total'],1)
    async def test_ipc_disconnected_mutation_still_has_idempotent_result(self):
        from subagent_pi.common import dumps
        sid=await self.open_scope()
        reader,writer=await asyncio.open_unix_connection(str(socket_path(self.home)))
        params={'scope':sid,'request_id':'lost-reply','cwd':str(self.workspace),'task':'hello','access':'read'}
        writer.write((dumps({'v':PROTOCOL_VERSION,'op':'spawn','params':params})+'\n').encode()); await writer.drain(); writer.close(); await writer.wait_closed()
        await asyncio.sleep(.5)
        result=await request(self.home,'spawn',params)
        self.assertTrue(result['replayed'])
        self.assertEqual((await request(self.home,'list',{'scope':sid}))['total'],1)

    async def test_live_orphan_requires_close_before_respawn(self):
        (self.home/'config.toml').write_text('pi_command = '+json.dumps([sys.executable,str(ROOT/'tests/fake_pi.py'),'--hold-eof'])+'\nrpc_timeout_seconds=8\n')
        sid=await self.open_scope(); a=await self.spawn(sid,'delay=30|orphan')
        pid=(await request(self.home,'ping',{}))['pid']
        os.kill(pid,signal.SIGKILL)
        await asyncio.sleep(.3)
        listing=await request(self.home,'list',{'scope':sid})
        self.assertEqual(listing['agents'][0]['state'],'orphaned')
        with self.assertRaises(AgentError) as cm:
            await request(self.home,'respawn',{'scope':sid,'agent_id':a['agent_id'],'request_id':'unsafe-respawn'})
        self.assertEqual(cm.exception.code,'orphaned_worker')
        closed=await request(self.home,'close',{'scope':sid,'agent_id':a['agent_id'],'request_id':'reap'})
        self.assertEqual(closed['cleanup'],'verified')
        revived=await request(self.home,'respawn',{'scope':sid,'agent_id':a['agent_id'],'request_id':'revive','message':'recovered'})
        self.assertEqual(revived['generation'],2)
        result=await request(self.home,'wait',{'scope':sid,'run_ids':[revived['run_id']],'timeout_seconds':4})
        self.assertEqual(result['runs'][0]['state'],'completed')

    async def test_base_env_survives_daemon_restart(self):
        """The scope's non-secret base env must outlive the daemon, or a worker
        booted on an existing scope starts with an empty environment (no PATH)."""
        from subagent_pi.runtime import Runtime
        env_file=self.root/'child-env.json'
        pi_cmd='pi_command = '+json.dumps([sys.executable,str(ROOT/'tests/fake_pi.py')])
        env_cfg=('PI_TEST_PROBE_FILE = '+json.dumps(str(env_file))+'\n'
                 'PI_TEST_PROBE_CMD = "true"\n')
        (self.home/'config.toml').write_text(
            pi_cmd+'\nrpc_timeout_seconds=8\nstartup_timeout_seconds=15\n[inheritance]\nenabled = false\n'
            '[profiles.default.env]\n'+env_cfg+'[profiles.reader.env]\n'+env_cfg)

        rt=Runtime(self.home)
        sid=(await rt.dispatch('scope_open',{'cwd':str(self.workspace)},
             {'env':{'PATH':os.environ['PATH'],'HOME':os.environ['HOME']}}))['scope']
        spawned=await rt.dispatch('spawn',{'scope':sid,'request_id':'env-1','cwd':str(self.workspace),
                                           'task':'delay=0.1|warm','access':'read'})
        first=json.loads(env_file.read_text())
        self.assertTrue(first['path'],'baseline: first boot must have PATH')
        self.assertEqual(first['rc'],0,'baseline: first boot could not run a PATH-discovered command')
        await rt.dispatch('close',{'scope':sid,'agent_id':spawned['agent_id'],'request_id':'env-2'})
        await rt.shutdown()

        env_file.unlink()
        rt2=Runtime(self.home)                 # fresh daemon over the same ledger
        revived=await rt2.dispatch('respawn',{'scope':sid,'agent_id':spawned['agent_id'],'request_id':'env-3'})
        await rt2.dispatch('close',{'scope':sid,'agent_id':spawned['agent_id'],'request_id':'env-4'})
        await rt2.shutdown()
        second=json.loads(env_file.read_text())
        self.assertTrue(second['path'],'respawned worker lost PATH across a daemon restart')
        self.assertEqual(second['rc'],0,'respawned worker could not run a PATH-discovered command')
        self.assertEqual(revived['generation'],2)

    async def test_restart_persists_base_env_but_never_secrets(self):
        """The restart fallback stores base keys only; a bound secret stays in
        memory and must not appear in the ledger."""
        from subagent_pi.runtime import Runtime
        (self.home/'config.toml').write_text('pi_command = '+json.dumps([sys.executable,str(ROOT/'tests/fake_pi.py')])+
            '\nrpc_timeout_seconds=8\nstartup_timeout_seconds=15\n[inheritance]\nenabled = true\nskills = false\nmcp = false\n')
        rt=Runtime(self.home)
        sid=(await rt.dispatch('scope_open',{'cwd':str(self.workspace)},
             {'env':{'PATH':'/bin','SECRET_CANARY':'sk-do-not-persist'}}))['scope']
        stored=rt.store.scope(sid)['base_env']
        self.assertIn('PATH',stored)
        self.assertNotIn('SECRET_CANARY',stored)
        self.assertNotIn(b'sk-do-not-persist',(self.home/'registry.sqlite').read_bytes())
        await rt.shutdown()

class LivePiLifecycleTests(McpHarness, unittest.IsolatedAsyncioTestCase):
    """Real Pi plus an offline mock provider through the real MCP boundary.

    Gated by SUBAGENT_PI_LIVE_PI=1 and never part of the default suite. The mock
    provider replaces global fetch with a thrower, so a model request cannot be
    made; Pi, its agent dir and HOME are all isolated under the test root.
    SUBAGENT_PI_LIVE_PI_BIN selects an isolated Pi install (default: PATH).

    Uses the plugin-owned SDK child against an unmodified Pi 0.87 installation.
    """
    BIN=os.environ.get('SUBAGENT_PI_LIVE_PI_BIN','pi')
    BEFORE_SETTLE='PI_MOCK_BEFORE_SETTLE'
    READ_TIMEOUT=45

    async def asyncSetUp(self):
        if os.environ.get('SUBAGENT_PI_LIVE_PI')!='1':
            raise unittest.SkipTest('set SUBAGENT_PI_LIVE_PI=1 to run the real-Pi check; default suite never launches Pi')
        if not shutil.which(self.BIN): raise unittest.SkipTest(f'{self.BIN} not available')
        await super().asyncSetUp()
        self.pi_home=self.root/'userhome'; self.pi_home.mkdir()
        self.pi_agent=self.root/'agent'; self.pi_agent.mkdir()
        (self.pi_agent/'settings.json').write_text(json.dumps({'compaction':{'enabled':False},'retry':{'enabled':False}}))
        self.saved_env={k:os.environ.get(k) for k in ('HOME','PI_CODING_AGENT_DIR')}
        os.environ['HOME']=str(self.pi_home); os.environ['PI_CODING_AGENT_DIR']=str(self.pi_agent)

    async def asyncTearDown(self):
        await super().asyncTearDown()
        for key,value in self.saved_env.items():
            if value is None: os.environ.pop(key,None)
            else: os.environ[key]=value

    def write_config(self,**profile_env):
        knobs={'PI_OFFLINE':'"1"','PI_SKIP_VERSION_CHECK':'"1"','PI_TELEMETRY':'"0"',**profile_env}
        (self.home/'config.toml').write_text(
            'pi_command = '+json.dumps([self.BIN])+'\nrpc_timeout_seconds=20\nstartup_timeout_seconds=60\n'
            '[inheritance]\nenabled = false\n'
            '[profiles.reader]\nprovider = "pi-mock-offline"\nmodel = "mock"\nextensions = '
            +json.dumps([str(ROOT/'tests/pi_mock_provider.ts')])+'\n'
            '[profiles.reader.env]\n'+''.join(f'{k} = {v}\n' for k,v in knobs.items()))

    async def lifecycle(self):
        """Spawn a reader, queue one follow-up, wait for both, return the run
        outputs plus the child's stderr (the mock's boundary marker)."""
        await self.initialize()
        scope=(await self.tool('pi_context',{'cwd':str(self.workspace)}))['scope']
        a=await self.tool('pi_spawn_agent',{'scope':scope,'cwd':str(self.workspace),'access':'read',
            'task':'offline first','request_id':'live-spawn'})
        q=await self.tool('pi_send_input',{'scope':scope,'agent_id':a['agent_id'],'mode':'follow_up',
            'message':'offline second','request_id':'live-follow'})
        done=await self.tool('pi_wait_agent',{'scope':scope,'run_ids':[a['run_id'],q['run_id']],
            'timeout_seconds':8,'mode':'all'})
        stderr=(self.home/'agents'/a['agent_id']/'stderr.log').read_text()
        return scope,a,q,done,stderr

    async def results(self,scope,runs):
        return [(await self.tool('pi_agent_result',{'scope':scope,'run_id':r['id']}))['text'] for r in runs]

    async def payloads(self,scope,runs):
        return [await self.tool('pi_agent_result',{'scope':scope,'run_id':r['id']}) for r in runs]

    async def wait_marker(self,path,text,count=1,timeout=15.0):
        """Bounded handshake on a child-side marker; never a fixed sleep."""
        loop=asyncio.get_running_loop(); end=loop.time()+timeout
        while loop.time()<end:
            if path.exists() and path.read_text().count(text)>=count: return path.read_text()
            await asyncio.sleep(.05)
        raise AssertionError(f'{text!r} x{count} not observed in {path} within {timeout}s')

    async def test_model_question_reaches_wait_and_answer_returns_to_the_same_task(self):
        self.write_config(PI_MOCK_ASK_PARENT='"1"',PI_MOCK_CONTEXT='"1"')
        await self.initialize(); await self.tool('pi_context',{'cwd':str(self.workspace)})
        spawned=await self.tool('pi_spawn_agent',{'task':'ask before proceeding','request_id':'ask','access':'read'})
        alert=await self.tool('pi_wait_agent',{'run_ids':[spawned['run_id']],'mode':'all','timeout_seconds':8})
        self.assertEqual(alert['reason'],'needs_input',alert)
        question=alert['questions'][0]
        self.assertEqual(question['title'],'Which branch should I use?')
        self.assertEqual(question['run_id'],spawned['run_id'])
        await self.tool('pi_answer_agent',{'agent_id':spawned['agent_id'],'request_id':'answer',
            'ui_request_id':question['id'],'answer':'Use feature/parent-answer'})
        done=await self.tool('pi_wait_agent',{'run_ids':[spawned['run_id']],'timeout_seconds':8})
        self.assertEqual(done['reason'],'completed'); self.assertEqual(done['runs'][0]['result']['text'],'MOCK_REPLY_2')
        trace=(self.home/'agents'/spawned['agent_id']/'stderr.log').read_text()
        self.assertIn('Use feature/parent-answer',trace)

    async def test_provider_failure_returns_to_parent_wait(self):
        self.write_config(PI_MOCK_FAIL='"1"')
        await self.initialize(); await self.tool('pi_context',{'cwd':str(self.workspace)})
        spawned=await self.tool('pi_spawn_agent',{'task':'fail offline','request_id':'fail','access':'read'})
        done=await self.tool('pi_wait_agent',{'run_ids':[spawned['run_id']],'mode':'all','timeout_seconds':8})
        self.assertEqual(done['reason'],'failed_or_stopped'); self.assertFalse(done['timed_out'])
        self.assertEqual(done['runs'][0]['state'],'failed'); self.assertIn('Mock provider failed',done['runs'][0]['error'])

    async def test_thinking_uses_model_capabilities_defaults_and_respawn(self):
        self.write_config(PI_MOCK_THINKING='"1"')
        settings=self.pi_agent/'settings.json'
        settings.write_text(json.dumps({'compaction':{'enabled':False},'defaultThinkingLevel':'low'}))
        before=settings.read_bytes()
        await self.initialize(); await self.tool('pi_context',{'cwd':str(self.workspace)})
        unsupported=await self.rpc('tools/call',{'name':'pi_spawn_agent','arguments':{
            'task':'must never call model','access':'read','request_id':'bad-thinking','thinking':'medium'}})
        self.assertTrue(unsupported['result']['isError'])
        error=self.unpack(unsupported)['error']
        self.assertEqual(error['code'],'unsupported_thinking'); self.assertIn('available:',error['message'])
        for path in (self.home/'agents').glob('*/stderr.log'):
            self.assertNotIn('PI_MOCK_REPLY',path.read_text())
        for requested in (None,'max'):
            params={'task':'thinking check','access':'read','request_id':f'thinking-{requested}'}
            if requested: params['thinking']=requested
            spawned=await self.tool('pi_spawn_agent',params)
            self.assertEqual(spawned['thinking'],requested or 'low')
            self.assertEqual(spawned['available_thinking'],['low','high','max'])
            done=await self.tool('pi_wait_agent',{'run_ids':[spawned['run_id']],'timeout_seconds':8})
            self.assertEqual(done['runs'][0]['result']['text'],'MOCK_REPLY_1')
            await self.tool('pi_close_agent',{'agent_id':spawned['agent_id'],'request_id':f'stop-{requested}'})
            await self.tool('pi_respawn_agent',{'agent_id':spawned['agent_id'],'request_id':f'respawn-{requested}'})
            state=await self.tool('pi_inspect_agent',{'agent_id':spawned['agent_id']})
            self.assertEqual(state['agent']['thinking'],requested or 'low')
        self.assertEqual(settings.read_bytes(),before)

    async def test_stock_sdk_completion_and_follow_up(self):
        self.write_config()
        scope,a,q,done,_=await self.lifecycle()
        self.assertFalse(done['timed_out'],done)
        self.assertEqual(await self.results(scope,done['runs']),['MOCK_REPLY_1','MOCK_REPLY_2'])
        close=await self.tool('pi_close_agent',{'scope':scope,'agent_id':a['agent_id'],'request_id':'close'})
        self.assertEqual(close['cleanup'],'verified')

    async def test_sdk_thinking_and_silent_tool_outlive_idle_limit(self):
        self.write_config(PI_MOCK_PROGRESS='"thinking"',PI_MOCK_STREAM_MS='"2200"',PI_MOCK_TOOL_MS='"2200"')
        await self.initialize()
        scope=(await self.tool('pi_context',{'cwd':str(self.workspace)}))['scope']
        a=await self.tool('pi_spawn_agent',{'scope':scope,'access':'read','task':'offline work',
            'idle_timeout_seconds':1,'request_id':'idle-live'})
        done=await self.tool('pi_wait_agent',{'scope':scope,'run_ids':[a['run_id']],'timeout_seconds':15})
        self.assertFalse(done['timed_out'],done)
        self.assertEqual(done['runs'][0]['state'],'completed',done)
        self.assertEqual(await self.results(scope,done['runs']),['MOCK_REPLY_2'])
        trace=(self.home/'agents'/a['agent_id']/'stderr.log').read_text()
        self.assertIn('PI_MOCK_TOOL_END',trace)
        state=await self.tool('pi_inspect_agent',{'scope':scope,'agent_id':a['agent_id']})
        self.assertEqual(state['agent']['active_tools'],[])
        self.assertIsNone(state['agent']['idle_seconds'])

    async def test_sdk_silent_model_is_stopped(self):
        self.write_config(PI_MOCK_STREAM_MS='"30000"')
        await self.initialize()
        scope=(await self.tool('pi_context',{'cwd':str(self.workspace)}))['scope']
        a=await self.tool('pi_spawn_agent',{'scope':scope,'access':'read','task':'offline silence',
            'idle_timeout_seconds':1,'request_id':'idle-stall'})
        done=await self.tool('pi_wait_agent',{'scope':scope,'run_ids':[a['run_id']],'timeout_seconds':8})
        self.assertEqual(done['runs'][0]['state'],'timed_out',done)
        state=await self.tool('pi_inspect_agent',{'scope':scope,'agent_id':a['agent_id']})
        self.assertEqual(state['agent']['cleanup'],'verified')

    async def test_sdk_ui_timeout_releases_the_watchdog_pause(self):
        self.write_config(PI_MOCK_CONFIRM='"1"',PI_MOCK_CONFIRM_TIMEOUT='"100"',PI_MOCK_AFTER_CONFIRM_MS='"30000"')
        await self.initialize()
        scope=(await self.tool('pi_context',{'cwd':str(self.workspace)}))['scope']
        a=await self.tool('pi_spawn_agent',{'scope':scope,'access':'read','task':'ASK offline',
            'idle_timeout_seconds':1,'request_id':'ui-expire'})
        path=self.home/'agents'/a['agent_id']/'stderr.log'
        await self.wait_marker(path,'PI_MOCK_CONFIRM_ANSWER false')
        done=await self.tool('pi_wait_agent',{'scope':scope,'run_ids':[a['run_id']],'timeout_seconds':8})
        self.assertEqual(done['runs'][0]['state'],'timed_out',done)
        self.assertEqual(done.get('questions',[]),[])

    async def test_startup_commands_finish_before_tasks_without_model_calls(self):
        self.write_config(PI_MOCK_STARTUP_COMMANDS='"1"')
        scope,a,q,done,trace=await self.lifecycle()
        self.assertFalse(done['timed_out'],done)
        self.assertEqual(await self.results(scope,done['runs']),['MOCK_REPLY_1','MOCK_REPLY_2'])
        self.assertLess(trace.index('MOCK_COMMAND_CAPTURED'),trace.index('MOCK_COMMAND_NESTED'))
        self.assertLess(trace.index('MOCK_COMMAND_NESTED'),trace.index('PI_MOCK_REPLY 1'))
        report=await self.tool('pi_inspect_agent',{'scope':scope,'agent_id':a['agent_id'],'limit':100,'max_bytes':16384})
        self.assertFalse([e for e in report['events'] if e['type']=='extension_error'])

    async def test_startup_commands_do_not_admit_unowned_prompts(self):
        self.write_config(PI_MOCK_STARTUP_COMMANDS='"reject"',PI_MOCK_CONTEXT='"1"')
        scope,a,q,done,trace=await self.lifecycle()
        self.assertFalse(done['timed_out'],done)
        self.assertEqual(await self.results(scope,done['runs']),['MOCK_REPLY_1','MOCK_REPLY_2'])
        self.assertNotIn('UNOWNED_STARTUP_INPUT',trace)
        self.assertEqual(trace.count('MOCK_COMMAND_NESTED'),1)
        report=await self.tool('pi_inspect_agent',{'scope':scope,'agent_id':a['agent_id'],'limit':100,'max_bytes':16384})
        errors=[e for e in report['events'] if e['type']=='extension_error']
        self.assertEqual(len(errors),3,errors)
        self.assertTrue(all('Input rejected' in e['data']['error'] for e in errors))

    async def test_extension_stdout_cannot_corrupt_protocol_events(self):
        self.write_config(PI_MOCK_STDOUT='"1"')
        scope,a,q,done,trace=await self.lifecycle()
        self.assertFalse(done['timed_out'],done)
        self.assertEqual(await self.results(scope,done['runs']),['MOCK_REPLY_1','MOCK_REPLY_2'])
        self.assertIn('MOCK_EXTENSION_LOG',trace)
        self.assertEqual(trace.count('\x1b]777;notify;pi;MOCK_NOTIFY\x07'),2)
        report=await self.tool('pi_inspect_agent',{'scope':scope,'agent_id':a['agent_id'],'limit':100,'max_bytes':16384})
        kinds=[e['type'] for e in report['events']]
        self.assertNotIn('protocol_warning',kinds)
        self.assertEqual(kinds.count('agent_end'),2)
        self.assertEqual(kinds.count('run_terminal'),2)

    async def test_before_settle_work_and_native_continuation(self):
        self.write_config(PI_MOCK_CONTINUE='"1"',PI_MOCK_SETTLE_MS='"100"')
        scope,a,q,done,trace=await self.lifecycle()
        self.assertFalse(done['timed_out'],done)
        self.assertIn('PI_MOCK_BEFORE_SETTLE',trace)
        self.assertEqual(await self.results(scope,done['runs']),['MOCK_REPLY_2','MOCK_REPLY_3'])

    async def test_multiple_nested_extension_inputs_remain_in_the_original_task(self):
        self.write_config(PI_MOCK_SETTLED_SEND='"ONE"',PI_MOCK_SETTLED_SIBLING='"TWO"',
                          PI_MOCK_SETTLED_NESTED='"THREE"',PI_MOCK_SETTLED_INPUT_MS='"150"',PI_MOCK_CONTEXT='"1"')
        scope,a,q,done,trace=await self.lifecycle()
        self.assertFalse(done['timed_out'],done)
        self.assertEqual(await self.results(scope,done['runs']),['MOCK_REPLY_4','MOCK_REPLY_5'])
        entered=re.findall(r'PI_MOCK_SETTLED_INPUT_TEXT (.+)',trace)
        self.assertEqual(entered,['ONE','TWO','THREE'])
        before=await self.payloads(scope,done['runs'])
        after=await self.payloads(scope,done['runs'])
        self.assertEqual([r['result_sha256'] for r in before],[r['result_sha256'] for r in after])

    async def test_handled_extension_continuation_drains_without_an_extra_run(self):
        self.write_config(PI_MOCK_SETTLED_SEND='"HANDLED"',PI_MOCK_SETTLED_INPUT='"handled"',
                          PI_MOCK_SETTLED_INPUT_MS='"150"')
        scope,a,q,done,trace=await self.lifecycle()
        self.assertFalse(done['timed_out'],done)
        self.assertEqual(await self.results(scope,done['runs']),['MOCK_REPLY_1','MOCK_REPLY_2'])
        self.assertIn('PI_MOCK_SETTLED_INPUT_MODE handled',trace)

    async def test_handled_primary_fails_and_the_next_task_runs(self):
        self.write_config(PI_MOCK_BLOCK_INPUT='"offline first"',PI_MOCK_BLOCK_MS='"150"',
                          PI_MOCK_BLOCK_RESULT='"handled"')
        scope,a,q,done,trace=await self.lifecycle()
        self.assertFalse(done['timed_out'],done)
        self.assertEqual(done['reason'],'failed_or_stopped')
        self.assertEqual(done['runs'][0]['state'],'failed')
        following=await self.tool('pi_wait_agent',{'scope':scope,'run_ids':[q['run_id']],'timeout_seconds':8})
        self.assertEqual(following['runs'][0]['state'],'completed')
        done['runs'][1]=following['runs'][0]
        self.assertEqual(await self.results(scope,done['runs']),['','MOCK_REPLY_1'])
        first=(await self.payloads(scope,done['runs']))[0]
        self.assertIn('without producing',first['run']['error'])

    async def start_case(self,task='FIRST'):
        await self.initialize()
        scope=(await self.tool('pi_context',{'cwd':str(self.workspace)}))['scope']
        a=await self.tool('pi_spawn_agent',{'scope':scope,'cwd':str(self.workspace),'access':'read',
            'task':task,'request_id':'spawn'})
        return scope,a,self.home/'agents'/a['agent_id']/'stderr.log'

    async def follow_and_wait(self,scope,a):
        q=await self.tool('pi_send_input',{'scope':scope,'agent_id':a['agent_id'],'mode':'follow_up',
            'message':'NEXT_TASK','request_id':'follow'})
        done=await self.tool('pi_wait_agent',{'scope':scope,'run_ids':[a['run_id'],q['run_id']],
            'timeout_seconds':8,'mode':'all'})
        self.assertFalse(done['timed_out'],done)
        self.assertEqual([r['state'] for r in done['runs']],['completed','completed'])
        return done

    async def test_concurrent_extension_submissions_have_serial_preflights_and_preserve_forced_prompt(self):
        release=self.root/'release'; release.mkdir()
        self.write_config(PI_MOCK_EXT_FOLLOWUP='"ONE,TWO"',PI_MOCK_BLOCK_INPUT='"ONE,TWO"',
                          PI_MOCK_BLOCK_MS='"60000"',PI_MOCK_BLOCK_RELEASE_DIR=json.dumps(str(release)),
                          PI_MOCK_FORCE_PROMPT='"KEEP_FORCED_PROMPT"',PI_MOCK_WIRE='"1"')
        scope,a,path=await self.start_case()
        await self.wait_marker(path,'PI_MOCK_INPUT_BLOCKED_START 1 ONE')
        # Both inputs were accepted in agent_start. The second preflight cannot
        # run ahead, even if its release is already available.
        (release/'2').touch()
        self.assertNotIn('PI_MOCK_INPUT_BLOCKED_START 2',path.read_text())
        (release/'1').touch()
        done=await self.follow_and_wait(scope,a)
        self.assertEqual(await self.results(scope,done['runs']),['MOCK_REPLY_3','MOCK_REPLY_4'])
        trace=path.read_text()
        self.assertLess(trace.index('PI_MOCK_INPUT_BLOCKED_END 1'),trace.index('PI_MOCK_INPUT_BLOCKED_START 2'))
        wires=[json.loads(line.split(' ',2)[2]) for line in trace.splitlines() if line.startswith('PI_MOCK_WIRE ')]
        self.assertEqual([w['forced'] for w in wires],['KEEP_FORCED_PROMPT']*4)
        self.assertIn('ONE',wires[1]['texts']); self.assertIn('TWO',wires[2]['texts'])
        self.assertNotIn('NEXT_TASK',wires[2]['texts'])

    async def test_steer_is_an_ordered_continuation_with_its_own_input_hook_and_prompt(self):
        self.write_config(PI_MOCK_STREAM_MS='"500"',PI_MOCK_FORCE_PROMPT='"FORCED"',PI_MOCK_WIRE='"1"')
        scope,a,path=await self.start_case()
        await self.wait_marker(path,'PI_MOCK_REPLY 1')
        receipt=await self.tool('pi_send_input',{'scope':scope,'agent_id':a['agent_id'],'mode':'steer',
            'message':'STEER_TEXT','request_id':'steer'})
        self.assertEqual(receipt['run_id'],a['run_id'])
        self.assertEqual(receipt['execution'],'after_current_sdk_call')
        self.assertEqual(receipt['name'],a['name'])
        done=await self.follow_and_wait(scope,a)
        self.assertEqual(await self.results(scope,done['runs']),['MOCK_REPLY_2','MOCK_REPLY_3'])
        wires=[json.loads(line.split(' ',2)[2]) for line in path.read_text().splitlines() if line.startswith('PI_MOCK_WIRE ')]
        self.assertEqual([w['forced'] for w in wires],['FORCED']*3)
        self.assertNotIn('STEER_TEXT',wires[0]['texts']); self.assertIn('STEER_TEXT',wires[1]['texts'])
        inspected=await self.tool('pi_inspect_agent',{'scope':scope,'agent_id':a['agent_id'],
            'limit':100,'max_bytes':16384})
        observed=[r for r in inspected['receipts'] if r['id']==receipt['receipt_id']]
        self.assertEqual([r['state'] for r in observed],['consumed'])
        consumed=[e for e in inspected['events'] if e['type']=='control_consumed'
            and e['data']['request_id']==receipt['receipt_id']]
        self.assertEqual(len(consumed),1)

    async def test_interrupt_stops_a_primary_input_hook_and_preserves_the_session(self):
        release=self.root/'release'
        self.write_config(PI_MOCK_BLOCK_INPUT='"BLOCK"',PI_MOCK_BLOCK_MS='"60000"',
                          PI_MOCK_RELEASE_FILE=json.dumps(str(release)))
        scope,a,path=await self.start_case('BLOCK')
        await self.wait_marker(path,'PI_MOCK_INPUT_BLOCKED_START')
        q=await self.tool('pi_send_input',{'scope':scope,'agent_id':a['agent_id'],'mode':'follow_up',
            'message':'CANCELLED_FOLLOW','request_id':'queued'})
        stop=await self.tool('pi_interrupt_agent',{'scope':scope,'agent_id':a['agent_id'],'request_id':'interrupt'})
        self.assertEqual((stop['state'],stop['cleanup'],stop['process_retained']),('dormant','verified',False))
        release.touch()
        self.assertNotIn('PI_MOCK_REPLY',path.read_text())
        done=await self.tool('pi_wait_agent',{'scope':scope,'run_ids':[a['run_id'],q['run_id']],'timeout_seconds':1})
        self.assertEqual([r['state'] for r in done['runs']],['interrupted','cancelled'])

    async def test_interrupt_stops_a_steer_preflight_without_leaking_to_replacement(self):
        self.write_config(PI_MOCK_STREAM_MS='"300"',PI_MOCK_BLOCK_INPUT='"CANCEL_STEER"',PI_MOCK_BLOCK_MS='"60000"')
        scope,a,path=await self.start_case()
        await self.wait_marker(path,'PI_MOCK_REPLY 1')
        await self.tool('pi_send_input',{'scope':scope,'agent_id':a['agent_id'],'mode':'steer',
            'message':'CANCEL_STEER','request_id':'steer'})
        await self.wait_marker(path,'PI_MOCK_INPUT_BLOCKED_START')
        replaced=await self.tool('pi_send_input',{'scope':scope,'agent_id':a['agent_id'],'interrupt':True,
            'message':'REPLACEMENT','request_id':'replace'})
        done=await self.tool('pi_wait_agent',{'scope':scope,'run_ids':[replaced['run_id']],'timeout_seconds':8})
        self.assertFalse(done['timed_out'],done)
        self.assertEqual(await self.results(scope,done['runs']),['MOCK_REPLY_1'])

    async def test_ambient_provider_default_and_settings_remain_unchanged(self):
        self.write_config()
        config=self.home/'config.toml'
        config.write_text(config.read_text().replace('provider = "pi-mock-offline"\nmodel = "mock"\n',''))
        settings=self.pi_agent/'settings.json'
        settings.write_text(json.dumps({'defaultProvider':'pi-mock-offline','defaultModel':'mock',
            'compaction':{'enabled':False},'retry':{'enabled':False}}))
        before=settings.read_bytes()
        scope,a,q,done,_=await self.lifecycle()
        self.assertFalse(done['timed_out'],done)
        self.assertEqual(await self.results(scope,done['runs']),['MOCK_REPLY_1','MOCK_REPLY_2'])
        self.assertEqual(settings.read_bytes(),before)

    async def test_old_extension_timer_cannot_attach_to_the_next_task(self):
        release=self.root/'late'
        self.write_config(PI_MOCK_LATE_RELEASE=json.dumps(str(release)),PI_MOCK_STREAM_MS='"400"',PI_MOCK_CONTEXT='"1"')
        scope,a,path=await self.start_case()
        await self.tool('pi_wait_agent',{'scope':scope,'run_ids':[a['run_id']],'timeout_seconds':8})
        second=await self.tool('pi_send_input',{'scope':scope,'agent_id':a['agent_id'],'mode':'send',
            'message':'SECOND','request_id':'second'})
        await self.wait_marker(path,'PI_MOCK_REPLY 2')
        release.touch()
        await self.wait_marker(path,'PI_MOCK_LATE_ATTEMPTED')
        done=await self.tool('pi_wait_agent',{'scope':scope,'run_ids':[second['run_id']],'timeout_seconds':8})
        self.assertFalse(done['timed_out'],done)
        self.assertEqual(await self.results(scope,done['runs']),['MOCK_REPLY_2'])
        self.assertNotIn('STALE_TIMER_INPUT',path.read_text())
        self.assertNotIn('PI_MOCK_REPLY 3',path.read_text())

    async def test_extension_confirmation_requires_explicit_mcp_answer(self):
        self.write_config(PI_MOCK_CONFIRM='"1"')
        scope,a,path=await self.start_case('ASK')
        await self.wait_marker(path,'PI_MOCK_CONFIRM_WAIT')
        state=await self.tool('pi_inspect_agent',{'scope':scope,'agent_id':a['agent_id']})
        question=state['agent']['pending_input'][0]
        self.assertEqual(question['method'],'confirm')
        self.assertNotIn('PI_MOCK_REPLY',path.read_text())
        await self.tool('pi_answer_agent',{'scope':scope,'agent_id':a['agent_id'],
            'ui_request_id':question['id'],'answer':True,'request_id':'answer'})
        done=await self.tool('pi_wait_agent',{'scope':scope,'run_ids':[a['run_id']],'timeout_seconds':8})
        self.assertFalse(done['timed_out'],done)
        self.assertEqual(await self.results(scope,done['runs']),['MOCK_REPLY_1'])
