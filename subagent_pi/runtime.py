"""Daemon-side runtime: the scope/agent/run state machine behind every IPC and MCP
operation. Process mechanics live in worker.py, scope binding in binding.py and the
read projections in views.py; this module owns ledger state transitions."""
from __future__ import annotations
import asyncio
from collections import defaultdict
import contextlib
import json
import os
from pathlib import Path
import shutil
import sys

from . import __version__, PROTOCOL_VERSION, views
from .binding import bind_scope_source, doctor
from .common import (TERMINAL, AgentError, RESIDENT_AGENT_STATES, bounded, crop,
    dumps, group_members, identifier, integer, new_id, now, text)
from .config import load_config, launch_spec
from . import parent
from .store import Store
from .worker import (RESULT_CAP, boot_worker, message_text, ownership,
    reap_orphan, terminate)

def delegated_text(value: str, envelope: str) -> str:
    """A delegated task is data, never a Pi extension command: text that would be
    parsed as a slash command is wrapped before it enters the child, at every
    entry point that sends text into a session."""
    return envelope + value if value.lstrip().startswith('/') else value

class Runtime:
    def __init__(self, home):
        self.home = home
        self.store = Store(home)
        self.config = load_config(home)
        self.workers = {}
        self.scope_env = {}  # bound snapshots; secret values live here and nowhere else
        self.agent_locks = defaultdict(asyncio.Lock)
        self.request_locks = defaultdict(asyncio.Lock)
        self.admission = asyncio.Lock()
        self.changed = asyncio.Condition()
        self.shutdown_requested = asyncio.Event()
        self.closing = False
        self.background = set()
        self.parent_delivery = None
        self._reconcile()

    def spawn_task(self,coro):
        t = asyncio.create_task(coro)
        self.background.add(t)
        def done(t):
            self.background.discard(t)
            if not t.cancelled() and t.exception(): print('runtime task: '+str(t.exception()),file=sys.stderr)
        t.add_done_callback(done)
        return t

    def _reconcile(self):
        """A new daemon cannot recover old pipes; never claim a live orphan is
        reattached. Every resident row is re-judged from its owner record."""
        self.store.execute("UPDATE parent_notifications SET state='unknown',error='Daemon restarted during delivery; not retried' WHERE state='sending'")
        for a in self.store.all('SELECT * FROM agents'):
            verdict = ownership(self.home/'agents'/a['id'], a)
            if a['state'] in RESIDENT_AGENT_STATES:
                # Only a verified-gone owner clears the agent: this daemon cannot
                # reattach a live writer's pipes, so it stays an orphan either way.
                gone = verdict['status']=='gone'
                self.store.agent_update(a['id'],state='dormant' if gone else 'orphaned',
                                        cleanup='verified' if gone else 'unknown')
            for r in self.store.all("SELECT * FROM runs WHERE agent_id=? AND state IN ('running','starting','needs_input','stopping','queued')",(a['id'],)):
                self.store.finish(r['id'],'crashed' if r['state']!='queued' else 'cancelled','',
                    'Daemon restarted; execution outcome may be partial. Inspect the session before explicit recovery.')
            self.store.agent_update(a['id'],current_run=None)
        self.store.execute("UPDATE receipts SET state='unknown' WHERE state IN ('queued','sending')")

    def notify(self):
        async def wake():
            async with self.changed: self.changed.notify_all()
        self.spawn_task(wake())
        if not self.closing and (self.parent_delivery is None or self.parent_delivery.done()):
            self.parent_delivery=self.spawn_task(parent.deliver_pending(self))

    def event(self,w,kind,payload):
        self.store.event(w.agent['id'],w.run_id,w.generation,kind,bounded(payload,4096))
        w.events_written += 1
        if w.events_written % 256 == 0:
            cap = self.config['event_max_count_per_agent']
            self.store.execute('DELETE FROM events WHERE agent_id=? AND seq < COALESCE((SELECT seq FROM events WHERE agent_id=? ORDER BY seq DESC LIMIT 1 OFFSET ?),0)',(w.agent['id'],w.agent['id'],cap-1))

    def on_event(self,w,e):
        a = self.store.one('SELECT * FROM agents WHERE id=?',(w.agent['id'],))
        if not a or a['generation'] != w.generation: return
        kind = e.get('type','unknown')
        w.last_activity = now()
        if kind == 'agent_start':
            # Defense against activity outside the managed SDK task queue.
            # Refuse output immediately, then stop only this owned worker.
            if w.run_id is None and not w.stopping and a['state'] not in {'closed','dormant'}:
                w.tainted = 'unowned_run'
                self.store.agent_update(a['id'],state='dormant',current_run=None)
                self.event(w,'unowned_run_started',{'run_id':None})
                self.spawn_task(self.stop_unowned(w))
                self.notify()
            return
        if w.tainted: return
        if kind == 'extension_error':
            self.event(w,kind,e)
            return
        if kind == 'message_update':
            return  # keep text deltas out of SQLite; message_end is canonical
        if kind in {'tool_execution_start','tool_execution_update','tool_execution_end'}:
            if kind == 'tool_execution_start': w.current_tool=e.get('toolName')
            if kind == 'tool_execution_end': w.current_tool=None
            if kind != 'tool_execution_update': self.event(w,kind,e)
            return
        if kind == 'message_end':
            m=e.get('message',{})
            if not isinstance(m,dict): return
            role=m.get('role')
            mt=message_text(m)
            if role=='assistant':
                if mt:
                    w.last_text=crop(mt,RESULT_CAP)
                    w.usage['result_truncated']=len(mt.encode('utf-8'))>RESULT_CAP
                if m.get('stopReason') in {'error','aborted'}: w.error=crop(str(m.get('errorMessage') or m.get('stopReason')),2000)
                elif m.get('stopReason'): w.error=None
                u=m.get('usage')
                if isinstance(u,dict):
                    for key in ('input','output','cacheRead','cacheWrite','totalTokens'):
                        value=u.get(key)
                        if isinstance(value,(int,float)) and not isinstance(value,bool):
                            w.usage[key]=w.usage.get(key,0)+value
                    cost=u.get('cost')
                    if isinstance(cost,dict) and isinstance(cost.get('total'),(int,float)):
                        w.usage['cost_total']=w.usage.get('cost_total',0)+cost['total']
            if role=='user' and w.run_id:
                receipt=self.store.one("SELECT * FROM receipts WHERE run_id=? AND message=? AND state IN ('queued','sending') ORDER BY created LIMIT 1",(w.run_id,mt))
                if receipt:
                    self.store.execute("UPDATE receipts SET state='consumed',updated=? WHERE id=?",(now(),receipt['id']))
                    self.event(w,'control_consumed',{'request_id':receipt['id'],'evidence':'user_message_text_fifo'})
            self.event(w,'message',{'role':role,'text':crop(mt,3000),'stopReason':m.get('stopReason')})
            return
        if kind=='extension_ui_request':
            if e.get('method') in {'select','confirm','input','editor'} and e.get('id'):
                w.ui[str(e['id'])]=bounded(e,3000)
                self.store.agent_update(a['id'],state='needs_input')
                if w.run_id: self.store.execute("UPDATE runs SET state='needs_input' WHERE id=?",(w.run_id,))
                self.event(w,'needs_input',e)
                if w.run_id: self.store.attention(w.run_id,'question',str(e['id']))
                self.store.bump(a['scope']); self.notify()
            return
        if kind=='agent_end':
            # One low-level run finished. Pi may still retry, compact, run
            # before-settle work or continue with queued input afterwards, so
            # this stays a trajectory record.
            self.event(w,'agent_end',{'run_id':w.run_id,'willRetry':bool(e.get('willRetry'))})
            return
        if kind=='managed_task_end':
            if w.run_id and e.get('runId') == w.run_id:
                if e.get('error'): w.error=crop(str(e['error']),2000)
                self.spawn_task(self.settle(w,w.run_id))
            return
        if kind in {'auto_retry_start','auto_retry_end','auto_compaction_start','auto_compaction_end'}:
            self.event(w,kind,e)

    def assert_writer_exclusive(self, aid, cwd):
        """Only one managed writer may own a cwd subtree at a time (a read label
        strips mutation builtins; it is not OS confinement)."""
        states = RESIDENT_AGENT_STATES
        marks=','.join('?'*len(states))
        q=f"SELECT * FROM agents WHERE id!=? AND (state IN ({marks}) OR cleanup='unknown')"
        for other in self.store.all(q,(aid,*states)):
            if json.loads(other['launch']).get('access')!='write': continue
            left,right=Path(cwd),Path(other['cwd'])
            if left.is_relative_to(right) or right.is_relative_to(left):
                raise AgentError('writer_conflict','Another managed writer owns an overlapping cwd; close it or use read access',agent_id=other['id'])

    def require_worker(self,a):
        w=self.workers.get(a['id'])
        if not w or w.closed or w.tainted: raise AgentError('worker_unavailable','No connected Pi worker; use respawn after checking orphan state')
        return w

    async def start_run(self,w,rid):
        r=self.store.run(w.agent['scope'],rid)
        if r['state'] not in {'queued','starting'}: return
        w.run_id=rid; w.last_text=''; w.error=None; w.usage={}; w.stopping=False; w.ui.clear()
        deadline=r['deadline'] or now()+self.config['default_run_timeout_seconds']
        self.store.execute("UPDATE runs SET state='running',started=?,deadline=? WHERE id=?",(now(),deadline,rid))
        self.store.agent_update(w.agent['id'],state='running',current_run=rid)
        self.store.bump(w.agent['scope'])
        self.event(w,'run_started',{'deadline':deadline})
        receipt=new_id('msg_')
        self.store.execute('INSERT INTO receipts VALUES(?,?,?,?,?,?,?,?)',(receipt,w.agent['id'],rid,w.agent['scope'],r['task'],'sending',now(),now()))
        try:
            # Deliberate natural-language envelope avoids executing a slash command as a task.
            await w.rpc('prompt',message=r['task'],runId=rid)
        except AgentError as e:
            if e.code=='pi_rejected':
                self.store.finish(rid,'failed','',e.message)
                w.run_id=None
                self.store.agent_update(w.agent['id'],state='idle',current_run=None)
                self.notify()
            # Timeouts are uncertain: retain the run and wait for events rather than re-executing.
            raise
        self.store.execute("UPDATE receipts SET state='queued',updated=? WHERE id=? AND state='sending'",(now(),receipt))
        return receipt

    def add_run(self,a,task,state='queued',timeout=None):
        rid=new_id('run_')
        self.store.execute('INSERT INTO runs(id,agent_id,scope,state,task,created,deadline) VALUES(?,?,?,?,?,?,?)',
            (rid,a['id'],a['scope'],state,task,now(),None if timeout is None else now()+timeout))
        self.store.bump(a['scope'])
        return rid

    async def settle(self,w,rid):
        async with self.agent_locks[w.agent['id']]:
            if w.run_id!=rid or w.stopping: return
            # Only the SDK transport's identity-bound task completion can enter
            # here. Per-run Pi events and transient idle readings never settle it.
            state='failed' if w.error else 'completed'
            self.store.finish(rid,state,w.last_text,w.error,w.usage)
            self.event(w,'run_terminal',{'state':state})
            w.run_id=None; w.ui.clear(); w.current_tool=None
            self.store.agent_update(w.agent['id'],state='idle',current_run=None,cleanup='not_checked')
            self.notify()
            q=self.store.one("SELECT id FROM runs WHERE agent_id=? AND state='queued' ORDER BY created LIMIT 1",(w.agent['id'],))
            if q and not self.closing:
                try: await self.start_run(w,q['id'])
                except AgentError as exc: self.event(w,'start_failed',exc.as_dict())

    async def stop_unowned(self,w):
        """Terminate a worker whose host started a run no daemon run owns.

        The interrupted work is not replayed and its session is kept, so the agent
        can be respawned deliberately; what is not allowed is a live process that
        silently continues work the caller was told had stopped.
        """
        async with self.agent_locks[w.agent['id']]:
            a=self.store.one('SELECT * FROM agents WHERE id=?',(w.agent['id'],))
            if not a or a['generation']!=w.generation or w.closed: return
            cleanup=await terminate(self,w)
            self.store.agent_update(a['id'],state='dormant',current_run=None,cleanup=cleanup)
            self.event(w,'worker_stopped',{'reason':'unowned_run','cleanup':cleanup})
            self.store.bump(a['scope']); self.notify()

    async def fail_worker(self,w,error):
        async with self.agent_locks[w.agent['id']]:
            w.stopping=True
            if w.run_id:
                self.store.finish(w.run_id,'failed',w.last_text,error,w.usage); w.run_id=None
            await terminate(self,w)
            self.store.agent_update(w.agent['id'],state='crashed',current_run=None)
            self.notify()

    async def worker_exited(self,w,code):
        async with self.agent_locks[w.agent['id']]:
            a=self.store.one('SELECT * FROM agents WHERE id=?',(w.agent['id'],))
            if not a or a['generation']!=w.generation: return
            if w.run_id:
                self.store.finish(w.run_id,'interrupted' if w.stopping else 'crashed',w.last_text,
                    w.error or f'Pi guard exited with code {code}; inspect partial changes',w.usage)
                w.run_id=None
            for q in self.store.all("SELECT id FROM runs WHERE agent_id=? AND state='queued'",(a['id'],)):
                self.store.finish(q['id'],'cancelled','','Worker exited; queued task was not automatically retried')
            if a['state'] not in {'closed','dormant'}:
                self.store.agent_update(a['id'],state='crashed',current_run=None,
                    cleanup='unknown' if group_members(w.proc.pid) else 'verified')
            self.store.bump(a['scope']); self.notify()

    async def interrupt(self,a,terminal='interrupted'):
        w=self.require_worker(a)
        w.stopping=True
        self.store.agent_update(a['id'],state='stopping')
        for q in self.store.all("SELECT id FROM runs WHERE agent_id=? AND state='queued'",(a['id'],)):
            self.store.finish(q['id'],'cancelled','','Cancelled by explicit interruption')
        # Terminating only this verified process group also cancels input hooks,
        # authentication preflights and extension timers that Pi cannot abort.
        cleanup=await terminate(self,w)
        if w.run_id:
            self.store.finish(w.run_id,terminal,w.last_text,'Explicit interruption; filesystem effects may be partial',w.usage)
            self.event(w,'run_terminal',{'state':terminal})
        w.run_id=None; w.ui.clear(); w.current_tool=None
        self.store.agent_update(a['id'],state='dormant',current_run=None,cleanup=cleanup)
        self.store.bump(a['scope']); self.notify()
        return {'agent_id':a['id'],'state':'dormant','cleanup':cleanup,'process_retained':False}

    async def dispatch(self,op,p,source=None):
        if op=='ping': return {'version':__version__,'protocol':PROTOCOL_VERSION,'pid':os.getpid()}
        if op=='scope_list':
            return {'scopes':self.store.all('SELECT * FROM scopes ORDER BY created DESC LIMIT 100')}
        if op=='scope_open':
            cwd_input=Path(text(p.get('cwd'),'cwd',4096)).expanduser()
            if not cwd_input.is_absolute(): raise AgentError('invalid_cwd','cwd must be absolute; daemon cwd is not the workspace')
            cwd=str(cwd_input.resolve())
            if not Path(cwd).is_dir(): raise AgentError('invalid_cwd','cwd must be an existing directory')
            sid=p.get('scope')
            if sid:
                s=self.store.scope(identifier(sid,'scope'))
                if s['cwd']!=cwd: raise AgentError('scope_mismatch','Scope is bound to a different cwd')
            else:
                sid=new_id('scope_')
                self.store.execute('INSERT INTO scopes(id,cwd,label,created) VALUES(?,?,?,?)',(sid,cwd,text(p.get('label','Codex Pi delegation'),'label',160),now()))
            parent.bind(self,sid,source)
            bind_scope_source(self,sid,p,source)
            return {'scope':sid,'cwd':cwd, 'outstanding':views.outstanding(self,sid),'parent_notifications':parent.status(self,sid)}
        if op=='shutdown':
            if not p.get('force') and any(not w.closed for w in self.workers.values()):
                raise AgentError('agents_present','Close resident agents first or use daemon stop --force')
            self.shutdown_requested.set(); return {'shutdown':'requested'}
        if op=='doctor':
            report={'version':__version__,'platform':sys.platform,'python':sys.version.split()[0],
                    'pi_executable':shutil.which(self.config['pi_command'][0]),'profiles':list(self.config['profiles']),
                    'resident_agents':sum(not w.closed for w in self.workers.values()),
                    'home':str(self.home),'hooks':False,'native_codex_agents_ui':False,
                    'warning':'Managed Pi runs with your OS-user permissions; no inherited Codex sandbox.'}
            if p.get('inheritance'): report['inheritance']=doctor(self)
            return report
        sid=identifier(p.get('scope'),'scope'); scope=self.store.scope(sid)
        if op=='spawn' and 'cwd' not in p: p={**p,'cwd':scope['cwd']}
        if op in {'spawn','send','interrupt','close','respawn','ack','answer'}:
            parent.bind(self,sid,source,allow_new=False)
            key=identifier(p.get('request_id'),'request_id')
            async with self.request_locks[(sid,key)]:
                previous=self.store.request_begin(sid,key,op,p)
                if previous is not None:
                    if 'error' in previous: raise AgentError(**previous['error'])
                    return {**previous,'replayed':True}
                try: result=await self.mutate(op,p)
                except AgentError as e:
                    self.store.request_end(sid,key,{'error':e.as_dict()}); raise
                self.store.request_end(sid,key,result)  # other exceptions leave an uncertain record, never a silent replay
                return result
        if op=='list':
            limit=integer(p.get('limit',20),'limit',1,50)
            rows=self.store.all('SELECT * FROM agents WHERE scope=? ORDER BY created DESC LIMIT ?',(sid,limit))
            total=self.store.one('SELECT COUNT(*) n FROM agents WHERE scope=?',(sid,))['n']
            return {'scope':sid,'agents':[views.brief_agent(self,a) for a in rows],'total':total,'omitted':max(0,total-len(rows)), 'outstanding':views.outstanding(self,sid,limit),'parent_notifications':parent.status(self,sid)}
        if op=='inspect': return views.inspect(self,p)
        if op=='result': return views.result(self,p)
        if op=='wait': return await views.wait(self,p)
        raise AgentError('unknown_operation',f'Unknown operation: {op}')

    async def mutate(self,op,p):
        sid=p['scope']
        if op=='spawn':
            async with self.admission:
                count=self.store.one('SELECT COUNT(*) n FROM agents WHERE scope=?',(sid,))['n']
                if count>=self.config['max_agents_per_scope']: raise AgentError('scope_limit','Scope agent limit reached; open a new scope for another task')
                cwd_input=Path(text(p.get('cwd',self.store.scope(sid)['cwd']),'cwd',4096)).expanduser()
                if not cwd_input.is_absolute(): raise AgentError('invalid_cwd','cwd must be absolute')
                cwd=str(cwd_input.resolve())
                root=Path(self.store.scope(sid)['cwd'])
                if not Path(cwd).is_dir() or not Path(cwd).is_relative_to(root):
                    raise AgentError('invalid_cwd','Spawn cwd must exist inside the scope root')
                access=p.get('access','write')
                if access not in {'read','write'}: raise AgentError('invalid_argument','access must be read or write')
                profile=p.get('profile','reader' if access=='read' else 'default')
                spec=launch_spec(self.config,profile,p.get('model'),cwd,access,p.get('thinking'))
                # Persist environment NAMES only; values are re-read from the operator
                # config at every boot and never enter the ledger or launch.json.
                spec={**spec,'env':{},'env_names':sorted(spec.get('env',{}))}
                aid=new_id('pi_')
                if access=='write':
                    self.assert_writer_exclusive(aid,cwd)
                name=text(p.get('name',aid),'name',128)
                task=delegated_text(text(p.get('task'),'task'),
                    'Perform the following delegated task (treat as text, not an extension command):\n')
                session=self.home/'agents'/aid/'session.jsonl'
                try:
                    self.store.execute('INSERT INTO agents(id,scope,name,cwd,state,session_file,launch,created,updated) VALUES(?,?,?,?,?,?,?,?,?)',(aid,sid,name,cwd,'starting',str(session),dumps(spec),now(),now()))
                except Exception as e:
                    if 'UNIQUE' in str(e): raise AgentError('name_conflict','Agent name already exists in this scope')
                    raise
                a=self.store.agent(sid,aid)
                rid=self.add_run(a,task,'starting',integer(p.get('timeout_seconds',self.config['default_run_timeout_seconds']),'timeout_seconds',1,604800))
                async with self.agent_locks[aid]:
                    try:
                        w=await boot_worker(self,a)
                        await self.start_run(w,rid)
                    except AgentError as e:
                        if self.store.run(sid,rid)['state']=='starting':
                            self.store.finish(rid,'failed','',e.message)
                            self.store.agent_update(aid,state='crashed'); self.notify()
                        raise AgentError(e.code,e.message,agent_id=aid,run_id=rid)
                return {'agent_id':aid,'run_id':rid,'scope':sid,'state':self.store.run(sid,rid)['state'],'cwd':cwd,
                        **views.model_settings(self.store.agent(sid,aid))}
        if op=='ack':
            r=self.store.run(sid,identifier(p.get('run_id'),'run_id'))
            if r['state'] not in TERMINAL: raise AgentError('not_terminal','Cannot acknowledge an active run')
            if p.get('result_sha256')!=r['result_sha']: raise AgentError('result_version_mismatch','Read and acknowledge the exact result hash')
            self.store.execute('UPDATE runs SET ack=1 WHERE id=?',(r['id'],)); self.store.bump(sid)
            return {'run_id':r['id'],'acknowledged':True}
        aid=identifier(p.get('agent_id'),'agent_id')
        async with self.agent_locks[aid]:
            a=self.store.agent(sid,aid)
            if op=='interrupt': return await self.interrupt(a)
            if op=='close':
                w=self.workers.get(aid)
                if w and not w.closed:
                    cleanup=(await self.interrupt(a))['cleanup']
                else: cleanup=await reap_orphan(self,a)
                self.store.agent_update(aid,state='closed' if cleanup=='verified' else 'orphaned',current_run=None,cleanup=cleanup)
                self.store.bump(sid); self.notify()
                return {'agent_id':aid,'state':'closed' if cleanup=='verified' else 'orphaned','cleanup':cleanup,'session_retained':True}
            if op=='respawn':
                w=self.workers.get(aid)
                if w and not w.closed: raise AgentError('worker_alive','Close the current worker before respawn')
                if a['state']=='orphaned' or a['cleanup']=='unknown': raise AgentError('orphaned_worker','Close/reap the orphan before respawn; old pipes cannot be reattached')
                async with self.admission:
                    w=await boot_worker(self,a)
                rid=None
                if p.get('message'):
                    msg=delegated_text(text(p['message']),'Continue this delegated task:\n')
                    rid=self.add_run(a,msg)
                    await self.start_run(w,rid)
                self.store.bump(sid)
                return {'agent_id':aid,'generation':w.generation,'run_id':rid,'state':'running' if rid else 'idle','scope':sid}
            w=self.require_worker(a)
            if op=='answer':
                ui_id=text(p.get('ui_request_id'),'ui_request_id',256)
                item=w.ui.get(ui_id)
                if not item: raise AgentError('input_not_found','No such pending Pi UI request')
                answer=p.get('answer')
                payload={'type':'extension_ui_response','id':ui_id}
                if item.get('method')=='confirm':
                    if not isinstance(answer,bool): raise AgentError('invalid_argument','Confirmation answer must be boolean')
                    payload['confirmed']=answer
                else:
                    if not isinstance(answer,str): raise AgentError('invalid_argument','Answer must be text')
                    if item.get('method')=='select' and answer not in item.get('options',[]):
                        raise AgentError('invalid_argument','Answer is not an offered selection')
                    payload['value']=text(answer,'answer',8192)
                await w.raw(payload); w.ui.pop(ui_id,None)
                if not w.ui:
                    self.store.agent_update(aid,state='running' if w.run_id else 'idle')
                    if w.run_id: self.store.execute("UPDATE runs SET state='running' WHERE id=?",(w.run_id,))
                return {'agent_id':aid,'sent':True,'ui_request_id':ui_id}
            if op=='send':
                msg=delegated_text(text(p.get('message')),'Delegated instruction (not a slash command):\n')
                mode=p.get('mode','steer')
                if mode not in {'send','steer','follow_up'}: raise AgentError('invalid_argument','Invalid message mode')
                if p.get('interrupt',False):
                    await self.interrupt(a)
                    a=self.store.agent(sid,aid)
                    if a['cleanup']!='verified':
                        raise AgentError('cleanup_unconfirmed','Previous worker cleanup could not be verified; inspect before respawn')
                    w=await boot_worker(self,a); mode='send'
                if mode=='steer':
                    if not w.run_id: raise AgentError('agent_idle','Agent is idle; use mode=send. Steering never implicitly respawns.')
                    if len(self.store.all("SELECT id FROM receipts WHERE agent_id=? AND state IN ('sending','queued')",(aid,)))>=20:
                        raise AgentError('queue_full','Too many unconsumed steering messages')
                    receipt=new_id('msg_')
                    self.store.execute('INSERT INTO receipts VALUES(?,?,?,?,?,?,?,?)',(receipt,aid,w.run_id,sid,msg,'sending',now(),now()))
                    try: await w.rpc('steer',message=msg)
                    except AgentError as e:
                        self.store.execute("UPDATE receipts SET state=?,updated=? WHERE id=?",('not_consumed' if e.code=='pi_rejected' else 'unknown',now(),receipt)); raise
                    self.store.execute("UPDATE receipts SET state='queued',updated=? WHERE id=? AND state='sending'",(now(),receipt))
                    return {'agent_id':aid,'run_id':w.run_id,'receipt_id':receipt,'delivery':self.store.one('SELECT state FROM receipts WHERE id=?',(receipt,))['state']}
                if mode=='send' and w.run_id: raise AgentError('agent_busy','Use steer, follow_up, or interrupt=true for an active agent')
                if len(self.store.all("SELECT id FROM runs WHERE agent_id=? AND state='queued'",(aid,)))>=20: raise AgentError('queue_full','Follow-up queue is full')
                rid=self.add_run(a,msg)
                if not w.run_id: await self.start_run(w,rid)
                self.notify()
                return {'agent_id':aid,'run_id':rid,'state':self.store.run(sid,rid)['state'],'queue_owner':'daemon'}
        raise AgentError('unknown_operation',f'Unknown mutation: {op}')

    async def deadline_loop(self):
        while not self.closing:
            await asyncio.sleep(.5)
            for r in self.store.all("SELECT * FROM runs WHERE state IN ('running','needs_input') AND deadline<?",(now(),)):
                async with self.agent_locks[r['agent_id']]:
                    current=self.store.run(r['scope'],r['id'])
                    if current['state'] not in {'running','needs_input'}: continue
                    try: await self.interrupt(self.store.agent(r['scope'],r['agent_id']),terminal='timed_out')
                    except AgentError as exc: print('deadline: '+str(exc),file=sys.stderr)

    async def shutdown(self):
        self.closing=True
        for a in self.store.all('SELECT * FROM agents'):
            w=self.workers.get(a['id'])
            if w and not w.closed:
                async with self.agent_locks[a['id']]:
                    try: await self.interrupt(a)
                    except Exception:
                        with contextlib.suppress(Exception): await terminate(self,w)
                    self.store.agent_update(a['id'],state='dormant',current_run=None)
        tasks=[t for w in self.workers.values() for t in w.tasks]+list(self.background)
        for t in tasks:
            if not t.done(): t.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        self.store.close()
