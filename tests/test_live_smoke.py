"""The opt-in script's actual IPC and subprocess flow, using offline fake Pi."""
import asyncio
import json
import os
import sqlite3
import sys
import unittest

from test_transport import McpHarness, ROOT


class LiveSmokeScript(McpHarness, unittest.IsolatedAsyncioTestCase):
    async def test_delivered_result_consumes_attention_and_closes_worker(self):
        (self.home/'config.toml').write_text(
            'pi_command = '+json.dumps([sys.executable,str(ROOT/'tests/fake_pi.py'),
                                      '--reply-file','marker.txt'])+
            '\n[inheritance]\nenabled = false\n')
        env=os.environ.copy()
        env.update(PI_AGENTS_HOME=str(self.home),TMPDIR=str(self.root))
        for key in ('CODEX_THREAD_ID','CODEX_SESSION_ID','PI_AGENTS_SCOPE'):
            env.pop(key,None)
        proc=await asyncio.create_subprocess_exec(sys.executable,str(ROOT/'scripts/live_smoke.py'),
            '--allow-model-call',env=env,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        out,err=await asyncio.wait_for(proc.communicate(),20)
        self.assertEqual(proc.returncode,0,err.decode()+out.decode())
        self.assertIn('Real Pi smoke passed',out.decode())
        with sqlite3.connect(self.home/'registry.sqlite') as db:
            self.assertEqual(db.execute('SELECT state,handled FROM parent_notifications').fetchall(),
                             [('observed',1)])
            self.assertEqual(db.execute('SELECT state,cleanup FROM agents').fetchall(),
                             [('closed','verified')])
