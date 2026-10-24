"""Native Windows contracts at real subprocess, Job and named-pipe boundaries."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from runtime_harness import RuntimeHarness
from test_transport import ROOT
from subagent_pi.common import AgentError, IS_WINDOWS, live_identity
from subagent_pi import worker


@unittest.skipUnless(IS_WINDOWS,'native Windows OS boundaries')
class WindowsRuntime(RuntimeHarness,unittest.IsolatedAsyncioTestCase):

    async def test_close_reaps_detached_tool_descendant(self):
        a=await self.spawn('SPAWN_DETACHED_CHILD')
        events=await self.until(lambda:self.events(a['agent_id'],'tool_execution_start'))
        child=json.loads(events[0]['payload'])['args']['pid']
        directory=self.home/'agents'/a['agent_id']
        owner=json.loads((directory/'owner.json').read_text(encoding='utf-8'))
        from subagent_pi import windows
        members=windows.job_members(owner['guard_pid'])
        self.assertIn(child,members)
        stamps={pid:windows.process_identity(pid) for pid in members}
        await self.mutation('close',a['agent_id'])
        self.assertEqual(windows.job_members(owner['guard_pid']),[])
        self.assertTrue(all(live_identity(pid,stamp) is False for pid,stamp in stamps.items()))
        self.assertEqual(json.loads((directory/'owner.json').read_text(encoding='utf-8'))['descendants_cleanup'],'verified')

    async def test_bootstrap_pipe_authenticates_owned_node_and_checks_receipt(self):
        from subagent_pi.bootstrap import BootstrapChannel
        from subagent_pi.processes import spawn,finish
        import shutil
        node=shutil.which('node')
        if not node: self.skipTest('Node unavailable')
        channel=await BootstrapChannel.open()
        channel.pid=os.getpid()  # This test process owns no guard Job.
        sending=asyncio.create_task(channel.send(b'{"v":1,"agent":{"id":"test","generation":3},"mcp":{"servers":[]}}'))
        # Payload is available, but an ordinary same-user process must not read
        # it: membership in this launch's guard Job is also required.
        stranger=await asyncio.create_subprocess_exec(node,'-e',
            "const s=require('node:net').createConnection(process.argv[1]);s.on('data',d=>{console.log(d.length);s.destroy()});s.on('end',()=>console.log(0));",
            channel.name,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        try:
            out,err=await asyncio.wait_for(stranger.communicate(),10)
            self.assertEqual(stranger.returncode,0,err)
            self.assertEqual(out.strip(),b'0')
        except BaseException:
            if stranger.returncode is None: stranger.kill()
            await stranger.wait()
            channel.close(); sending.cancel()
            await asyncio.gather(sending,return_exceptions=True)
            raise
        script="import {bootstrap} from "+json.dumps((ROOT/'runtime/bootstrap.mjs').as_uri())+"; await bootstrap(); globalThis.__subagentPiBridgeReceipt({kind:'subagent-pi-bridge-receipt',agent:'test',generation:3,state:'ready',servers:[]});"
        proc=await spawn(node,'--input-type=module','-e',script,env={**os.environ,'PI_AGENTS_BOOTSTRAP_PIPE':channel.name},stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        channel.pid=proc.pid
        try:
            self.assertEqual(await sending,'ok')
            receipt=await worker.read_receipt(channel,'test',3,10)
            worker.check_receipt(receipt)
            with self.assertRaises(AgentError): worker.validate_receipt(receipt,'test',4)
        finally:
            channel.close(); await finish(proc)


@unittest.skipUnless(IS_WINDOWS,'native Windows executable launch')
class WindowsExecutables(unittest.TestCase):
    def test_npm_shim_passes_literal_arguments_without_a_shell(self):
        from subagent_pi.executables import argv
        with tempfile.TemporaryDirectory(prefix='npm shim 中文 ') as tmp:
            root=Path(tmp)
            (root/'cli.cjs').write_text('console.log(JSON.stringify(process.argv.slice(2)))',encoding='utf-8')
            shim=root/'tool.cmd'
            shim.write_text('@echo off\nnode "%dp0%\\cli.cjs" %*\n',encoding='ascii')
            args=['a & echo unsafe','%PATH%','quote"inside','中文 space','$(echo unsafe)']
            result=subprocess.run(argv(str(shim),*args),capture_output=True,text=True,encoding='utf-8')
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(result.stdout),args)

