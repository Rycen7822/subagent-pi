"""Parent identity and durable attention through real MCP/IPC/subprocess boundaries."""
import asyncio
import json
import os
from pathlib import Path
import sys
import unittest
import uuid

from subagent_pi.client import request
from test_transport import McpHarness


class ParentNotifications(McpHarness, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.codex_home=self.root/'codex'; self.codex_home.mkdir()
        self.log=self.codex_home/'queue.jsonl'
        bindir=self.root/'bin'; bindir.mkdir()
        command=bindir/'codex'
        command.write_text('#!'+sys.executable+'\n'+"""
import json,os,sys,uuid
from pathlib import Path
home=Path(os.environ['CODEX_HOME']); args=sys.argv[1:]
assert args[0]=='queue'
thread=args[args.index('--thread')+1]; message=args[args.index('--message')+1]
with (home/'queue.jsonl').open('a') as f: f.write(json.dumps({'thread':thread,'message':message})+'\\n')
if (home/'reject').exists(): sys.exit(2)
print('Queued message '+str(uuid.uuid4())+' for thread '+thread+'.')
""")
        command.chmod(0o755)
        self.mcp_env={'CODEX_HOME':str(self.codex_home),'HOME':str(self.root),
                      'PATH':str(bindir)+os.pathsep+os.environ['PATH']}
        self.parent=str(uuid.uuid4())
        await self.initialize()

    async def parent_tool(self,name,args,thread=None,error=False):
        response=await self.rpc('tools/call',{'name':name,'arguments':args,'_meta':{'threadId':thread or self.parent}})
        value=self.unpack(response)
        self.assertEqual(bool(response['result'].get('isError')),error,value)
        return value

    async def settled_notifications(self,sid,count):
        until=asyncio.get_running_loop().time()+8
        while asyncio.get_running_loop().time()<until:
            state=await self.tool('pi_list_agents',{'scope':sid})
            rows=state['parent_notifications']['recent']
            if len(rows)>=count and all(n['state'] not in ('pending','sending') for n in rows): return rows
            await asyncio.sleep(.02)
        self.fail('Notifications did not settle')

    def queued(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    async def open_parent(self):
        return (await self.parent_tool('pi_context',{'cwd':str(self.workspace)}))['scope']

    async def test_two_parents_share_adapter_without_scope_or_notification_cross_talk(self):
        first=await self.open_parent(); other=str(uuid.uuid4())
        second=(await self.parent_tool('pi_context',{'cwd':str(self.workspace)},other))['scope']
        self.assertNotEqual(first,second)
        for thread,sid in ((self.parent,first),(other,second)):
            run=await self.parent_tool('pi_spawn_agent',{'request_id':'same-key','task':'hello','access':'read'},thread)
            self.assertEqual(run['scope'],sid)
            await self.parent_tool('pi_wait_agent',{'run_ids':[run['run_id']],'timeout_ms':4000},thread)
            rows=await self.settled_notifications(sid,1)
            self.assertEqual(rows[0]['state'],'queued'); self.assertTrue(rows[0]['queued_id'])
            # Replaying the mutation never produces a second terminal notification.
            retry=await self.parent_tool('pi_spawn_agent',{'request_id':'same-key','task':'hello','access':'read'},thread)
            self.assertEqual(retry['run_id'],run['run_id'])
        messages=self.queued(); self.assertEqual([m['thread'] for m in messages],[self.parent,other])
        self.assertTrue(all('not a user instruction or approval' in m['message'] for m in messages))
        conflict=await self.parent_tool('pi_context',{'cwd':str(self.workspace),'scope':first},other,error=True)
        self.assertEqual(conflict['error']['code'],'parent_conflict')
        conflict=await self.parent_tool('pi_spawn_agent',{'scope':first,'request_id':'wrong-parent','task':'must not start','access':'read'},other,error=True)
        self.assertEqual(conflict['error']['code'],'parent_conflict')

    async def test_question_and_stop_notify_without_a_parent_wait(self):
        sid=await self.open_parent()
        run=await self.parent_tool('pi_spawn_agent',{'request_id':'ask','task':'UI_CONFIRM','access':'read'})
        rows=await self.settled_notifications(sid,1)
        self.assertEqual(rows[0]['kind'],'question')
        self.assertIn('ui-1',self.queued()[0]['message'])
        await self.parent_tool('pi_close_agent',{'request_id':'stop','agent_id':run['agent_id']})
        rows=await self.settled_notifications(sid,2)
        self.assertEqual({r['kind'] for r in rows},{'question','terminal'})
        self.assertIn('interrupted',self.queued()[1]['message'])

    async def test_crash_notifies_without_a_parent_wait(self):
        sid=await self.open_parent()
        response=await self.rpc('tools/call',{'name':'pi_spawn_agent','arguments':{'scope':sid,'request_id':'crash','task':'CRASH','access':'read'}})
        value=self.unpack(response)
        rows=await self.settled_notifications(sid,1)
        self.assertEqual(rows[0]['state'],'queued',value)
        self.assertRegex(self.queued()[0]['message'],'crashed|failed')

    async def test_uncertain_queue_is_durable_and_never_retried(self):
        (self.codex_home/'reject').touch()
        sid=await self.open_parent()
        run=await self.parent_tool('pi_spawn_agent',{'request_id':'finish','task':'done','access':'read'})
        await self.parent_tool('pi_wait_agent',{'run_ids':[run['run_id']],'timeout_ms':4000})
        rows=await self.settled_notifications(sid,1)
        self.assertEqual(rows[0]['state'],'unknown'); self.assertIn('not retried',rows[0]['error'])
        await request(self.home,'shutdown',{'force':True})
        from subagent_pi.common import socket_path
        until=asyncio.get_running_loop().time()+5
        while socket_path(self.home).exists() and asyncio.get_running_loop().time()<until: await asyncio.sleep(.02)
        (self.codex_home/'reject').unlink()
        await self.tool('pi_list_agents',{'scope':sid})  # restarts daemon
        rows=await self.settled_notifications(sid,1)
        self.assertEqual(rows[0]['state'],'unknown'); self.assertEqual(len(self.queued()),1)

    async def restart_daemon(self):
        await request(self.home,'shutdown',{'force':True})
        from subagent_pi.common import socket_path
        until=asyncio.get_running_loop().time()+5
        while socket_path(self.home).exists() and asyncio.get_running_loop().time()<until: await asyncio.sleep(.02)
        await request(self.home,'ping',{})

    async def test_pending_stop_survives_daemon_restart(self):
        sid=await self.open_parent()
        await self.parent_tool('pi_spawn_agent',{'request_id':'slow','task':'delay=30|work','access':'read'})
        await self.restart_daemon()
        rows=await self.settled_notifications(sid,1)
        self.assertEqual(rows[0]['state'],'queued'); self.assertEqual(len(self.queued()),1)
        self.assertIn('interrupted',self.queued()[0]['message'])

    async def test_crash_in_sending_window_is_not_retried(self):
        sid=await self.open_parent()
        run=await self.parent_tool('pi_spawn_agent',{'request_id':'once','task':'once','access':'read'})
        await self.parent_tool('pi_wait_agent',{'run_ids':[run['run_id']],'timeout_ms':4000})
        await self.settled_notifications(sid,1)
        # Reconstruct the on-disk state of a daemon killed after enqueue but
        # before receipt persistence. The external side effect already happened.
        import sqlite3
        with sqlite3.connect(self.home/'registry.sqlite') as db:
            db.execute("UPDATE parent_notifications SET state='sending',queued_id=NULL")
        await self.restart_daemon()
        rows=await self.settled_notifications(sid,1)
        self.assertEqual(rows[0]['state'],'unknown'); self.assertEqual(len(self.queued()),1)

    async def test_unbound_clients_never_guess_the_parent(self):
        scope=await self.tool('pi_context',{'cwd':str(self.workspace)})
        self.assertFalse(scope['parent_notifications']['enabled'])
        run=await self.tool('pi_spawn_agent',{'request_id':'plain','task':'plain','access':'read'})
        await self.tool('pi_wait_agent',{'run_ids':[run['run_id']],'timeout_ms':4000})
        self.assertFalse(self.log.exists())
        bad=await self.rpc('tools/call',{'name':'pi_context','arguments':{'cwd':str(self.workspace),'parent_thread_id':self.parent}})
        self.assertTrue(bad['result']['isError'])
