from __future__ import annotations
import asyncio
import contextlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from subagent_pi.client import request
from subagent_pi.common import AgentError, group_members, socket_path

class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='subagent-pi-ipc-')
        self.root=Path(self.tmp.name); self.home=self.root/'state'; self.home.mkdir()
        self.workspace=self.root/'workspace'; self.workspace.mkdir()
        (self.home/'config.toml').write_text('pi_command = '+json.dumps([sys.executable,str(ROOT/'tests/fake_pi.py')])+'\nrpc_timeout_seconds=8\n')
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
        self.mcp=await asyncio.create_subprocess_exec(sys.executable,str(ROOT/'bin/subagent-pi'),'mcp',
            stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,env=env,limit=8*1024*1024)
        self.stderr_task=asyncio.create_task(self.mcp.stderr.read())
    async def send(self,method,params=None,rid=None):
        self.reqid+=1; rid=rid or self.reqid
        self.mcp.stdin.write((json.dumps({'jsonrpc':'2.0','id':rid,'method':method,'params':params or {}})+'\n').encode()); await self.mcp.stdin.drain(); return rid
    async def receive(self): return json.loads(await asyncio.wait_for(self.mcp.stdout.readline(),8))
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
        done=await self.tool('pi_wait_agent',{'scope':sid,'run_ids':[a['run_id']],'timeout_ms':4000})
        self.assertFalse(done['timed_out'])
    async def test_mcp_initialize_and_list(self):
        init=await self.initialize(); self.assertEqual(init['result']['protocolVersion'],'2025-06-18')
        result=await self.rpc('tools/list'); tools=result['result']['tools']
        self.assertEqual(len(tools),12)
        self.assertTrue(all('_op' not in t for t in tools))
    async def test_mcp_scope_reused_per_workspace(self):
        await self.initialize()
        a=await self.tool('pi_context',{'cwd':str(self.workspace)})
        b=await self.tool('pi_context',{'cwd':str(self.workspace)})
        self.assertEqual(a['scope'],b['scope'])
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
        waitid=await self.send('tools/call',{'name':'pi_wait_agent','arguments':{'scope':sid,'run_ids':[a['run_id']],'timeout_ms':5000}})
        await asyncio.sleep(.05)
        self.mcp.stdin.write((json.dumps({'jsonrpc':'2.0','method':'notifications/cancelled','params':{'requestId':waitid}})+'\n').encode()); await self.mcp.stdin.drain()
        self.assertEqual((await self.rpc('ping'))['result'],{})
        state=await self.tool('pi_list_agents',{'scope':sid})
        self.assertEqual(state['agents'][0]['state'],'running')
        done=await self.tool('pi_wait_agent',{'scope':sid,'run_ids':[a['run_id']],'timeout_ms':4000})
        self.assertEqual(done['runs'][0]['state'],'completed')
    async def test_mcp_exit_does_not_stop_worker(self):
        await self.initialize()
        sid=(await self.tool('pi_context',{'cwd':str(self.workspace)}))['scope']
        a=await self.tool('pi_spawn_agent',{'scope':sid,'cwd':str(self.workspace),'task':'delay=0.4|survive-adapter','access':'read','request_id':'survive'})
        self.mcp.stdin.close(); await self.mcp.wait()
        done=await request(self.home,'wait',{'scope':sid,'run_ids':[a['run_id']],'timeout_ms':4000})
        self.assertEqual(done['runs'][0]['state'],'completed')
    async def test_durable_unacknowledged_result_after_daemon_restart(self):
        sid=await self.open_scope(); a=await self.spawn(sid)
        await request(self.home,'wait',{'scope':sid,'run_ids':[a['run_id']],'timeout_ms':4000})
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
        writer.write((dumps({'v':1,'op':'spawn','params':params})+'\n').encode()); await writer.drain(); writer.close(); await writer.wait_closed()
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
        result=await request(self.home,'wait',{'scope':sid,'run_ids':[revived['run_id']],'timeout_ms':4000})
        self.assertEqual(result['runs'][0]['state'],'completed')

if __name__=='__main__': unittest.main()
