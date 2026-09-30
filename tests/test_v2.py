from __future__ import annotations
import asyncio
import json
import os
import sys
import time
import unittest
from unittest import mock
import test_runtime as base
from test_transport import McpHarness, ROOT
from subagent_pi.common import AgentError
from subagent_pi.schema import BY_NAME, validate

class V2RuntimeTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=base.RuntimeTests.asyncSetUp
    asyncTearDown=base.RuntimeTests.asyncTearDown
    key=base.RuntimeTests.key
    spawn=base.RuntimeTests.spawn
    mutation=base.RuntimeTests.mutation
    wait=base.RuntimeTests.wait
    result=base.RuntimeTests.result
    until=base.RuntimeTests.until
    events=base.RuntimeTests.events

    async def test_soft_interrupt_retains_worker_cancels_queue_and_reuses_session(self):
        a=await self.spawn('delay=1|first',name='worker')
        w=self.rt.workers[a['agent_id']]; pid=w.proc.pid; generation=w.generation
        q=await self.mutation('send',a['agent_id'],mode='follow_up',message='must not run')
        interrupted=await self.mutation('soft_interrupt','worker')
        self.assertTrue(interrupted['runtime_retained']); self.assertEqual(interrupted['previous_status'],'running')
        self.assertEqual((await self.result(a['run_id']))['run']['state'],'interrupted')
        self.assertEqual((await self.result(q['run_id']))['run']['state'],'cancelled')
        b=await self.mutation('followup','worker',message='replacement')
        await self.wait(b['run_id'])
        self.assertNotEqual(a['run_id'],b['run_id']); self.assertEqual((w.proc.pid,w.generation),(pid,generation))
        self.assertEqual((await self.result(b['run_id']))['text'],'Completed: replacement')

    async def test_abort_rejection_falls_back_to_verified_termination(self):
        self.rt.config['pi_command'].append('--reject-abort')
        a=await self.spawn('delay=1|first')
        stopped=await self.mutation('soft_interrupt',a['agent_id'])
        self.assertFalse(stopped['runtime_retained']); self.assertTrue(stopped['forced'])
        self.assertEqual(self.rt.store.agent(self.scope,a['agent_id'])['cleanup'],'verified')
        self.assertEqual((await self.result(a['run_id']))['run']['state'],'interrupted')

    async def test_question_interrupt_retains_worker_and_rejects_old_answer(self):
        a=await self.spawn('UI_CONFIRM')
        await self.until(lambda: self.rt.workers[a['agent_id']].ui)
        stopped=await self.mutation('soft_interrupt',a['agent_id'])
        self.assertTrue(stopped['runtime_retained'])
        with self.assertRaises(AgentError):
            await self.mutation('answer',a['agent_id'],ui_request_id='ui-1',answer=True)
        self.assertFalse(self.rt.workers[a['agent_id']].ui)

    async def test_native_followup_joins_current_run(self):
        a=await self.spawn('delay=1|first')
        b=await self.mutation('followup',a['agent_id'],message='native message')
        self.assertEqual(a['run_id'],b['run_id'])
        await self.wait(a['run_id'])
        messages=[json.loads(e['payload']) for e in self.events(a['agent_id'],'message')]
        self.assertTrue(any(e['role']=='user' and e['text']=='native message' for e in messages))
        self.assertEqual(self.rt.store.one('SELECT COUNT(*) n FROM runs')['n'],1)

    async def test_idle_message_is_durable_without_a_model_turn(self):
        a=await self.spawn(); await self.wait(a['run_id'])
        sent=await self.mutation('message',a['agent_id'],message='remember this')
        self.assertEqual((sent['delivery'],sent['run_id']),('stored',None))
        self.assertEqual(self.rt.store.one('SELECT COUNT(*) n FROM runs')['n'],1)
        path=self.rt.store.agent(self.scope,a['agent_id'])['session_file']
        self.assertIn('remember this',base.Path(path).read_text())
        w=self.rt.workers[a['agent_id']]; w.idle_since=time.monotonic()-1801
        await self.rt.park_expired()
        b=await self.mutation('followup',a['agent_id'],message='next')
        await self.wait(b['run_id'])
        self.assertIn('remember this',base.Path(path).read_text())

    async def test_idle_timer_ignores_telemetry_and_preserves_unacked_result(self):
        a=await self.spawn(); await self.wait(a['run_id'])
        w=self.rt.workers[a['agent_id']]; stamp=time.monotonic()-1801; w.idle_since=stamp
        self.rt.on_event(w,{'type':'extension_ui_request','method':'notify','message':'telemetry'})
        await self.rt.dispatch('inspect',{'scope':self.scope,'agent_id':a['agent_id']})
        self.assertEqual(w.idle_since,stamp)
        await self.rt.park_expired()
        self.assertEqual(self.rt.store.agent(self.scope,a['agent_id'])['state'],'dormant')
        self.assertFalse((await self.result(a['run_id']))['acknowledged'])
        listing=await self.rt.dispatch('list',{'scope':self.scope})
        self.assertEqual(listing['agents'][0]['agent_status'],'completed')

    async def test_idle_timer_excludes_active_task_and_question(self):
        a=await self.spawn('UI_CONFIRM')
        await self.until(lambda: self.rt.workers[a['agent_id']].ui)
        w=self.rt.workers[a['agent_id']]; w.idle_since=time.monotonic()-1801
        await self.rt.park_expired()
        self.assertFalse(w.closed); self.assertTrue(w.ui)

    async def test_expiry_rechecks_idle_epoch_after_waiting_for_lock(self):
        a=await self.spawn(); await self.wait(a['run_id'])
        w=self.rt.workers[a['agent_id']]; w.idle_since=time.monotonic()-1801
        lock=self.rt.agent_locks[a['agent_id']]; await lock.acquire()
        t=asyncio.create_task(self.rt.park_expired()); await asyncio.sleep(0)
        w.idle_since=time.monotonic(); lock.release(); await t
        self.assertFalse(w.closed)

    async def test_idle_writer_is_parked_for_replacement_but_busy_writer_rejects(self):
        a=await self.spawn(access='write'); await self.wait(a['run_id'])
        b=await self.spawn('delay=1|new writer',access='write')
        self.assertEqual(self.rt.store.agent(self.scope,a['agent_id'])['cleanup'],'verified')
        with self.assertRaises(AgentError) as err: await self.spawn(access='write')
        self.assertEqual(err.exception.code,'writer_conflict')
        await self.wait(b['run_id'])

    async def test_target_collision_rejects_public_reference_without_breaking_internal_lookup(self):
        a=await self.spawn(name='original'); await self.wait(a['run_id'])
        b=await self.spawn(name=a['agent_id']); await self.wait(b['run_id'])
        with self.assertRaises(AgentError) as err:
            await self.mutation('followup',a['agent_id'],message='ambiguous')
        self.assertEqual(err.exception.code,'ambiguous_agent')
        self.assertEqual(self.rt.store.agent(self.scope,a['agent_id'])['id'],a['agent_id'])

    async def test_interrupt_unloaded_agent_does_not_boot(self):
        a=await self.spawn(); await self.wait(a['run_id'])
        await self.mutation('close',a['agent_id'])
        generation=self.rt.store.agent(self.scope,a['agent_id'])['generation']
        stopped=await self.mutation('soft_interrupt',a['agent_id'])
        self.assertFalse(stopped['runtime_retained']); self.assertEqual(stopped['previous_status'],'completed')
        self.assertEqual(self.rt.store.agent(self.scope,a['agent_id'])['generation'],generation)

class V2TransportTests(McpHarness,unittest.IsolatedAsyncioTestCase):
    async def test_cli_v2_operations_share_mcp_ledger_and_projections(self):
        await self.initialize()
        target='审查 agent'
        a=await self.tool('pi_spawn_agent',{'cwd':str(self.workspace),'task':'first','access':'read',
            'name':target,'request_id':'spawn'})
        await self.tool('pi_wait_agent',{'run_ids':[a['run_id']],'timeout_seconds':8})
        env=os.environ.copy(); env['PI_AGENTS_HOME']=str(self.home)
        env.pop('CODEX_THREAD_ID',None); env.pop('CODEX_SESSION_ID',None)
        async def cli(command,*extra):
            proc=await asyncio.create_subprocess_exec(sys.executable,str(ROOT/'bin/subagent-pi'),
                command,target,'--scope',a['scope'],*extra,
                stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,env=env)
            out,err=await asyncio.wait_for(proc.communicate(),10)
            self.assertEqual(proc.returncode,0,err.decode()); return json.loads(out)
        stored=await cli('send-message','--message','idle text','--request-id','msg')
        validate(stored,BY_NAME['pi_send_message']['outputSchema']); self.assertIsNone(stored['run_id'])
        inspected=await self.tool('pi_inspect_agent',{'agent_id':target})
        self.assertEqual(inspected['agent']['id'],a['agent_id'])
        await self.tool('pi_send_message',{'agent_id':target,'message':'MCP Unicode target','request_id':'mcp-msg'})
        next_run=await cli('followup-task','--message','delay=1|next','--request-id','next')
        stopped=await cli('interrupt','--request-id','stop')
        validate(stopped,BY_NAME['pi_interrupt_agent']['outputSchema']); self.assertTrue(stopped['runtime_retained'])
        result=await self.tool('pi_agent_result',{'run_id':next_run['run_id']})
        self.assertEqual(result['run']['state'],'interrupted')

    async def test_discovery_and_structured_output_contracts(self):
        await self.initialize()
        listed=await self.rpc('tools/list')
        names={t['name'] for t in listed['result']['tools']}
        self.assertIn('pi_interrupt_agent',names); self.assertNotIn('pi_close_agent',names)
        self.assertNotIn('pi_send_input',names); self.assertNotIn('pi_respawn_agent',names)
        self.assertTrue(all('outputSchema' in t for t in listed['result']['tools']))
        calls=[('pi_spawn_agent',{'cwd':str(self.workspace),'task':'delay=1|first','access':'read','name':'named','request_id':'a'})]
        run=None
        for name,args in calls:
            r=await self.rpc('tools/call',{'name':name,'arguments':args}); value=self.unpack(r)
            self.assertFalse(r['result']['isError'],value); self.assertEqual(value,r['result']['structuredContent'])
            validate(value,BY_NAME[name]['outputSchema']); run=value
        for name,args in [('pi_followup_task',{'agent_id':'named','message':'native','request_id':'b'}),
                          ('pi_interrupt_agent',{'agent_id':'named','request_id':'c'}),
                          ('pi_list_agents',{}),
                          ('pi_wait_agent',{'run_ids':[run['run_id']],'timeout_seconds':0})]:
            r=await self.rpc('tools/call',{'name':name,'arguments':args}); value=self.unpack(r)
            self.assertFalse(r['result']['isError'],value); self.assertEqual(value,r['result']['structuredContent'])
            validate(value,BY_NAME[name]['outputSchema'])
