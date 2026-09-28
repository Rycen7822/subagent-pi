"""Daemon-side runtime: the scope/agent/run state machine behind every IPC and MCP
operation. Process mechanics live in worker.py, scope binding in binding.py and the
read projections in views.py; this module owns ledger state transitions."""
from __future__ import annotations
import asyncio
from weakref import WeakValueDictionary
import contextlib
import json
import os
from pathlib import Path
import shutil
import sys
import time

from . import __version__, PROTOCOL_VERSION, views, worker
from .binding import ScopeBindings
from .common import (TERMINAL, AgentError, RESIDENT_AGENT_STATES, bounded, crop,
    dumps, group_members, identifier, integer, new_id, now, process_identity, text)
from .config import load_config, launch_spec
from . import parent
from .store import Store
from .worker import (RESULT_CAP, Worker, message_text, ownership,
    reap_orphan, terminate, write_bootstrap)

def delegated_text(value: str, envelope: str) -> str:
    """A delegated task is data, never a Pi extension command: text that would be
    parsed as a slash command is wrapped before it enters the child, at every
    entry point that sends text into a session."""
    return envelope + value if value.lstrip().startswith('/') else value

class LockPool(WeakValueDictionary):
    """Holders and waiters keep a lock alive; idle keys need no daemon cache."""
    def __getitem__(self, key):
        lock = self.get(key)
        if lock is None:
            self[key] = lock = asyncio.Lock()
        return lock

class Runtime:
    def __init__(self, home):
        self.home = home
        self.store = Store(home)
        self.config = load_config(home)
        self.workers = {}
        self.bindings = ScopeBindings(self.store, self.config)
        self.agent_locks = LockPool()
        self.request_locks = LockPool()
        self.admission = asyncio.Lock()
        self.changed = asyncio.Condition()
        self.views = views.ReadViews(self.store, self.workers.get, self.changed, self.config['max_wait_seconds'])
        self.shutdown_requested = asyncio.Event()
        self.closing = False
        self.background = set()
        self.parent_notifications = parent.ParentNotifications(self.store,self.spawn_task,self.workers.get)
        self._reconcile()

    def spawn_task(self,coro):
        t = asyncio.create_task(coro)
        self.background.add(t)
        def done(t):
            self.background.discard(t)
            if not t.cancelled() and t.exception(): print('runtime task: '+str(t.exception()),file=sys.stderr)
        t.add_done_callback(done)
        return t

    def make_worker(self, agent, proc):
        return Worker(agent,proc,self.home/'agents'/agent['id']/'stderr.log',
                      self.config['rpc_timeout_seconds'],self.config['default_idle_timeout_seconds'],
                      self.spawn_task,self.on_event,self.fail_worker,self.worker_exited,self.retire_worker)

    def retire_worker(self, w):
        if self.workers.get(w.agent['id']) is w:
            del self.workers[w.agent['id']]

    async def record_bootstrap_write(self, fd, aid, generation, body):
        status=await write_bootstrap(fd,body)
        if status=='broken': self.store.event(aid,None,generation,'bootstrap_write_failed',{'error':'BrokenPipeError'})
        elif status=='timeout': self.store.event(aid,None,generation,'bootstrap_write_timeout',{})

    async def terminate_worker(self, w):
        cleanup=await terminate(self.home/'agents'/w.agent['id'],w)
        self.store.agent_update(w.agent['id'],cleanup=cleanup)
        return cleanup

    async def _boot_worker(self, a):
        """Own the generation and ledger transitions; worker owns process pipes."""
        aid=a['id']
        if sum(not w.closed for w in self.workers.values()) >= self.config['max_resident_agents']:
            raise AgentError('capacity_exceeded','Resident Pi limit reached; close an idle agent first')
        spec=json.loads(a['launch'])
        if spec.get('access')=='write': self.assert_writer_exclusive(aid,a['cwd'])
        directory,session,argv=worker.session_argv(self.home,a,spec)
        generation=a['generation']+1
        first_launch=not a['generation']
        plan=self.bindings.inheritance_plan(a,spec,generation)
        worker.write_launch(directory,spec,[*argv,*plan['argv']],generation)
        if plan['diagnostics']:
            self.store.event(aid,None,generation,'inheritance_diagnostics',
                bounded({'source':plan.get('source'),'servers':plan['servers'],'diagnostics':plan['diagnostics']},4096))
        body=worker.bootstrap_body(plan['payload'])
        guard_env=self.bindings.child_env(a['scope'],spec)
        guard_env['PI_AGENTS_SCOPE_CWD']=self.store.scope(a['scope'])['cwd']
        if spec.get('surface'):
            guard_env['PI_AGENTS_CHILD_BUILTINS']=','.join(spec.get('builtins',[]))
        proc=w=write_fd=receipt_fd=None
        try:
            proc,write_fd,receipt_fd=await worker.start_guard(directory,spec,guard_env,body,
                lambda:self.store.agent_update(aid,state='starting',generation=generation,cleanup='pending'))
            self.store.agent_update(aid,pid=proc.pid,identity=process_identity(proc.pid))
            w=self.make_worker(self.store.agent(a['scope'],aid),proc)
            self.workers[aid]=w
            w.start()
            if write_fd is not None:
                self.spawn_task(self.record_bootstrap_write(write_fd,aid,generation,body))
                write_fd=None  # writer thread owns and closes this end
            state=await w.rpc('get_state',timeout=self.config['startup_timeout_seconds'])
            if state.get('configurationError'):
                raise AgentError('unsupported_thinking',state['configurationError'])
            actual=state.get('sessionFile')
            if not actual or not Path(actual).is_absolute():
                raise AgentError('session_mismatch','Pi did not report an absolute persistent session path')
            actual_path=Path(actual).resolve()
            if first_launch:
                if not actual_path.is_relative_to((directory/'sessions').resolve()):
                    raise AgentError('session_mismatch','Pi session escaped its managed session directory')
                self.store.agent_update(aid,session_file=str(actual_path))
            elif actual_path!=session.resolve():
                raise AgentError('session_mismatch','Pi did not select the managed session path')
            if state.get('isStreaming'):
                raise AgentError('unexpected_activity','Pi started a model turn without an explicit task')
            if state.get('subagentProtocol') != 1:
                raise AgentError('unsupported_transport',
                    'The child must use subagent-pi SDK transport; stock Pi RPC and host patches are not supported')
            try: await asyncio.wait_for(w.context_ready.wait(),worker.SURFACE_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                raise AgentError('context_unavailable',
                    'Managed Pi did not load its context extension; inspect the agent stderr log') from None
            if spec.get('surface'):
                report=await worker.surface_report(w)
                self.store.event(aid,None,generation,'tool_surface',bounded(report if report is not None else {'ok':False,'reason':'no-report'},2048))
                worker.check_surface(report)
            if receipt_fd is not None:
                fd,receipt_fd=receipt_fd,None  # read_receipt owns and closes the fd
                receipt=await worker.read_receipt(fd,aid,generation,min(self.config['startup_timeout_seconds'],20))
                self.store.event(aid,None,generation,'bridge_receipt',bounded(receipt,4096))
                worker.check_receipt(receipt)
            if plan['skills']:
                self.store.event(aid,None,generation,'inheritance_skills',await worker.resolve_skills(w,plan))
            if isinstance(state.get('thinking'),str):
                if '--thinking' not in spec['argv']: spec['argv'] += ['--thinking',state['thinking']]
                spec['thinking']=state['thinking']
                spec['available_thinking']=state.get('availableThinking',[])
            resolved_model=state.get('model')
            if isinstance(resolved_model,dict) and resolved_model.get('id'):
                if '--model' not in spec['argv']: spec['argv'] += ['--model',resolved_model['id']]
                if '--provider' not in spec['argv'] and resolved_model.get('provider'):
                    spec['argv'] += ['--provider',resolved_model['provider']]
                spec['resolved_model']={'id':resolved_model['id'],'provider':resolved_model.get('provider')}
                self.store.agent_update(aid,launch=dumps(spec))
            self.store.agent_update(aid,state='idle',cleanup='not_checked')
            self.event(w,'worker_ready',{'pi_session_id':state.get('sessionId'),'model':bounded(state.get('model'),1000)})
            return w
        except BaseException:
            if w:
                w.stopping=True
                await self.terminate_worker(w)
            elif proc:
                cleanup,_=await worker.stop_owned_process(directory,self.store.agent(a['scope'],aid),proc)
                self.store.agent_update(aid,cleanup=cleanup)
            raise
        finally:
            worker.close_quietly(write_fd)
            worker.close_quietly(receipt_fd)

    def _reconcile(self):
        """A new daemon cannot recover old pipes; never claim a live orphan is
        reattached. Every resident row is re-judged from its owner record."""
        self.store.execute("UPDATE parent_notifications SET state='unknown',error='Daemon restarted during delivery; not retried' WHERE state='sending'")
        # Keep exact-ID recall intent across restart. A wait reservation does
        # not imply delivered output; handled=0 must still permit a wakeup.
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
        self.parent_notifications.schedule()

    def event(self,w,kind,payload):
        self.store.event(w.agent['id'],w.run_id,w.generation,kind,bounded(payload,4096))
        w.events_written += 1
        # Short generations still inherit this agent's existing event history.
        if w.events_written == 1 or w.events_written % 256 == 0:
            cap = self.config['event_max_count_per_agent']
            self.store.execute('DELETE FROM events WHERE agent_id=? AND seq < COALESCE((SELECT seq FROM events WHERE agent_id=? ORDER BY seq DESC LIMIT 1 OFFSET ?),0)',(w.agent['id'],w.agent['id'],cap-1))

    def on_event(self,w,e):
        kind = e.get('type','unknown')
        if kind in {'protocol_warning','protocol_error'}:
            self.event(w,kind,{'message':e['message']})
            return
        a = self.store.one('SELECT * FROM agents WHERE id=?',(w.agent['id'],))
        if not a or a['generation'] != w.generation: return
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
            update=e.get('assistantMessageEvent',{})
            if isinstance(update,dict) and update.get('type') in {'text_delta','thinking_delta','toolcall_delta'} and update.get('delta'):
                w.last_progress=time.monotonic()
            return  # keep text deltas out of SQLite; message_end is canonical
        if kind in {'tool_execution_start','tool_execution_update','tool_execution_end'}:
            tool_id=e.get('toolCallId') or e.get('toolName')
            if kind == 'tool_execution_start': w.active_tools[tool_id]=e.get('toolName')
            if kind == 'tool_execution_end':
                w.active_tools.pop(tool_id,None)
                w.last_progress=time.monotonic()
            if kind != 'tool_execution_update': self.event(w,kind,e)
            return
        if kind == 'message_end':
            m=e.get('message',{})
            if not isinstance(m,dict): return
            role=m.get('role')
            mt=message_text(m)
            if role=='assistant':
                w.last_progress=time.monotonic()
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
                item=bounded(e,3000)
                if e['method']=='select':
                    options=e.get('options')
                    if (isinstance(options,list) and len(options)<=256 and
                        all(isinstance(option,str) and option and '\x00' not in option and
                            len(option.encode('utf-8'))<=8192 for option in options) and
                        sum(len(option.encode('utf-8')) for option in options)<=65536):
                        # The public event may be shortened, but answer validation
                        # must use the exact labels Pi offered to the user.
                        item['options']=options.copy()
                    else:
                        item['_options_unavailable']=True
                w.ui[str(e['id'])]=item
                self.store.agent_update(a['id'],state='needs_input')
                if w.run_id: self.store.execute("UPDATE runs SET state='needs_input' WHERE id=?",(w.run_id,))
                self.event(w,'needs_input',e)
                if w.run_id: self.store.attention(w.run_id,'question',str(e['id']))
                self.store.bump(a['scope']); self.notify()
            return
        if kind=='extension_ui_closed':
            self.dismiss_ui(w,str(e.get('id','')))
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
            # A host-scheduled retry delay is not a stalled provider request.
            delay=e.get('delayMs',0) if kind=='auto_retry_start' else 0
            w.last_progress=time.monotonic()+max(0,float(delay))/1000
            self.event(w,kind,e)

    def dismiss_ui(self,w,ui_id):
        if w.ui.pop(ui_id,None) is None: return
        w.last_progress=time.monotonic()
        if w.run_id and not w.ui:
            self.store.agent_update(w.agent['id'],state='running')
            self.store.execute("UPDATE runs SET state='running' WHERE id=? AND state='needs_input'",(w.run_id,))
        self.notify()

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

    async def deliver_input(self,w,kind,message,**params):
        receipt=new_id('msg_')
        self.store.execute('INSERT INTO receipts VALUES(?,?,?,?,?,?,?,?)',
            (receipt,w.agent['id'],w.run_id,w.agent['scope'],message,'sending',now(),now()))
        try:
            await w.rpc(kind,message=message,**params)
        except AgentError as exc:
            if kind=='steer':
                self.store.execute("UPDATE receipts SET state=?,updated=? WHERE id=?",
                    ('not_consumed' if exc.code=='pi_rejected' else 'unknown',now(),receipt))
            raise
        # Consumption may precede the RPC reply; acknowledgement must not undo it.
        self.store.execute("UPDATE receipts SET state='queued',updated=? WHERE id=? AND state='sending'",(now(),receipt))
        return receipt

    async def start_run(self,w,rid):
        r=self.store.run(w.agent['scope'],rid)
        if r['state'] not in {'queued','starting'}: return
        w.run_id=rid; w.last_text=''; w.error=None; w.usage={}; w.stopping=False; w.ui.clear(); w.active_tools.clear()
        w.last_progress=time.monotonic()
        w.idle_timeout_seconds=r['idle_timeout_seconds'] or self.config['default_idle_timeout_seconds']
        self.store.execute("UPDATE runs SET state='running',started=? WHERE id=?",(now(),rid))
        self.store.agent_update(w.agent['id'],state='running',current_run=rid)
        self.store.bump(w.agent['scope'])
        self.event(w,'run_started',{'idle_timeout_seconds':w.idle_timeout_seconds})
        try:
            return await self.deliver_input(w,'prompt',r['task'],runId=rid)
        except AgentError as e:
            if e.code=='pi_rejected':
                self.store.finish(rid,'failed','',e.message)
                w.run_id=None
                self.store.agent_update(w.agent['id'],state='idle',current_run=None)
                self.notify()
            # Timeouts are uncertain: retain the run and wait for events rather than re-executing.
            raise

    def add_run(self,a,task,state='queued',timeout=None):
        rid=new_id('run_')
        self.store.execute('INSERT INTO runs(id,agent_id,scope,state,task,created,idle_timeout_seconds) VALUES(?,?,?,?,?,?,?)',
            (rid,a['id'],a['scope'],state,task,now(),timeout or self.config['default_idle_timeout_seconds']))
        self.store.bump(a['scope'])
        return rid

    async def settle(self,w,rid):
        async with self.agent_locks[w.agent['id']]:
            a=self.store.one('SELECT generation FROM agents WHERE id=?',(w.agent['id'],))
            if (self.workers.get(w.agent['id']) is not w or not a or a['generation']!=w.generation
                    or w.run_id!=rid or w.stopping or w.closed): return
            # Only the SDK transport's identity-bound task completion can enter
            # here. Per-run Pi events and transient idle readings never settle it.
            state='failed' if w.error else 'completed'
            self.store.finish(rid,state,w.last_text,w.error,w.usage)
            self.event(w,'run_terminal',{'state':state})
            w.run_id=None; w.ui.clear(); w.active_tools.clear()
            self.store.agent_update(w.agent['id'],state='idle',current_run=None,cleanup='not_checked')
            self.notify()
            q=self.store.one("SELECT id FROM runs WHERE agent_id=? AND state='queued' ORDER BY created LIMIT 1",(w.agent['id'],))
            if q and not self.closing and w.proc.returncode is None:
                try: await self.start_run(w,q['id'])
                except AgentError as exc: self.event(w,'start_failed',exc.as_dict())

    def cancel_queued(self,aid,reason):
        for q in self.store.all("SELECT id FROM runs WHERE agent_id=? AND state='queued'",(aid,)):
            self.store.finish(q['id'],'cancelled','',reason)

    async def stop_unowned(self,w):
        """Terminate a worker whose host started a run no daemon run owns.

        The interrupted work is not replayed and its session is kept, so the agent
        can be respawned deliberately; what is not allowed is a live process that
        silently continues work the caller was told had stopped.
        """
        async with self.agent_locks[w.agent['id']]:
            a=self.store.one('SELECT * FROM agents WHERE id=?',(w.agent['id'],))
            if not a or a['generation']!=w.generation or w.closed: return
            self.cancel_queued(a['id'],'Worker stopped after unowned activity; queued task was not retried')
            cleanup=await self.terminate_worker(w)
            self.store.agent_update(a['id'],state='dormant',current_run=None,cleanup=cleanup)
            self.event(w,'worker_stopped',{'reason':'unowned_run','cleanup':cleanup})
            self.store.bump(a['scope']); self.notify()

    async def fail_worker(self,w,error):
        async with self.agent_locks[w.agent['id']]:
            a=self.store.one('SELECT * FROM agents WHERE id=?',(w.agent['id'],))
            # Reader errors can wait behind an interrupt/respawn. Even terminate
            # writes cleanup to the ledger, so validate ownership before it too.
            if (self.workers.get(w.agent['id']) is not w or not a or a['generation']!=w.generation
                    or w.stopping or w.closed): return
            w.stopping=True
            self.cancel_queued(w.agent['id'],'Worker protocol failed; queued task was not retried')
            if w.run_id:
                self.store.finish(w.run_id,'failed',w.last_text,error,w.usage); w.run_id=None
            await self.terminate_worker(w)
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
            self.cancel_queued(a['id'],'Worker exited; queued task was not automatically retried')
            if a['state'] not in {'closed','dormant'}:
                self.store.agent_update(a['id'],state='crashed',current_run=None,
                    cleanup='unknown' if group_members(w.proc.pid) else 'verified')
            self.store.bump(a['scope']); self.notify()

    async def interrupt(self,a,terminal='interrupted',reason='Explicit interruption; filesystem effects may be partial'):
        w=self.require_worker(a)
        w.stopping=True
        self.store.agent_update(a['id'],state='stopping')
        self.cancel_queued(a['id'],'Cancelled because the active agent was stopped')
        # Terminating only this verified process group also cancels input hooks,
        # authentication preflights and extension timers that Pi cannot abort.
        cleanup=await self.terminate_worker(w)
        if w.run_id:
            self.store.finish(w.run_id,terminal,w.last_text,reason,w.usage)
            self.event(w,'run_terminal',{'state':terminal})
        w.run_id=None; w.ui.clear(); w.active_tools.clear()
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
            parent.bind(self.store,sid,source)
            self.bindings.bind_scope_source(sid,p,source)
            return {'scope':sid,'cwd':cwd, 'outstanding':self.views.outstanding(sid),'parent_notifications':parent.status(self.store,sid)}
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
            if p.get('inheritance'): report['inheritance']=self.bindings.doctor()
            return report
        sid=identifier(p.get('scope'),'scope'); scope=self.store.scope(sid)
        if op=='spawn' and 'cwd' not in p: p={**p,'cwd':scope['cwd']}
        if op in {'spawn','send','interrupt','close','respawn','ack','answer'}:
            parent.bind(self.store,sid,source,allow_new=False)
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
            return {'scope':sid,'agents':[views.brief_agent(a,self.workers.get(a['id'])) for a in rows],'total':total,'omitted':max(0,total-len(rows)), 'outstanding':self.views.outstanding(sid,limit),'parent_notifications':parent.status(self.store,sid)}
        if op=='inspect': return self.views.inspect(p)
        if op=='result': return self.views.result(p)
        if op=='wait': return await self.views.wait(p)
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
                if not Path(cwd).is_dir():
                    raise AgentError('invalid_cwd','Spawn cwd must be an existing directory')
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
                rid=self.add_run(a,task,'starting',integer(p.get('idle_timeout_seconds',self.config['default_idle_timeout_seconds']),'idle_timeout_seconds',1,604800))
                async with self.agent_locks[aid]:
                    try:
                        w=await self._boot_worker(a)
                        await self.start_run(w,rid)
                    except AgentError as e:
                        if self.store.run(sid,rid)['state']=='starting':
                            self.store.finish(rid,'failed','',e.message)
                            self.store.agent_update(aid,state='crashed'); self.notify()
                        raise AgentError(e.code,e.message,agent_id=aid,run_id=rid)
                return {'agent_id':aid,'name':name,'run_id':rid,'scope':sid,'state':self.store.run(sid,rid)['state'],'cwd':cwd,
                        **views.model_settings(self.store.agent(sid,aid))}
        if op=='ack':
            r=self.store.run(sid,identifier(p.get('run_id'),'run_id'))
            if r['state'] not in TERMINAL: raise AgentError('not_terminal','Cannot acknowledge an active run')
            if p.get('result_sha256')!=r['result_sha']: raise AgentError('result_version_mismatch','Read and acknowledge the exact result hash')
            self.store.execute('UPDATE runs SET ack=1 WHERE id=?',(r['id'],)); self.store.bump(sid)
            recalled=await self.parent_notifications.acknowledge(sid,r['id'])
            return {'run_id':r['id'],'acknowledged':True,**({'notification_recall':recalled} if recalled!='complete' else {})}
        aid=identifier(p.get('agent_id'),'agent_id')
        async with self.agent_locks[aid]:
            a=self.store.agent(sid,aid)
            if op=='interrupt': return await self.interrupt(a)
            if op=='close':
                w=self.workers.get(aid)
                if w and not w.closed:
                    cleanup=(await self.interrupt(a))['cleanup']
                else: cleanup=await reap_orphan(self.home/'agents'/aid,a)
                self.store.agent_update(aid,state='closed' if cleanup=='verified' else 'orphaned',current_run=None,cleanup=cleanup)
                self.store.bump(sid); self.notify()
                return {'agent_id':aid,'state':'closed' if cleanup=='verified' else 'orphaned','cleanup':cleanup,'session_retained':True}
            if op=='respawn':
                w=self.workers.get(aid)
                if w and not w.closed: raise AgentError('worker_alive','Close the current worker before respawn')
                if a['state']=='orphaned' or a['cleanup']=='unknown': raise AgentError('orphaned_worker','Close/reap the orphan before respawn; old pipes cannot be reattached')
                async with self.admission:
                    w=await self._boot_worker(a)
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
                    if item.get('_options_unavailable'):
                        raise AgentError('input_too_large','Pi offered too many or oversized selections to answer safely')
                    if item.get('method')=='select' and answer not in item.get('options',[]):
                        raise AgentError('invalid_argument','Answer is not an offered selection')
                    payload['value']=text(answer,'answer',8192)
                await w.raw(payload)
                self.dismiss_ui(w,ui_id)
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
                    async with self.admission:
                        w=await self._boot_worker(a)
                    mode='send'
                if mode=='steer':
                    if not w.run_id: raise AgentError('agent_idle','Agent is idle; use mode=send. Steering never implicitly respawns.')
                    if len(self.store.all("SELECT id FROM receipts WHERE agent_id=? AND state IN ('sending','queued')",(aid,)))>=20:
                        raise AgentError('queue_full','Too many unconsumed steering messages')
                    receipt=await self.deliver_input(w,'steer',msg)
                    return {'agent_id':aid,'name':a['name'],'run_id':w.run_id,'receipt_id':receipt,
                            'execution':'after_current_sdk_call',
                            'delivery':self.store.one('SELECT state FROM receipts WHERE id=?',(receipt,))['state']}
                if mode=='send' and w.run_id: raise AgentError('agent_busy','Use steer, follow_up, or interrupt=true for an active agent')
                if len(self.store.all("SELECT id FROM runs WHERE agent_id=? AND state='queued'",(aid,)))>=20: raise AgentError('queue_full','Follow-up queue is full')
                rid=self.add_run(a,msg)
                if not w.run_id: await self.start_run(w,rid)
                self.notify()
                return {'agent_id':aid,'name':a['name'],'run_id':rid,'state':self.store.run(sid,rid)['state'],'queue_owner':'daemon'}
        raise AgentError('unknown_operation',f'Unknown mutation: {op}')

    async def idle_loop(self):
        while not self.closing:
            await asyncio.sleep(.5)
            await self.check_idle()

    async def check_idle(self):
        for w in list(self.workers.values()):
            rid=w.run_id
            idle=w.idle_seconds()
            if idle is None or idle<w.idle_timeout_seconds: continue
            async with self.agent_locks[w.agent['id']]:
                # Recheck after waiting: progress, a tool, completion or a new
                # worker/run must never be stopped by an obsolete observation.
                if self.workers.get(w.agent['id']) is not w or w.run_id!=rid or w.stopping or w.closed: continue
                idle=w.idle_seconds()
                if idle is None or idle<w.idle_timeout_seconds: continue
                try:
                    await self.interrupt(self.store.agent(w.agent['scope'],w.agent['id']),terminal='timed_out',
                        reason=f'No model output or thinking progress for {w.idle_timeout_seconds}s outside tool execution/parent input; filesystem effects may be partial')
                except AgentError as exc: print('idle timeout: '+str(exc),file=sys.stderr)

    async def shutdown(self):
        self.closing=True
        self.parent_notifications.closing=True
        for w in list(self.workers.values()):
            if not w.closed:
                a=self.store.agent(w.agent['scope'],w.agent['id'])
                async with self.agent_locks[a['id']]:
                    try: await self.interrupt(a)
                    except Exception:
                        with contextlib.suppress(Exception): await self.terminate_worker(w)
                    self.store.agent_update(a['id'],state='dormant',current_run=None)
        tasks=list(self.background)
        for t in tasks:
            if not t.done(): t.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        self.store.close()
