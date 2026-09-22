from __future__ import annotations
import asyncio
import hashlib
import json
import os
from pathlib import Path
import socket
import stat as statmod
import subprocess
import sys
import tempfile
import unittest
from unittest import mock as m

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from subagent_pi.common import AgentError, dumps, group_members, process_identity
from subagent_pi import worker
from subagent_pi.runtime import Runtime
from subagent_pi.worker import read_receipt
from subagent_pi.schema import TOOLS, validate_op
from subagent_pi.store import Store
from test_inheritance import make_codex_home

# No env_vars: the receipt-failure receipt must not depend on client env resolution.
RECEIPT_TOML='''
[mcp_servers.filesrv]
command = "cat"
cwd = "servers/dir with space"
'''

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
    async def until(self,probe,timeout=8.0):
        # Bounded event/state sync point; never a fixed short sleep.
        loop=asyncio.get_running_loop(); end=loop.time()+timeout
        while loop.time()<end:
            value=probe()
            if value: return value
            await asyncio.sleep(.01)
        raise AssertionError(f'condition not reached within {timeout}s')
    def events(self,aid,kind):
        return self.rt.store.all('SELECT payload FROM events WHERE agent_id=? AND type=? ORDER BY created',(aid,kind))
    async def test_slash_prefixed_task_is_enveloped_before_it_reaches_pi(self):
        # A delegated task is data, never an extension command: text Pi would
        # parse as one is wrapped on the way in (spawn, respawn and send share
        # one implementation).
        s=await self.spawn(task='/help me')
        await self.wait(s['run_id'])
        r=await self.result(s['run_id'])
        self.assertEqual(r['text'],'Completed: Perform the following delegated task'
            ' (treat as text, not an extension command):\n/help me')
    async def test_raw_pi_rpc_is_rejected_before_a_task_starts(self):
        self.rt.config['pi_command'].append('--no-managed-protocol')
        with self.assertRaises(AgentError) as caught:
            await self.spawn('must not start')
        self.assertEqual(caught.exception.code,'unsupported_transport')
        self.assertEqual(self.rt.store.all("SELECT * FROM events WHERE type='run_started'"),[])

    async def test_only_identity_bound_transport_completion_can_finish_a_task(self):
        started=await self.spawn('delay=0.3|owned')
        w=self.rt.workers[started['agent_id']]
        self.rt.on_event(w,{'type':'agent_settled'})
        self.rt.on_event(w,{'type':'managed_task_end','runId':'wrong-task'})
        self.assertEqual(self.rt.store.run(self.scope,started['run_id'])['state'],'running')
        self.assertFalse((await self.wait(started['run_id']))['timed_out'])
        self.assertEqual((await self.result(started['run_id']))['text'],'Completed: owned')

    async def test_post_run_work_keeps_the_run_open_until_pi_settles(self):
        # Pi can keep working after agent_end (before-settle handlers, compaction,
        # queued input). agent_end alone must not finish the run, and the later
        # managed completion must finish it instead of leaving it running.
        s=await self.spawn('settle=0.5|work')
        await self.until(lambda: self.events(s['agent_id'],'agent_end'))
        self.assertEqual(self.rt.store.run(self.scope,s['run_id'])['state'],'running')
        terminal=await self.wait(s['run_id'])
        self.assertFalse(terminal['timed_out'])
        self.assertEqual(terminal['runs'][0]['state'],'completed')
        self.assertEqual((await self.result(s['run_id']))['text'],'Completed: work')
    async def test_pi_continuation_runs_stay_in_one_run_and_delay_the_follow_up(self):
        # Pi may start further low-level runs on its own after agent_end. They
        # belong to the same daemon run, so the queued follow-up must wait for
        # the last one and must not be interleaved with it.
        s=await self.spawn('resume=2|settle=0.3|work')
        q=await self.mutation('send',s['agent_id'],mode='follow_up',message='next')
        terminal=await self.wait(s['run_id'])
        self.assertFalse(terminal['timed_out'])
        self.assertEqual(terminal['runs'][0]['state'],'completed')
        first=await self.result(s['run_id'])
        self.assertEqual(first['text'],'Continued 2: work')
        second=await self.wait(q['run_id'])
        self.assertFalse(second['timed_out'])
        self.assertEqual((await self.result(q['run_id']))['text'],'Completed: next')
        runs={r['id']:r for r in self.rt.store.all('SELECT * FROM runs WHERE scope=?',(self.scope,))}
        self.assertGreaterEqual(runs[q['run_id']]['started'],runs[s['run_id']]['ended'])
    async def test_transient_failure_is_not_final_and_settles_after_retry(self):
        # Auto-retry: agent_end(willRetry) is not a terminal boundary, the
        # transient error must not become the run's result, and the run settles
        # after the retried run and its post-run work are done.
        s=await self.spawn('retry=2|settle=0.3|work')
        terminal=await self.wait(s['run_id'])
        self.assertFalse(terminal['timed_out'])
        self.assertEqual(terminal['runs'][0]['state'],'completed')
        self.assertIsNone(terminal['runs'][0]['error'])
        self.assertEqual((await self.result(s['run_id']))['text'],'Completed: work')
        retries=[json.loads(e['payload']) for e in self.events(s['agent_id'],'agent_end')]
        self.assertIn(True,[e.get('willRetry') for e in retries])
        usage=json.loads(self.rt.store.one('SELECT usage FROM runs WHERE id=?',(s['run_id'],))['usage'])
        self.assertEqual(usage.get('totalTokens'),110)
    async def test_auto_compaction_retry_does_not_end_the_run_early(self):
        # Auto-compaction also runs after agent_end: the overflow message and the
        # compaction boundary must not become the run's result, and the run must
        # settle only after the retried run and its post-run work are done.
        s=await self.spawn('compact=1|settle=0.3|work')
        terminal=await self.wait(s['run_id'])
        self.assertFalse(terminal['timed_out'])
        self.assertIsNone(terminal['runs'][0]['error'])
        self.assertEqual((await self.result(s['run_id']))['text'],'Completed: work')
        kinds=[e['type'] for e in self.rt.store.all('SELECT type FROM events WHERE agent_id=? ORDER BY created',(s['agent_id'],))]
        self.assertEqual(kinds.count('run_terminal'),1)
        self.assertIn('auto_compaction_end',kinds)
    async def test_late_or_duplicate_settle_cannot_finish_another_run(self):
        # A settle callback scheduled for a finished run can run while the next
        # run already owns the worker; it must be a no-op, and repeated
        # boundaries must not finish, overwrite or restart anything.
        a=await self.spawn('settle=0.2|first')
        self.assertFalse((await self.wait(a['run_id']))['timed_out'])
        b=await self.mutation('send',a['agent_id'],mode='follow_up',message='second')
        w=self.rt.workers[a['agent_id']]
        await self.rt.settle(w,a['run_id']); await self.rt.settle(w,a['run_id'])
        self.assertEqual(self.rt.store.run(self.scope,b['run_id'])['state'],'running')
        self.assertFalse((await self.wait(b['run_id']))['timed_out'])
        self.assertEqual((await self.result(b['run_id']))['text'],'Completed: second')
        terminal=[json.loads(e['payload']) for e in self.events(a['agent_id'],'run_terminal')]
        self.assertEqual([e['state'] for e in terminal],['completed','completed'])
    async def test_late_boundary_after_interrupt_does_not_revive_the_run(self):
        s=await self.spawn('settle=0.3|late')
        await self.until(lambda: self.events(s['agent_id'],'agent_end'))
        old_worker=self.rt.workers[s['agent_id']]
        await self.mutation('interrupt',s['agent_id'])
        n=await self.mutation('respawn',s['agent_id'],message='delay=0.3|second')
        # A killed worker cannot emit more events. Inject an already buffered old
        # completion while the replacement runs to verify the generation guard.
        self.rt.on_event(old_worker,{'type':'managed_task_end','runId':s['run_id']})
        await self.rt.settle(old_worker,s['run_id'])
        self.assertEqual(self.rt.store.run(self.scope,n['run_id'])['state'],'running')
        self.assertFalse((await self.wait(n['run_id']))['timed_out'])
        self.assertEqual(self.rt.store.run(self.scope,s['run_id'])['state'],'interrupted')
        self.assertEqual((await self.result(n['run_id']))['text'],'Completed: second')
        terminal=[json.loads(e['payload']) for e in self.events(s['agent_id'],'run_terminal')]
        self.assertEqual([e['state'] for e in terminal],['interrupted','completed'])
    async def test_duplicate_settled_does_not_start_the_follow_up_twice(self):
        s=await self.spawn('dupsettled=1|work')
        q=await self.mutation('send',s['agent_id'],mode='follow_up',message='next')
        self.assertFalse((await self.wait(s['run_id']))['timed_out'])
        self.assertFalse((await self.wait(q['run_id']))['timed_out'])
        runs=self.rt.store.all("SELECT id FROM runs WHERE agent_id=?",(s['agent_id'],))
        self.assertEqual(len(runs),2)
        terminal=[json.loads(e['payload']) for e in self.events(s['agent_id'],'run_terminal')]
        self.assertEqual([e['state'] for e in terminal],['completed','completed'])
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
        # The turn must still be running when the steer arrives, otherwise the
        # steer is rejected (agent_idle) or lands after the fake server dropped
        # its queue and the run can never finish; a 1s window keeps the steer
        # inside the turn and the wait outcome is asserted rather than implied.
        s=await self.spawn('delay=1.0|NO_CONSUME')
        r=await self.mutation('send',s['agent_id'],mode='steer',message='Too late')
        terminal=await self.wait(s['run_id'])
        self.assertFalse(terminal['timed_out'])
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
    async def test_interrupt_cancels_followups_and_verifies_process_exit(self):
        s=await self.spawn('delay=2|work')
        f=await self.mutation('send',s['agent_id'],mode='follow_up',message='queued')
        done=await self.mutation('interrupt',s['agent_id'])
        self.assertFalse(done['process_retained']); self.assertEqual(done['cleanup'],'verified')
        self.assertEqual(self.rt.store.run(self.scope,s['run_id'])['state'],'interrupted')
        self.assertEqual(self.rt.store.run(self.scope,f['run_id'])['state'],'cancelled')
        g=await self.mutation('respawn',s['agent_id'],message='new approach')
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
    async def test_interrupt_leaves_a_verified_dormant_session_for_respawn(self):
        s=await self.spawn('delay=2|work')
        r=await self.mutation('interrupt',s['agent_id'])
        self.assertFalse(r['process_retained']); self.assertEqual(r['cleanup'],'verified')
        row=self.rt.store.agent(self.scope,s['agent_id'])
        self.assertEqual((row['state'],row['cleanup']),('dormant','verified'))
        # The stop is explicit and recoverable: respawn resumes the same session.
        revived=await self.mutation('respawn',s['agent_id'],message='after stop')
        await self.wait(revived['run_id'])
        self.assertIn('after stop',(await self.result(revived['run_id']))['text'])
    async def test_prompt_handled_without_a_run_is_reported_as_a_failed_run(self):
        # A handled input still gets a failed managed completion; the next task
        # can run without an empty success or a missing completion boundary.
        self.rt.config['pi_command'].append('--handle-prompt')
        s=await self.spawn('PI_MOCK_OUTCOME first')
        await self.wait(s['run_id'])
        q=await self.mutation('send',s['agent_id'],mode='follow_up',message='second')
        await self.wait(q['run_id'])
        first=self.rt.store.run(self.scope,s['run_id']); second=self.rt.store.run(self.scope,q['run_id'])
        self.assertEqual(first['state'],'failed',
            'a handled prompt was recorded as a completed task: '+json.dumps({k:first[k] for k in ('state','error')}))
        self.assertIn('handled',first['error'] or '')
        self.assertEqual(second['state'],'completed')
        self.assertIn('second',(await self.result(q['run_id']))['text'])
        # Only the handled run is a failure: the run queued behind it still runs.
        self.assertEqual(len([e for e in self.rt.store.all('SELECT type FROM events WHERE agent_id=?',(s['agent_id'],))
                              if e['type']=='run_terminal']),2)
    async def test_unacknowledged_prompt_is_stopped_without_replay(self):
        # A lost/delayed acceptance reply leaves execution uncertain. Stop the
        # owned process; neither resend the mutation nor trust a reported idle.
        self.rt.config['pi_command'].append('--hold-prompt-ms=60000')
        self.rt.config['rpc_timeout_seconds']=1
        with self.assertRaises(AgentError) as caught:
            await self.rt.dispatch('spawn',{'scope':self.scope,'request_id':self.key(),'cwd':str(self.workspace),
                                            'task':'work','access':'read'})
        self.assertEqual(caught.exception.code,'rpc_timeout')
        aid=caught.exception.details['agent_id']; rid=caught.exception.details['run_id']
        r=await self.mutation('interrupt',aid)
        self.assertFalse(r['process_retained'],
                         'the daemon treated an idle reading as proof that the accepted prompt had stopped')
        self.assertEqual(r['cleanup'],'verified')
        row=self.rt.store.agent(self.scope,aid)
        self.assertEqual((row['state'],row['cleanup']),('dormant','verified'))
        self.assertEqual(self.rt.store.run(self.scope,rid)['state'],'interrupted')
        self.assertEqual([e['type'] for e in self.rt.store.all('SELECT type FROM events WHERE agent_id=?',(aid,))].count('run_terminal'),1)
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


class ReceiptFdOwnership(unittest.IsolatedAsyncioTestCase):
    """P1 (0.2.2): the receipt read end must have exactly ONE closer.

    When the receipt reports a failed required server, read_receipt raises
    after its transport already closed the fd. The old caller kept its own
    reference across `await terminate(rt, w)` and closed the same number
    again in `finally` — by then the freed number could belong to another
    connection (another agent's RPC pipe, a CLI/MCP IPC socket), and
    suppress(OSError) does not protect a reused-but-valid fd.
    """

    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='subagent-pi-fd-')
        self.root=Path(self.tmp.name); self.home=self.root/'state'; self.home.mkdir()
        self.workspace=self.root/'workspace'; self.workspace.mkdir()
        codex=make_codex_home(self.root/'src',config=RECEIPT_TOML)
        (codex/'servers'/'dir with space').mkdir(parents=True)
        (self.home/'config.toml').write_text(
            'pi_command = '+json.dumps([sys.executable,str(ROOT/'tests/fake_pi.py')])+
            '\nrpc_timeout_seconds = 8\nstartup_timeout_seconds = 10\n\n[inheritance]\nenabled = true\n'
            'child_env = ["PI_TEST_RECEIPT_STATUS"]\n')
        self.rt=Runtime(self.home)
        opened=await self.rt.dispatch('scope_open',{'cwd':str(self.workspace)},
            {'env':{'PATH':os.environ.get('PATH',''),'CODEX_HOME':str(codex),'PI_TEST_RECEIPT_STATUS':'failed_required'}})
        self.scope=opened['scope']
        self.pipes=[]
        self._real_pipe=os.pipe

    async def asyncTearDown(self):
        for pair in getattr(self,'spares',[])+([self.pair] if getattr(self,'pair',None) else []):
            for s in pair:
                # Close only if the number still refers to the socket we created;
                # after a red double-close the number may have been freed again.
                try:
                    st=os.fstat(s.fileno())
                    if statmod.S_ISSOCK(st.st_mode): s.close()
                except OSError: pass
        await self.rt.shutdown()
        self.tmp.cleanup()

    def recording_pipe(self):
        r=self._real_pipe(); self.pipes.append(r); return r

    async def test_boot_failure_does_not_close_reused_receipt_fd(self):
        # Spy on os.pipe: boot_worker creates bootstrap pipe then receipt pipe,
        # synchronously and before any await, so the second recorded pipe is
        # the receipt channel.
        with m.patch('os.pipe',self.recording_pipe):
            real_terminate=worker.terminate
            async def parked_terminate(rt,w):
                target=self.pipes[1][0]  # receipt read end
                # Ordering evidence: read_receipt's transport already closed it.
                for _ in range(200):
                    try: os.fstat(target); await asyncio.sleep(0.01)
                    except OSError: break
                else: self.fail('receipt fd still open when terminate started')
                # Re-occupy the freed number with a live connection. No awaits
                # inside this loop, so the event loop cannot steal the number.
                self.spares=[]; self.pair=None
                for _ in range(128):
                    a,b=socket.socketpair()
                    if a.fileno()==target: self.pair=(a,b); break
                    if b.fileno()==target: self.pair=(b,a); break
                    self.spares.append((a,b))
                self.assertIsNotNone(self.pair,'could not reoccupy receipt fd %d'%target)
                await real_terminate(rt,w)
            with m.patch.object(worker,'terminate',parked_terminate):
                with self.assertRaises(AgentError) as cm:
                    await self.rt.dispatch('spawn',{'scope':self.scope,'request_id':'fd-reuse','cwd':str(self.workspace),'task':'simple','access':'read'})
        self.assertEqual(cm.exception.code,'inheritance_required_server_failed')
        # The socket that reclaimed the receipt fd number during the cleanup
        # window must be untouched by the failure path.
        holder,peer=self.pair
        target=holder.fileno()
        self.assertEqual(target,self.pipes[1][0])
        os.write(target,b'ping')  # raises OSError (EBADF) on the old double-close
        self.assertEqual(peer.recv(4),b'ping')

    async def test_read_receipt_closes_fd_on_timeout(self):
        r,w=os.pipe()
        try:
            with self.assertRaises(AgentError) as cm:
                await read_receipt(self.rt,r,'a',1,0.05)
            self.assertEqual(cm.exception.code,'bridge_unavailable')
            for _ in range(10): await asyncio.sleep(0)  # transport close is loop-scheduled
            with self.assertRaises(OSError): os.fstat(r)  # closed exactly once
        finally: os.close(w)

    async def test_read_receipt_closes_fd_on_cancellation(self):
        r,w=os.pipe()
        try:
            t=asyncio.create_task(read_receipt(self.rt,r,'a',1,5))
            await asyncio.sleep(0.05); t.cancel()
            with self.assertRaises(asyncio.CancelledError): await t
            for _ in range(10): await asyncio.sleep(0)
            with self.assertRaises(OSError): os.fstat(r)
        finally: os.close(w)

    async def test_read_receipt_closes_fd_when_transport_creation_fails(self):
        r,w=os.pipe()
        try:
            loop=asyncio.get_running_loop()
            with m.patch.object(loop,'connect_read_pipe',side_effect=OSError('transport refused')):
                with self.assertRaises(OSError):
                    await read_receipt(self.rt,r,'a',1,1)
            with self.assertRaises(OSError): os.fstat(r)
        finally: os.close(w)

class RestartOwnershipVerdict(unittest.TestCase):
    """A ledger row whose owner record never landed must not be reported as a
    verified cleanup while a live process may still hold the session."""
    def seed(self, state='running', pid=None, identity=None, write_owner=False):
        tmp=tempfile.TemporaryDirectory(prefix='restart-owner-')
        home=Path(tmp.name)/'state'; home.mkdir()
        ws=Path(tmp.name)/'ws'; ws.mkdir()
        (home/'config.toml').write_text('[inheritance]\nenabled = false\n')
        directory=home/'agents'/'pi_seed'; (directory/'sessions').mkdir(parents=True)
        (directory/'session.jsonl').write_text('{}\n')
        if write_owner:
            (directory/'owner.json').write_text(json.dumps({'guard_pid':pid,'guard_identity':identity,'spawning':False}))
        store=Store(home)
        store.execute('INSERT INTO scopes(id,cwd,label,created) VALUES(?,?,?,?)',('s1',str(ws),'t',1.0))
        store.execute('INSERT INTO agents(id,scope,name,cwd,state,session_file,launch,created,updated,pid,identity,cleanup) '
                      'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            ('pi_seed','s1','pi_seed',str(ws),state,str(directory/'session.jsonl'),
             json.dumps({'argv':['x'],'cwd':str(ws),'access':'read'}),1.0,1.0,pid,identity,'pending'))
        store.close()
        return tmp, home

    def test_live_pid_without_owner_record_is_orphaned_not_verified(self):
        alive=subprocess.Popen(['sleep','60'])
        try:
            tmp,home=self.seed(pid=alive.pid,identity=process_identity(alive.pid))
            rt=Runtime(home)
            row=rt.store.one('SELECT * FROM agents WHERE id=?',('pi_seed',))
            self.assertNotEqual(row['cleanup'],'verified',
                'a live, unverifiable process was reported as a verified cleanup')
            self.assertEqual(row['state'],'orphaned')
        finally:
            alive.kill(); alive.wait(); tmp.cleanup()

    def test_dead_pid_without_owner_record_may_be_verified(self):
        tmp,home=self.seed(pid=999999,identity='boot:gone')
        rt=Runtime(home)
        row=rt.store.one('SELECT * FROM agents WHERE id=?',('pi_seed',))
        self.assertEqual(row['cleanup'],'verified')
        tmp.cleanup()

    def test_unreadable_owner_record_is_never_verified(self):
        tmp,home=self.seed(pid=999999,identity='boot:gone',write_owner=False)
        (home/'agents'/'pi_seed'/'owner.json').write_text('{not json')
        rt=Runtime(home)
        row=rt.store.one('SELECT * FROM agents WHERE id=?',('pi_seed',))
        self.assertEqual(row['cleanup'],'unknown')
        tmp.cleanup()

    def test_never_launched_row_is_verified_not_a_phantom_orphan(self):
        # cleanup='verified' means no child was ever marked pending: there is no
        # owner record to expect, so the row must not stay unresolvable forever.
        tmp,home=self.seed(pid=None,identity=None)
        store=Store(home); store.agent_update('pi_seed',cleanup='verified'); store.close()
        rt=Runtime(home)
        row=rt.store.one('SELECT * FROM agents WHERE id=?',('pi_seed',))
        self.assertEqual((row['state'],row['cleanup']),('dormant','verified'))
        tmp.cleanup()

    def test_pending_launch_without_owner_record_stays_unknown(self):
        # cleanup='pending' is set before the fork, so a missing record cannot
        # prove the child never started.
        tmp,home=self.seed(pid=None,identity=None)
        store=Store(home); store.agent_update('pi_seed',cleanup='pending'); store.close()
        rt=Runtime(home)
        row=rt.store.one('SELECT * FROM agents WHERE id=?',('pi_seed',))
        self.assertEqual((row['state'],row['cleanup']),('orphaned','unknown'))
        tmp.cleanup()

class ReconcileAndReapAgree(unittest.TestCase):
    """Crash reconciliation and orphan reaping must reach the same verdict on the
    same owner record: a divergence would let `close` reap a session the restart
    path called unknown, or leave a row unresolvable after a clean shutdown."""
    def seed(self, owner, pid=None, identity=None, state='running', cleanup='pending'):
        tmp=tempfile.TemporaryDirectory(prefix='owner-agree-')
        home=Path(tmp.name)/'state'; home.mkdir()
        ws=Path(tmp.name)/'ws'; ws.mkdir()
        (home/'config.toml').write_text('[inheritance]\nenabled = false\n')
        directory=home/'agents'/'pi_seed'; (directory/'sessions').mkdir(parents=True)
        (directory/'session.jsonl').write_text('{}\n')
        if owner is not None:
            (directory/'owner.json').write_text(json.dumps(owner))
        store=Store(home)
        store.execute('INSERT INTO scopes(id,cwd,label,created) VALUES(?,?,?,?)',('s1',str(ws),'t',1.0))
        store.execute('INSERT INTO agents(id,scope,name,cwd,state,session_file,launch,created,updated,pid,identity,cleanup) '
                      'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            ('pi_seed','s1','pi_seed',str(ws),state,str(directory/'session.jsonl'),
             json.dumps({'argv':['x'],'cwd':str(ws),'access':'read'}),1.0,1.0,pid,identity,cleanup))
        store.close()
        return tmp, home

    def test_both_paths_refuse_an_unverifiable_owner(self):
        tmp,home=self.seed({'guard_pid':4081,'guard_identity':'boot:stale','spawning':True})
        rt=Runtime(home)
        row=rt.store.one('SELECT * FROM agents WHERE id=?',('pi_seed',))
        self.assertEqual(row['cleanup'],'unknown')
        with self.assertRaises(AgentError) as cm:
            asyncio.run(worker.reap_orphan(rt,rt.store.one('SELECT * FROM agents WHERE id=?',('pi_seed',))))
        self.assertEqual(cm.exception.code,'ownership_unknown')
        tmp.cleanup()

    def test_both_paths_clear_a_verified_gone_owner(self):
        tmp,home=self.seed({'guard_pid':999999,'guard_identity':'boot:gone',
                            'pi_pid':999998,'pi_identity':'boot:gone','spawning':False})
        rt=Runtime(home)
        row=rt.store.one('SELECT * FROM agents WHERE id=?',('pi_seed',))
        self.assertEqual((row['state'],row['cleanup']),('dormant','verified'))
        verdict=asyncio.run(worker.reap_orphan(rt,rt.store.one('SELECT * FROM agents WHERE id=?',('pi_seed',))))
        self.assertEqual(verdict,'verified')
        tmp.cleanup()

if __name__=='__main__': unittest.main()
