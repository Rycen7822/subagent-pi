"""Opt-in 310-second wait through real Codex and the installed plugin.
No model turn: app-server calls MCP directly; child is the offline fake Pi.
"""
import asyncio
import json
import os
import shutil
import sys
import unittest

from test_transport import McpHarness, ROOT
from subagent_pi.client import request


class LiveCodexWait(McpHarness, unittest.IsolatedAsyncioTestCase):
    async def test_installed_wait_outlives_codex_default_timeout(self):
        if os.environ.get('SUBAGENT_PI_LIVE_CODEX_WAIT')!='1':
            self.skipTest('set SUBAGENT_PI_LIVE_CODEX_WAIT=1 for the 310-second offline Codex wait')
        codex=shutil.which('codex')
        if not codex: self.skipTest('Codex is required')
        (self.root/'codex').mkdir()
        env={'HOME':str(self.root),'CODEX_HOME':str(self.root/'codex'),
             'PATH':os.environ['PATH'],'LANG':'C.UTF-8',
             'XDG_RUNTIME_DIR':os.environ.get('XDG_RUNTIME_DIR','/tmp')}
        market=self.root/'marketplace'
        async def command(*argv):
            proc=await asyncio.create_subprocess_exec(*argv,env=env,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            stdout,stderr=await proc.communicate()
            self.assertEqual(proc.returncode,0,stderr.decode())
            return stdout
        await command(sys.executable,str(ROOT/'scripts/install.py'),'--dest',str(market),
                      '--state-home',str(self.home),'--bin-dir',str(self.root/'bin'))
        await command(codex,'plugin','marketplace','add',str(market))
        await command(codex,'plugin','add','subagent-pi@subagent-pi-local')
        app=await asyncio.create_subprocess_exec(codex,'app-server','--stdio',env=env,
            stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,
            limit=8*1024*1024)
        stderr=asyncio.create_task(app.stderr.read())
        seq=0
        async def rpc(method,params):
            nonlocal seq
            seq+=1; rid=seq
            app.stdin.write((json.dumps({'id':rid,'method':method,'params':params})+'\n').encode())
            await app.stdin.drain()
            async with asyncio.timeout(340):
                while line:=await app.stdout.readline():
                    message=json.loads(line)
                    if message.get('id')==rid:
                        self.assertNotIn('error',message)
                        return message['result']
                    if 'id' in message and 'method' in message:
                        self.fail('Unexpected interactive request: '+message['method'])
            self.fail('Codex exited without a reply')
        try:
            await rpc('initialize',{'clientInfo':{'name':'offline-long-wait','version':'1'},'capabilities':{'experimentalApi':True}})
            app.stdin.write(b'{"method":"initialized"}\n'); await app.stdin.drain()
            thread=await rpc('thread/start',{'cwd':str(self.workspace),'ephemeral':True,'approvalPolicy':'never'})
            tid=thread['thread']['id']
            status=await rpc('mcpServerStatus/list',{'threadId':tid,'limit':100,'detail':'toolsAndAuthOnly'})
            servers=[s for s in status['data'] if s['name']=='subagent-pi']
            self.assertEqual(len(servers),1,status)
            async def tool(name,args):
                result=await rpc('mcpServer/tool/call',{'threadId':tid,'server':'subagent-pi','tool':name,'arguments':args})
                self.assertFalse(result.get('isError'),result)
                return json.loads(result['content'][0]['text'])
            scope=await tool('pi_context',{'cwd':str(self.workspace)})
            # CLI/teardown must find the SAME daemon as Codex's filtered MCP env.
            await request(self.home,'list',{'scope':scope['scope']},autostart=False)
            run=await tool('pi_spawn_agent',{'task':'delay=310|long-wait','access':'read','request_id':'long'})
            start=asyncio.get_running_loop().time()
            # Omit timeout_ms: catches a stale 25s default or 45s IPC budget too.
            result=await tool('pi_wait_agent',{'run_ids':[run['run_id']]})
            elapsed=asyncio.get_running_loop().time()-start
            self.assertGreater(elapsed,300)
            self.assertLess(elapsed,330)
            self.assertEqual(result['reason'],'completed')
            self.assertFalse(result['timed_out'])
            self.assertIn('Completed: long-wait',result['runs'][0]['result']['text'])
            await tool('pi_close_agent',{'agent_id':run['agent_id'],'request_id':'close'})
            await rpc('thread/unsubscribe',{'threadId':tid})
        finally:
            app.stdin.close()
            try: await asyncio.wait_for(app.wait(),5)
            except asyncio.TimeoutError: app.terminate(); await app.wait()
            await stderr
