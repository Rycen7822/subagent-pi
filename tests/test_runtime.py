from __future__ import annotations
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from subagent_pi.common import AgentError, dumps, group_members
from subagent_pi.runtime import Runtime
from subagent_pi.schema import TOOLS, validate, validate_op

class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='subagent-pi-test-')
        self.root=Path(self.tmp.name); self.home=self.root/'state'; self.home.mkdir()
        self.workspace=self.root/'workspace'; self.workspace.mkdir()
        self.fake=ROOT/'tests/fake_pi.py'
        (self.home/'config.toml').write_text('pi_command = '+json.dumps([sys.executable,str(self.fake)])+'\nrpc_timeout_seconds = 8\nstartup_timeout_seconds = 10\n[inheritance]\nenabled = false\n')
        self.rt=Runtime(self.home)
        self.scope=(await self.rt.dispatch('scope_open',{'cwd':str(self.workspace)}))['scope']
        self.n=0
    async def asyncTearDown(self):
        await self.rt.shutdown()
        self.tmp.cleanup()
    def key(self):
        self.n+=1; return 'request-'+str(self.n)
    async def spawn(self,task='simple',**extra):
        p={'scope':self.scope,'request_id':self.key(),'cwd':str(self.workspace),'task':task,'access':'read',**extra}
        return await self.rt.dispatch('spawn',p)
    async def mutation(self,op,aid,**extra):
        return await self.rt.dispatch(op,{'scope':self.scope,'agent_id':aid,'request_id':self.key(),**extra})
    async def wait(self,rid):
        return await self.rt.dispatch('wait',{'scope':self.scope,'run_ids':[rid],'mode':'all','timeout_ms':4000})
    async def result(self,rid,**extra):
        return await self.rt.dispatch('result',{'scope':self.scope,'run_id':rid,**extra})
    async def test_spawn_wait_result_and_ack(self):
        s=await self.spawn(); terminal=await self.wait(s['run_id'])
        self.assertFalse(terminal['timed_out']); self.assertEqual(terminal['runs'][0]['state'],'completed')
        r=await self.result(s['run_id']); self.assertEqual(r['text'],'Completed: simple')
        self.assertFalse(r['acknowledged'])
        await self.rt.dispatch('ack',{'scope':self.scope,'run_id':s['run_id'],'request_id':self.key(),'result_sha256':r['result_sha256']})
        self.assertTrue((await self.result(s['run_id']))['acknowledged'])
    async def test_spawn_idempotency(self):
        p={'scope':self.scope,'request_id':'same','cwd':str(self.workspace),'task':'simple','access':'read'}
        a=await self.rt.dispatch('spawn',p); b=await self.rt.dispatch('spawn',p)
        self.assertEqual(a['agent_id'],b['agent_id']); self.assertTrue(b['replayed'])
        self.assertEqual(len(self.rt.workers),1)
        with self.assertRaises(AgentError) as cm: await self.rt.dispatch('spawn',{**p,'task':'different'})
        self.assertEqual(cm.exception.code,'idempotency_conflict')
    async def test_concurrent_duplicate_spawns(self):
        p={'scope':self.scope,'request_id':'concurrent','cwd':str(self.workspace),'task':'simple','access':'read'}
        a,b=await asyncio.gather(self.rt.dispatch('spawn',p),self.rt.dispatch('spawn',p))
        self.assertEqual(a['agent_id'],b['agent_id'])
    async def test_steering_consumed_receipt(self):
        s=await self.spawn('delay=0.4|work')
        r=await self.mutation('send',s['agent_id'],mode='steer',message='Only inspect')
        await self.wait(s['run_id'])
        receipt=self.rt.store.one('SELECT * FROM receipts WHERE id=?',(r['receipt_id'],))
        self.assertEqual(receipt['state'],'consumed')
    async def test_unconsumed_steering_is_not_success(self):
        s=await self.spawn('NO_CONSUME')
        r=await self.mutation('send',s['agent_id'],mode='steer',message='Too late')
        await self.wait(s['run_id'])
        state=self.rt.store.one('SELECT state FROM receipts WHERE id=?',(r['receipt_id'],))['state']
        self.assertEqual(state,'not_consumed')
    async def test_followup_is_separate_durable_run(self):
        s=await self.spawn('delay=0.2|first')
        f=await self.mutation('send',s['agent_id'],mode='follow_up',message='second')
        self.assertEqual(f['state'],'queued'); self.assertNotEqual(f['run_id'],s['run_id'])
        await self.wait(f['run_id'])
        self.assertEqual((await self.result(s['run_id']))['text'],'Completed: first')
        self.assertEqual((await self.result(f['run_id']))['text'],'Completed: second')
    async def test_send_busy_rejected(self):
        s=await self.spawn('delay=0.5|work')
        with self.assertRaises(AgentError) as cm: await self.mutation('send',s['agent_id'],mode='send',message='bad')
        self.assertEqual(cm.exception.code,'agent_busy')
    async def test_idle_steer_does_not_spawn(self):
        s=await self.spawn(); await self.wait(s['run_id'])
        with self.assertRaises(AgentError) as cm: await self.mutation('send',s['agent_id'],mode='steer',message='bad')
        self.assertEqual(cm.exception.code,'agent_idle'); self.assertEqual(len(self.rt.workers),1)
    async def test_interrupt_cancels_followups_and_keeps_process(self):
        s=await self.spawn('delay=2|work')
        f=await self.mutation('send',s['agent_id'],mode='follow_up',message='queued')
        done=await self.mutation('interrupt',s['agent_id'])
        self.assertTrue(done['process_retained'])
        self.assertEqual(self.rt.store.run(self.scope,s['run_id'])['state'],'interrupted')
        self.assertEqual(self.rt.store.run(self.scope,f['run_id'])['state'],'cancelled')
        g=await self.mutation('send',s['agent_id'],mode='send',message='new approach')
        await self.wait(g['run_id']); self.assertIn('new approach',(await self.result(g['run_id']))['text'])
    async def test_interrupt_then_send(self):
        s=await self.spawn('delay=2|old')
        f=await self.mutation('send',s['agent_id'],message='replacement',interrupt=True)
        await self.wait(f['run_id'])
        self.assertEqual((await self.result(s['run_id']))['run']['state'],'interrupted')
        self.assertEqual((await self.result(f['run_id']))['text'],'Completed: replacement')
    async def test_wait_timeout_never_cancels(self):
        s=await self.spawn('delay=0.5|work')
        result=await self.rt.dispatch('wait',{'scope':self.scope,'run_ids':[s['run_id']],'timeout_ms':10})
        self.assertTrue(result['timed_out']); self.assertEqual(result['runs'][0]['state'],'running')
        await self.wait(s['run_id'])
    async def test_cancel_wait_never_cancels(self):
        s=await self.spawn('delay=0.3|work')
        task=asyncio.create_task(self.wait(s['run_id'])); await asyncio.sleep(.02); task.cancel()
        await asyncio.gather(task,return_exceptions=True)
        self.assertEqual(self.rt.store.run(self.scope,s['run_id'])['state'],'running')
        await self.wait(s['run_id'])
    async def test_wait_any_all(self):
        a=await self.spawn('delay=0.05|A'); b=await self.spawn('delay=0.4|B')
        ids=[a['run_id'],b['run_id']]
        first=await self.rt.dispatch('wait',{'scope':self.scope,'run_ids':ids,'mode':'any','timeout_ms':3000})
        self.assertTrue(any(r['state']=='completed' for r in first['runs']))
        last=await self.rt.dispatch('wait',{'scope':self.scope,'run_ids':ids,'mode':'all','timeout_ms':3000})
        self.assertTrue(all(r['state']=='completed' for r in last['runs']))
    async def test_foreign_scope_is_rejected(self):
        s=await self.spawn()
        other=(await self.rt.dispatch('scope_open',{'cwd':str(self.workspace)}))['scope']
        with self.assertRaises(AgentError): await self.rt.dispatch('inspect',{'scope':other,'agent_id':s['agent_id']})
    async def test_scope_cwd_validation(self):
        with self.assertRaises(AgentError): await self.rt.dispatch('scope_open',{'scope':self.scope,'cwd':str(self.root)})
    async def test_spawn_outside_scope_rejected(self):
        with self.assertRaises(AgentError): await self.spawn(cwd=str(self.root))
    async def test_writer_conflict(self):
        await self.spawn('delay=2|writer',access='write')
        with self.assertRaises(AgentError) as cm: await self.spawn('writer2',access='write')
        self.assertEqual(cm.exception.code,'writer_conflict')
    async def test_wrong_result_hash_rejected(self):
        s=await self.spawn(); await self.wait(s['run_id'])
        with self.assertRaises(AgentError): await self.rt.dispatch('ack',{'scope':self.scope,'run_id':s['run_id'],'request_id':self.key(),'result_sha256':'bad'})
    async def test_result_read_does_not_ack(self):
        s=await self.spawn(); await self.wait(s['run_id']); await self.result(s['run_id'])
        listing=await self.rt.dispatch('list',{'scope':self.scope})
        self.assertEqual(listing['outstanding']['total'],1)
    async def test_result_utf8_pagination_and_hash(self):
        s=await self.spawn('BIG'); await self.wait(s['run_id'])
        cursor=0; chunks=[]
        while True:
            r=await self.result(s['run_id'],offset=cursor,max_bytes=4096); chunks.append(r['text'])
            self.assertGreater(r['next_offset'],cursor)
            cursor=r['next_offset']
            if not r['has_more']: break
        body=''.join(chunks)
        self.assertEqual(hashlib.sha256(body.encode()).hexdigest(),r['result_sha256'])
        self.assertIn('\u2028',body)
    async def test_bounded_inspection_and_stable_cursor(self):
        s=await self.spawn('BIG'); await self.wait(s['run_id'])
        seen=[]; cursor=0
        for _ in range(50):
            r=await self.rt.dispatch('inspect',{'scope':self.scope,'agent_id':s['agent_id'],'after':cursor,'limit':2,'max_bytes':4096,'detail':'full'})
            self.assertLessEqual(len(dumps(r).encode()),4096)
            seen += [e['seq'] for e in r['events']]; cursor=r['next_cursor']
            if not r['has_more']: break
        self.assertEqual(len(seen),len(set(seen))); self.assertGreater(len(seen),2)
    async def test_close_respawn_keeps_identity_and_session(self):
        s=await self.spawn(); await self.wait(s['run_id'])
        a=self.rt.store.agent(self.scope,s['agent_id']); session=a['session_file']
        await self.mutation('close',s['agent_id'])
        revived=await self.mutation('respawn',s['agent_id'],message='continued')
        self.assertEqual(revived['generation'],2)
        self.assertEqual(self.rt.store.agent(self.scope,s['agent_id'])['session_file'],session)
        await self.wait(revived['run_id'])
        self.assertIn('continued',(await self.result(revived['run_id']))['text'])
    async def test_respawn_live_refused(self):
        s=await self.spawn()
        with self.assertRaises(AgentError) as cm: await self.mutation('respawn',s['agent_id'])
        self.assertEqual(cm.exception.code,'worker_alive')
    async def test_crash_not_completed(self):
        s=await self.spawn('CRASH'); r=await self.wait(s['run_id'])
        self.assertEqual(r['runs'][0]['state'],'crashed')
    async def test_close_kills_owned_descendants(self):
        s=await self.spawn('SPAWN_CHILD'); await asyncio.sleep(.15)
        pid=self.rt.workers[s['agent_id']].proc.pid
        self.assertGreaterEqual(len(group_members(pid)),2)
        r=await self.mutation('close',s['agent_id'])
        self.assertEqual(r['cleanup'],'verified'); self.assertEqual(group_members(pid),[])
    async def test_unknown_clear_queue_falls_back_to_hard_stop(self):
        self.rt.config['pi_command'].append('--no-clear')
        s=await self.spawn('delay=2|work')
        r=await self.mutation('interrupt',s['agent_id'])
        self.assertFalse(r['process_retained']); self.assertEqual(r['cleanup'],'verified')
    async def test_abort_success_but_busy_is_not_trusted(self):
        self.rt.config['pi_command'].append('--ignore-abort')
        s=await self.spawn('delay=2|work')
        r=await self.mutation('interrupt',s['agent_id'])
        self.assertFalse(r['process_retained'])
    async def test_needs_input_and_explicit_answer(self):
        s=await self.spawn('UI_CONFIRM'); r=await self.wait(s['run_id'])
        self.assertEqual(r['runs'][0]['state'],'needs_input')
        await self.mutation('answer',s['agent_id'],ui_request_id='ui-1',answer=False)
        await self.wait(s['run_id']); self.assertIn('False',(await self.result(s['run_id']))['text'])
    async def test_deadline_interrupts_not_success(self):
        loop=asyncio.create_task(self.rt.deadline_loop())
        try:
            s=await self.spawn('delay=5|work',timeout_seconds=1)
            r=await self.wait(s['run_id']); self.assertEqual(r['runs'][0]['state'],'timed_out')
        finally: loop.cancel(); await asyncio.gather(loop,return_exceptions=True)
    async def test_missing_pi_reports_startup_failure(self):
        self.rt.config['pi_command']=['/definitely/missing/pi']
        with self.assertRaises(AgentError) as cm: await self.spawn()
        self.assertEqual(cm.exception.code,'pi_not_found')
    async def test_schema_validation(self):
        with self.assertRaises(AgentError): validate_op('spawn',{'scope':self.scope,'task':'x'})
        with self.assertRaises(AgentError): validate_op('list',{'scope':self.scope,'limit':True})
        with self.assertRaises(AgentError): validate_op('list',{'scope':self.scope,'unexpected':1})
        self.assertEqual(len({t['name'] for t in TOOLS}),len(TOOLS))

    async def test_respawn_cannot_bypass_new_writer(self):
        a=await self.spawn('first',access='write'); await self.wait(a['run_id'])
        await self.mutation('close',a['agent_id'])
        b=await self.spawn('delay=1|second',access='write')
        with self.assertRaises(AgentError) as cm: await self.mutation('respawn',a['agent_id'])
        self.assertEqual(cm.exception.code,'writer_conflict')
    async def test_relative_scope_cwd_rejected(self):
        with self.assertRaises(AgentError): await self.rt.dispatch('scope_open',{'cwd':'.'})
    async def test_mid_codepoint_result_offset_rejected(self):
        a=await self.spawn('BIG'); await self.wait(a['run_id'])
        with self.assertRaises(AgentError) as cm: await self.result(a['run_id'],offset=1)
        self.assertEqual(cm.exception.code,'invalid_offset')
    async def test_inspection_strict_small_budget(self):
        a=await self.spawn('BIG'); await self.wait(a['run_id'])
        r=await self.rt.dispatch('inspect',{'scope':self.scope,'agent_id':a['agent_id'],'max_bytes':1024,'detail':'full'})
        self.assertLessEqual(len(dumps(r).encode()),1024)
    async def test_pending_request_never_blindly_reexecutes(self):
        params={'scope':self.scope,'request_id':'uncertain','cwd':str(self.workspace),'task':'simple','access':'read'}
        self.rt.store.request_begin(self.scope,'uncertain','spawn',params)
        with self.assertRaises(AgentError) as cm: await self.rt.dispatch('spawn',params)
        self.assertEqual(cm.exception.code,'request_uncertain')

if __name__=='__main__': unittest.main()
