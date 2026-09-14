from __future__ import annotations
import asyncio
from collections import defaultdict
import contextlib
import json
import os
from pathlib import Path
import signal
import shutil
import sys
import time
from . import __version__, PROTOCOL_VERSION
from .common import (TERMINAL, MAX_FRAME, AgentError, atomic_json, bounded,
    crop, dumps, group_members, identifier, integer, live_identity, new_id, now,
    private_dir, process_identity, read_frame, text)
from .config import load_config, launch_spec
from .inheritance import (CODEX_MCP_BASELINE, Diagnostic, collect_skills, parse_mcp_servers, policy_filter,
    read_codex_config, referenced_env_names, resolve_codex_home, resolve_environment)
from .store import Store

BASE_KEYS = ('PATH', 'HOME', 'LANG', 'LC_ALL', 'TERM', 'TMPDIR', 'SHELL', 'USER', 'LOGNAME')

RESULT_CAP = 1024 * 1024
BOOTSTRAP_MAX = 4 * 1024 * 1024

def message_text(message):
    content = message.get('content',[])
    if isinstance(content,str): return content
    if not isinstance(content,list): return ''
    return '\n'.join(str(p.get('text','')) for p in content if isinstance(p,dict) and p.get('type')=='text')

class Worker:
    def __init__(self, runtime, agent, proc):
        self.rt, self.agent, self.proc = runtime, agent, proc
        self.generation = agent['generation']
        self.pending = {}
        self.run_id = None
        self.last_text = ''
        self.error = None
        self.usage = {}
        self.stopping = False
        self.closed = False
        self.ui = {}
        self.current_tool = None
        self.last_activity = now()
        self.events_written = 0
        self.tasks = []
        self.lock_fd = None
        self.write_lock = asyncio.Lock()
    def start(self):
        self.tasks = [asyncio.create_task(self.read_stdout()),asyncio.create_task(self.read_stderr()),asyncio.create_task(self.watch_exit())]
    async def rpc(self, kind, timeout=None, **params):
        if self.closed or self.proc.returncode is not None:
            raise AgentError('worker_unavailable','Pi process is not connected; inspect then respawn')
        rid = new_id('rpc_')
        future = asyncio.get_running_loop().create_future()
        self.pending[rid] = future
        try:
            async with self.write_lock:
                self.proc.stdin.write((dumps({'id':rid,'type':kind,**params})+'\n').encode())
                await self.proc.stdin.drain()
            response = await asyncio.wait_for(asyncio.shield(future),timeout or self.rt.config['rpc_timeout_seconds'])
            if not response.get('success'):
                raise AgentError('pi_rejected',crop(str(response.get('error','Pi rejected the command')),2000),command=kind)
            return response.get('data') or {}
        except asyncio.TimeoutError:
            raise AgentError('rpc_timeout','Pi command outcome is uncertain; do not blindly retry',command=kind)
        except (BrokenPipeError, ConnectionResetError):
            raise AgentError('worker_unavailable','Pi RPC channel closed')
        finally:
            self.pending.pop(rid,None)
            if not future.done(): future.cancel()
    async def raw(self, value):
        async with self.write_lock:
            self.proc.stdin.write((dumps(value)+'\n').encode()); await self.proc.stdin.drain()
    async def read_stdout(self):
        try:
            while True:
                try: e = await read_frame(self.proc.stdout)
                except json.JSONDecodeError:
                    self.rt.event(self,'protocol_warning',{'message':'Non-JSON stdout line omitted'})
                    continue
                if e is None: break
                if not isinstance(e,dict): continue
                if e.get('type') == 'response':
                    f = self.pending.get(e.get('id'))
                    if f and not f.done(): f.set_result(e)
                else:
                    self.rt.on_event(self,e)
        except asyncio.CancelledError: raise
        except Exception as exc:
            self.error = f'RPC reader failed: {type(exc).__name__}: {exc}'
            with contextlib.suppress(Exception): self.rt.event(self,'protocol_error',{'message':crop(self.error,1000)})
            asyncio.create_task(self.rt.fail_worker(self,self.error))
    async def read_stderr(self):
        path = self.rt.home/'agents'/self.agent['id']/'stderr.log'
        with path.open('ab') as f:
            total = f.tell()
            while True:
                chunk = await self.proc.stderr.read(4096)
                if not chunk: break
                if total < 512*1024:
                    data = chunk[:512*1024-total]; f.write(data); f.flush(); total += len(data)
    async def watch_exit(self):
        code = await self.proc.wait()
        with contextlib.suppress(Exception):  # consume a final agent_end before reconciliation
            await asyncio.wait_for(asyncio.shield(self.tasks[0]),2)
        self.closed = True
        for future in list(self.pending.values()):
            if not future.done(): future.set_exception(AgentError('worker_exited',f'Pi guard exited ({code})'))
        await self.rt.worker_exited(self,code)

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
        self._restore()

    def _resolve_source(self, source_env):
        return resolve_codex_home(self.config['inheritance'], source_env)

    def _inheritance_plan(self, a, spec, generation):
        """Recompute managed-child inheritance from the original sources at boot.
        The persisted launch argv is never touched; secrets resolve into the pipe
        payload only, diagnostics carry names, never values."""
        inh = self.config['inheritance']
        empty = {'argv': [], 'payload': None, 'diagnostics': [], 'bridge': False, 'servers': []}
        if not inh.get('enabled'):
            return {**empty, 'reason': 'inheritance disabled by config'}
        scope = self.store.scope(a['scope'])
        if not scope['inheritance']:
            return {**empty, 'reason': 'inheritance disabled for this scope'}
        source_env = self.scope_env.get(a['scope'])
        if scope['codex_home']:
            codex_home, mode = Path(scope['codex_home']), scope['codex_source'] or 'scope_env'
        elif source_env and isinstance(source_env.get('CODEX_HOME'), str) and source_env['CODEX_HOME'].strip():
            codex_home, mode = self._resolve_source(source_env)
        elif scope['codex_source']:
            # A restart wiped the snapshot: never fall back to another Codex home
            # (e.g. the daemon user's ~/.codex); demand an explicit rebind.
            raise AgentError('inheritance_source_unbound',
                             'Scope lost its codex source binding after a daemon restart; the owning client must re-open the scope')
        else:
            codex_home, mode = self._resolve_source(source_env)
        if codex_home is None:
            diag = Diagnostic('source', 'codex_home', 'no codex source directory found').as_dict()
            return {**empty, 'diagnostics': [diag], 'reason': 'no source'}
        raw = read_codex_config(codex_home)
        existing_skills = [spec['argv'][i + 1] for i, flag in enumerate(spec['argv']) if flag == '--skill']
        skill_paths, skill_diag = [], []
        if inh.get('skills', True):
            skill_paths, skill_diag = collect_skills(codex_home, raw, a['cwd'], existing_skills)
        servers, mcp_diag = [], []
        if inh.get('mcp', True):
            servers, mcp_diag = parse_mcp_servers(codex_home, raw, inh.get('mcp_protocol_mode', 'auto'))
            servers, env_diag = resolve_environment(servers, source_env or {})
            mcp_diag += env_diag
            servers, access_diag = policy_filter(servers, spec['access'])
            mcp_diag += access_diag
        required_broken = [s['name'] for s in servers if s.get('required') and s.get('disposition') != 'ok']
        if required_broken:
            raise AgentError('inheritance_required_server_failed',
                             'required MCP server(s) cannot start: ' + ', '.join(sorted(required_broken)))
        usable = [s for s in servers if s.get('disposition') == 'ok']
        diagnostics = [d.as_dict() for d in skill_diag + mcp_diag]
        argv = []
        for path in (p for p in skill_paths if p not in existing_skills):
            argv += ['--skill', path]
        bridge_path = Path(__file__).resolve().parent.parent / 'extensions' / 'codex-mcp-bridge.ts'
        load_bridge = bool(inh.get('mcp', True) and usable and bridge_path.exists())
        source = {'codex_home': str(codex_home), 'mode': mode}
        payload = None
        if load_bridge:
            argv += ['--extension', str(bridge_path)]
            internal = ('env_var_names', 'env_header_names', 'static_env', 'static_headers',
                        'disposition', 'reasons')
            payload = {'v': 1,
                       'agent': {'id': a['id'], 'access': spec['access'], 'generation': generation},
                       'source': source,
                       'mcp': {'servers': [{k: v for k, v in s.items() if k not in internal} for s in usable]}}
        return {'argv': argv, 'payload': payload, 'diagnostics': diagnostics,
                'bridge': load_bridge, 'servers': [s['name'] for s in usable], 'source': source}

    @staticmethod
    def _merge_bridge_tool(argv, tool='codex_mcp'):
        """Merge the bridge tool into Pi's single tool allowlist over the FULL argv.
        Pi's CLI parser assigns on every --tools occurrence (last wins), so appending
        a second flag would wipe the builtin reader/writer allowlist. With no tool
        flag at all, argv is left untouched: Pi then allows extension tools by
        default, and a bare --tools would strip builtins.
        """
        i = next((k for k, x in enumerate(argv) if x in ('--tools', '-t')), None)
        if i is not None and i + 1 < len(argv):
            names = [n for n in argv[i + 1].split(',') if n]
            if tool not in names:
                names.append(tool)
            return [*argv[:i], '--tools', ','.join(names), *argv[i + 2:]]
        if '--no-tools' in argv:
            return [x for x in argv if x != '--no-tools'] + ['--tools', tool]
        return argv

    @staticmethod
    def _write_all(fd, data):
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]

    async def _write_bootstrap(self, fd, agent_id, generation, data):
        """Write the payload from a worker thread. The thread owns the fd and closes
        it when the write finishes or breaks; a timeout abandons the wait, never the
        write mid-close (closing an fd another thread still uses risks writing into
        a reused descriptor)."""
        def _write_and_close():
            try:
                self._write_all(fd, data)
                return 'ok'
            except OSError:
                return 'broken'
            finally:
                with contextlib.suppress(OSError):
                    os.close(fd)
        try:
            status = await asyncio.wait_for(asyncio.get_running_loop().run_in_executor(None, _write_and_close), 30)
            if status == 'broken':  # store writes stay on the event-loop thread (sqlite is single-thread bound)
                self.store.event(agent_id, None, generation, 'bootstrap_write_failed', {'error': 'BrokenPipeError'})
        except asyncio.TimeoutError:
            self.store.event(agent_id, None, generation, 'bootstrap_write_timeout', {})

    def spawn_task(self,coro):
        t = asyncio.create_task(coro)
        self.background.add(t)
        def done(t):
            self.background.discard(t)
            if not t.cancelled() and t.exception(): print('runtime task: '+str(t.exception()),file=sys.stderr)
        t.add_done_callback(done)
        return t
    def _restore(self):
        # A new daemon cannot recover old pipes; never claim a live orphan is reattached.
        for a in self.store.all('SELECT * FROM agents'):
            path = self.home/'agents'/a['id']/'owner.json'
            owner = {}
            try: owner = json.loads(path.read_text())
            except FileNotFoundError: pass
            except (ValueError,OSError): owner={'spawning':True}
            states = [live_identity(owner.get(k+'_pid'),owner.get(k+'_identity')) for k in ('guard','pi')]
            uncertain = any(v is not False for v in states) or bool(owner.get('spawning'))
            if owner.get('guard_pid') and group_members(owner['guard_pid']): uncertain=True
            if a['state'] in {'starting','running','needs_input','idle','stopping','orphaned'}:
                self.store.agent_update(a['id'],state='orphaned' if uncertain else 'dormant',cleanup='unknown' if uncertain else 'verified')
            for r in self.store.all("SELECT * FROM runs WHERE agent_id=? AND state IN ('running','starting','needs_input','stopping','queued')",(a['id'],)):
                self.store.finish(r['id'],'crashed' if r['state']!='queued' else 'cancelled','',
                    'Daemon restarted; execution outcome may be partial. Inspect the session before explicit recovery.')
            self.store.agent_update(a['id'],current_run=None)
        self.store.execute("UPDATE receipts SET state='unknown' WHERE state IN ('queued','sending')")
    def notify(self):
        async def wake():
            async with self.changed: self.changed.notify_all()
        self.spawn_task(wake())
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
                self.store.bump(a['scope']); self.notify()
            return
        if kind=='agent_end':
            self.event(w,'agent_end',{'run_id':w.run_id})
            if w.run_id: self.spawn_task(self.settle(w,w.run_id))
            return
        if kind in {'auto_retry_start','auto_retry_end','auto_compaction_start','auto_compaction_end'}:
            self.event(w,kind,e)

    def _child_env(self, sid, spec):
        """Base environment for the guard/Pi child, built from the scope's bound
        snapshot — never a copy of the daemon's environ. Env-based model auth
        requires explicitly configured names (inheritance.child_env); profile env
        values come from the current config, never from the persisted copy."""
        snapshot = self.scope_env.get(sid) or {}
        inh = self.config['inheritance']
        allowed = set(BASE_KEYS) | {k for k in inh.get('child_env', []) if isinstance(k, str)}
        env = {k: v for k, v in snapshot.items() if k in allowed and isinstance(v, str)}
        profile = self.config['profiles'].get(spec.get('profile'), {}) if isinstance(self.config['profiles'], dict) else {}
        penv = profile.get('env', {}) if isinstance(profile, dict) else {}
        if isinstance(penv, dict):
            env.update({k: v for k, v in penv.items() if isinstance(k, str) and isinstance(v, str)})
        env['PI_AGENTS_MANAGED_CHILD'] = '1'
        return env

    async def _read_receipt(self, fd, aid, generation, timeout):
        """Read and parse the bridge's structured receipt from the pipe (in-memory,
        not the size-capped stderr.log). Ownership: the caller hands over the fd
        before awaiting; this method closes it exactly once on every path (success,
        malformed receipt, required-server failure, timeout, cancellation,
        transport-creation failure). The caller must not close it again — a second
        close during failure cleanup could hit an fd another connection reclaimed."""
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        pipe = os.fdopen(fd, 'rb', buffering=0)
        transport = None
        try:
            try:
                transport, _ = await loop.connect_read_pipe(lambda: protocol, pipe)
            except BaseException:
                pipe.close()  # connect_read_pipe failed without adopting the pipe
                raise
            line = await asyncio.wait_for(reader.readline(), timeout)
        except asyncio.TimeoutError:
            raise AgentError('bridge_unavailable', 'Managed MCP bridge did not report readiness in time; inspect the agent stderr log')
        finally:
            if transport is not None:
                transport.close()  # owns the fd; idempotent
        if not line:
            raise AgentError('bridge_unavailable', 'Managed MCP bridge closed without a readiness receipt; inspect the agent stderr log')
        try:
            receipt = json.loads(line)
        except ValueError:
            raise AgentError('bridge_unavailable', 'Managed MCP bridge receipt was malformed')
        if not isinstance(receipt, dict) or receipt.get('kind') != 'subagent-pi-bridge-receipt':
            raise AgentError('bridge_unavailable', 'Managed MCP bridge receipt was malformed')
        if receipt.get('agent') != aid or receipt.get('generation') != generation:
            raise AgentError('bridge_unavailable', 'Managed MCP bridge receipt did not match this agent generation')
        self.store.event(aid, None, generation, 'bridge_receipt', bounded(receipt, 4096))
        failed_required = [s.get('name') for s in receipt.get('servers', [])
                           if isinstance(s, dict) and s.get('required') and s.get('status') != 'ready']
        if failed_required:
            raise AgentError('inheritance_required_server_failed',
                             'required MCP server(s) failed to initialize in the child: ' + ', '.join(sorted(map(str, failed_required))))
        if receipt.get('state') != 'ready':
            raise AgentError('bridge_unavailable', 'Managed MCP bridge reported failure to start; inspect the agent stderr log')
        return receipt

    def _assert_writer_exclusive(self, aid, cwd):
        """Only one managed writer may own a cwd subtree at a time (a read label
        strips mutation builtins; it is not OS confinement)."""
        q="SELECT * FROM agents WHERE id!=? AND (state IN ('starting','running','needs_input','idle','orphaned','stopping') OR cleanup='unknown')"
        for other in self.store.all(q,(aid,)):
            if json.loads(other['launch']).get('access')!='write': continue
            left,right=Path(cwd),Path(other['cwd'])
            if left.is_relative_to(right) or right.is_relative_to(left):
                raise AgentError('writer_conflict','Another managed writer owns an overlapping cwd; close it or use read access',agent_id=other['id'])

    async def boot_worker(self, a):
        aid=a['id']
        if len([w for w in self.workers.values() if not w.closed]) >= self.config['max_resident_agents']:
            raise AgentError('capacity_exceeded','Resident Pi limit reached; close an idle agent first')
        spec=json.loads(a['launch'])
        if spec.get('access')=='write':
            self._assert_writer_exclusive(aid,a['cwd'])
        directory=self.home/'agents'/aid
        private_dir(directory)
        session=Path(a['session_file'])
        if session.is_symlink(): raise AgentError('unsafe_session','Managed session path must not be a symlink')
        if a['generation'] and (not session.exists() or session.stat().st_size==0):
            raise AgentError('session_unavailable','Previous Pi session is missing or empty; create a new agent explicitly')
        generation=a['generation']+1
        first_launch = a['generation']==0
        if first_launch:
            private_dir(directory/'sessions')
            argv=[*spec['argv'],'--session-dir',str(directory/'sessions')]
        else:
            argv=[*spec['argv'],'--session',str(session)]
        # Inherited skills/extensions are rebuilt from original sources on every boot;
        # the persisted launch spec and argv stay untouched.
        plan=self._inheritance_plan(a,spec,generation)
        argv=[*argv,*plan['argv']]
        if plan['bridge']:
            argv=self._merge_bridge_tool(argv)  # never a second --tools flag
        payload={**spec,'argv':argv,'generation':generation}
        atomic_json(directory/'launch.json',payload)
        if plan['diagnostics']:
            self.store.event(aid,None,generation,'inheritance_diagnostics',
                bounded({'source':plan.get('source'),'servers':plan['servers'],'diagnostics':plan['diagnostics']},4096))
        stderr_path=directory/'stderr.log'
        bootstrap_r=None; bootstrap_w=None; receipt_r=None; receipt_w=None
        if plan['payload'] is not None:
            body=dumps(plan['payload']).encode()
            if len(body)>BOOTSTRAP_MAX:
                raise AgentError('bootstrap_too_large','Inherited MCP configuration exceeds the private channel limit')
            bootstrap_r,bootstrap_w=os.pipe()
            receipt_r,receipt_w=os.pipe()
        handed_off=False
        try:
            self.store.agent_update(aid,state='starting',generation=generation,cleanup='pending')
            guard=Path(__file__).with_name('worker_guard.py')
            guard_env=self._child_env(a['scope'],spec)
            pass_fds=()
            if bootstrap_r is not None:
                guard_env['PI_AGENTS_BOOTSTRAP_FD']=str(bootstrap_r)
                # the child writes the receipt (write end), the daemon reads it (read end)
                guard_env['PI_AGENTS_BRIDGE_RECEIPT_FD']=str(receipt_w)
                pass_fds=(bootstrap_r,receipt_w)
            proc=await asyncio.create_subprocess_exec(sys.executable,str(guard),str(directory/'launch.json'),
                stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,
                start_new_session=True,limit=MAX_FRAME,cwd=spec['cwd'],env=guard_env,pass_fds=pass_fds)
            identity=process_identity(proc.pid)
            self.store.agent_update(aid,pid=proc.pid,identity=identity)
            a=self.store.agent(a['scope'],aid)
            w=Worker(self,a,proc); self.workers[aid]=w; w.start()
            if bootstrap_r is not None:
                os.close(bootstrap_r)  # daemon keeps only the payload write end
                os.close(receipt_w)    # child keeps the receipt write end
                bootstrap_r=None; receipt_w=None
                handed_off=True
                self.spawn_task(self._write_bootstrap(bootstrap_w,aid,generation,body))
            try:
                state=await w.rpc('get_state',timeout=self.config['startup_timeout_seconds'])
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
                if plan['payload'] is not None:
                    # get_state success does not prove the bridge loaded; require its
                    # structured receipt for this exact generation. Ownership transfers
                    # before the await: _read_receipt closes the fd exactly once, so
                    # failure cleanup below must not close it again.
                    fd, receipt_r = receipt_r, None
                    await self._read_receipt(fd,aid,generation,min(self.config['startup_timeout_seconds'],20))
                # Pin the model Pi actually selected so a later global default
                # change does not silently alter a recovered agent.
                resolved_model=state.get('model')
                if isinstance(resolved_model,dict) and resolved_model.get('id'):
                    if '--model' not in spec['argv']:
                        spec['argv'] += ['--model',resolved_model['id']]
                    if '--provider' not in spec['argv'] and resolved_model.get('provider'):
                        spec['argv'] += ['--provider',resolved_model['provider']]
                    spec['resolved_model']={'id':resolved_model['id'],'provider':resolved_model.get('provider')}
                    self.store.agent_update(aid,launch=dumps(spec))
                self.store.agent_update(aid,state='idle',cleanup='not_checked')
                self.event(w,'worker_ready',{'pi_session_id':state.get('sessionId'),'model':bounded(state.get('model'),1000)})
                return w
            except BaseException:
                w.stopping=True
                await self.terminate(w)
                raise
        finally:
            if bootstrap_r is not None:
                with contextlib.suppress(OSError): os.close(bootstrap_r)
            if not handed_off and bootstrap_w is not None:
                with contextlib.suppress(OSError): os.close(bootstrap_w)
            if receipt_r is not None:
                with contextlib.suppress(OSError): os.close(receipt_r)
            if receipt_w is not None:
                with contextlib.suppress(OSError): os.close(receipt_w)

    def _bind_scope_source(self, sid, p, source):
        """Bind a scope in two independent layers: layer 1 (always) the worker base
        environment from the opening client plus authorized child_env names — a
        child must find its interpreter whether or not Codex inheritance is on;
        layer 2 (master switch on) the Codex source pointer. Secrets stay in
        memory in both layers."""
        inh=self.config['inheritance']
        scope=self.store.scope(sid)
        env = source.get('env') if isinstance(source,dict) else None
        master_enabled = bool(inh.get('enabled',True))
        explicit_home = p.get('codex_home')
        if explicit_home is not None:
            explicit_home = str(Path(text(explicit_home,'codex_home',4096)).expanduser().resolve())
            if not Path(explicit_home).is_dir(): raise AgentError('invalid_cwd','codex_home must be an existing directory')
        home=None; mode=None
        if master_enabled:
            home,mode = resolve_codex_home({**inh,'codex_home':explicit_home or inh.get('codex_home')},env)
            stored=scope['codex_home']
            if stored and home and Path(stored)!=Path(home) and explicit_home is None and p.get('inheritance') is None:
                raise AgentError('inheritance_source_conflict',
                    f'Scope is bound to codex source {stored}; rebind explicitly with codex_home or inheritance parameters')
        # A credential refresh must not silently flip the per-scope switch.
        enabled=bool(scope['inheritance'])
        if p.get('inheritance') is False: enabled=False
        elif p.get('inheritance') is True: enabled=True
        # Layer-2 fields stay untouched while the master switch is off, so
        # re-enabling later does not find them clobbered by a disabled-era rebind.
        self.store.execute('UPDATE scopes SET codex_home=?,codex_source=?,inheritance=? WHERE id=?',
            ((str(home) if home else scope['codex_home']) if master_enabled else scope['codex_home'],
             (mode if home else scope['codex_source']) if master_enabled else scope['codex_source'],
             1 if enabled else 0, sid))
        if env is not None:
            names=set(BASE_KEYS) | {k for k in inh.get('child_env',[]) if isinstance(k,str)}
            if master_enabled and home is not None:
                try:
                    servers,_=parse_mcp_servers(home,read_codex_config(home))
                    names |= referenced_env_names(servers)
                except AgentError:
                    pass
            # Minimal per-scope snapshot: referenced names only, never persisted.
            self.scope_env[sid]={k:v for k,v in env.items() if k in names}

    def inheritance_doctor(self):
        inh=self.config['inheritance']
        report={'baseline':CODEX_MCP_BASELINE,
                'config':{k:inh.get(k) for k in ('enabled','skills','mcp','codex_home','mcp_protocol_mode')},'scopes':[],
                'note':'Environment variable and header values are never shown; only names and sources.'}
        for s in self.store.all('SELECT * FROM scopes ORDER BY created DESC LIMIT 100'):
            entry={'scope':s['id'],'label':s['label'],'cwd':s['cwd'],
                   'inheritance_enabled':bool(s['inheritance']) and bool(inh.get('enabled',True)),
                   'codex_home':s['codex_home'],'source_mode':s['codex_source'],
                   'bound_env_names':sorted(self.scope_env.get(s['id'],{}))}
            if inh.get('enabled') and s['inheritance'] and s['codex_home']:
                home=Path(s['codex_home'])
                try:
                    raw=read_codex_config(home)
                    skills,skill_diag=collect_skills(home,raw,s['cwd'],[])
                    servers,mcp_diag=parse_mcp_servers(home,raw)
                    try:
                        servers,env_diag=resolve_environment(servers,self.scope_env.get(s['id'],{}))
                        mcp_diag+=env_diag
                        servers,acc_diag=policy_filter(servers,'write')
                        mcp_diag+=acc_diag
                    except AgentError as exc:
                        mcp_diag.append(Diagnostic('mcp','required',exc.message))
                    entry.update(inherited_skills=[{'path':path,'name':Path(path).name} for path in skills],
                                 mcp_servers=[{'name':x['name'],'transport':x['transport'],
                                               'disposition':x.get('disposition'),'required':x.get('required',False),
                                               'reasons':x.get('reasons',[])} for x in servers],
                                 diagnostics=[d.as_dict() for d in skill_diag+mcp_diag])
                except AgentError as exc:
                    entry['error']=exc.as_dict()
            report['scopes'].append(entry)
        return report

    def require_worker(self,a):
        w=self.workers.get(a['id'])
        if not w or w.closed: raise AgentError('worker_unavailable','No connected Pi worker; use respawn after checking orphan state')
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
            await w.rpc('prompt',message=r['task'])
            self.store.execute("UPDATE receipts SET state='queued',updated=? WHERE id=? AND state='sending'",(now(),receipt))
        except AgentError as e:
            if e.code=='pi_rejected':
                self.store.finish(rid,'failed','',e.message)
                w.run_id=None
                self.store.agent_update(w.agent['id'],state='idle',current_run=None)
                self.notify()
            # Timeouts are uncertain: retain the run and wait for events rather than re-executing.
            raise
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
            # An agent_end is a turn boundary, not process death. Check queues before completing the job.
            try:
                state=await w.rpc('get_state')
                if state.get('isStreaming') or state.get('pendingMessageCount',0)>0: return
            except AgentError:
                return  # closed-process reconciliation handles it; never assert completion
            self.store.finish(rid,'failed' if w.error else 'completed',w.last_text,w.error,w.usage)
            self.event(w,'run_terminal',{'state':'failed' if w.error else 'completed'})
            w.run_id=None; w.ui.clear(); w.current_tool=None
            self.store.agent_update(w.agent['id'],state='idle',current_run=None,cleanup='not_checked')
            self.notify()
            q=self.store.one("SELECT id FROM runs WHERE agent_id=? AND state='queued' ORDER BY created LIMIT 1",(w.agent['id'],))
            if q and not self.closing:
                try: await self.start_run(w,q['id'])
                except AgentError as exc: self.event(w,'start_failed',exc.as_dict())

    async def fail_worker(self,w,error):
        async with self.agent_locks[w.agent['id']]:
            w.stopping=True
            if w.run_id:
                self.store.finish(w.run_id,'failed',w.last_text,error,w.usage); w.run_id=None
            await self.terminate(w)
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

    async def terminate(self,w):
        w.stopping=True
        pgid=w.proc.pid
        owned=live_identity(w.proc.pid,w.agent.get('identity')) is True
        if not owned:
            try:
                owner=json.loads((self.home/'agents'/w.agent['id']/'owner.json').read_text())
                owned=live_identity(owner.get('pi_pid'),owner.get('pi_identity')) is True
            except (OSError,ValueError): pass
        if not owned and group_members(pgid):
            self.store.agent_update(w.agent['id'],cleanup='unknown')
            return 'unknown'
        # a live process created by this daemon; ownership is never inferred from a bare PID
        for sig,wait in ((signal.SIGTERM,1.5),(signal.SIGKILL,1.0)):
            members=group_members(pgid)
            if not members: break
            with contextlib.suppress(ProcessLookupError): os.killpg(pgid,sig)
            until=time.monotonic()+wait
            while group_members(pgid) and time.monotonic()<until: await asyncio.sleep(.025)
        cleanup='unknown' if group_members(pgid) else 'verified'
        w.closed=True
        with contextlib.suppress(asyncio.TimeoutError): await asyncio.wait_for(w.proc.wait(),2)
        self.store.agent_update(w.agent['id'],cleanup=cleanup)
        return cleanup

    async def interrupt(self,a,terminal='interrupted'):
        w=self.require_worker(a)
        w.stopping=True
        self.store.agent_update(a['id'],state='stopping')
        for q in self.store.all("SELECT id FROM runs WHERE agent_id=? AND state='queued'",(a['id'],)):
            self.store.finish(q['id'],'cancelled','','Cancelled by explicit interruption')
        try:
            for ui in list(w.ui):
                await w.raw({'type':'extension_ui_response','id':ui,'cancelled':True})
            await w.rpc('clear_queue')
            await w.rpc('abort')
            s=await w.rpc('get_state')
            if s.get('isStreaming') or s.get('pendingMessageCount',0):
                raise AgentError('abort_unconfirmed','Pi still reports active or queued work')
            cleanup='not_checked'  # RPC idle does not prove detached shell descendants are gone
            resident=True
        except AgentError:
            cleanup=await self.terminate(w); resident=False
        if w.run_id:
            self.store.finish(w.run_id,terminal,w.last_text,'Explicit interruption; filesystem effects may be partial',w.usage)
            self.event(w,'run_terminal',{'state':terminal})
        w.run_id=None; w.ui.clear(); w.current_tool=None
        self.store.agent_update(a['id'],state='idle' if resident else 'dormant',current_run=None,cleanup=cleanup)
        self.store.bump(a['scope']); self.notify()
        w.stopping=not resident
        return {'agent_id':a['id'],'state':'idle' if resident else 'dormant','cleanup':cleanup,'process_retained':resident}

    async def reap_orphan(self,a):
        owner_path=self.home/'agents'/a['id']/'owner.json'
        try: owner=json.loads(owner_path.read_text())
        except FileNotFoundError:
            if live_identity(a.get('pid'),a.get('identity')) is not False:
                raise AgentError('ownership_unknown','Owner record missing; manual process inspection required')
            return 'verified'
        except (OSError,ValueError): raise AgentError('ownership_unknown','Owner record unreadable; refusing to signal a PID')
        matches=[live_identity(owner.get(k+'_pid'),owner.get(k+'_identity')) for k in ('guard','pi')]
        if any(v is None for v in matches) or owner.get('spawning'):
            raise AgentError('ownership_unknown','Cannot safely prove orphan process identity')
        if all(v is False for v in matches):
            if owner.get('guard_pid') and group_members(owner['guard_pid']):
                raise AgentError('ownership_unknown','A process group remains but its leaders cannot be verified; manual inspection required')
            return 'verified'
        pgid=owner.get('guard_pid')
        if not pgid: raise AgentError('ownership_unknown','No verified process group')
        for sig,delay in ((signal.SIGTERM,1.5),(signal.SIGKILL,1.0)):
            with contextlib.suppress(ProcessLookupError): os.killpg(pgid,sig)
            until=time.monotonic()+delay
            while group_members(pgid) and time.monotonic()<until: await asyncio.sleep(.03)
        return 'unknown' if group_members(pgid) else 'verified'

    def brief_agent(self,a):
        w=self.workers.get(a['id'])
        result={k:a[k] for k in ('id','name','scope','cwd','state','generation','current_run','cleanup')}
        if w and not w.closed:
            result.update(current_tool=w.current_tool,last_activity=w.last_activity)
            if w.ui: result['pending_input']=list(w.ui.values())[:4]
        return result
    def brief_run(self,r):
        return {k:r[k] for k in ('id','agent_id','state','created','started','ended','deadline','result_sha','ack','error')}
    def outstanding(self,sid,limit=20):
        rows=self.store.all("SELECT * FROM runs WHERE scope=? AND ack=0 ORDER BY created DESC LIMIT ?",(sid,limit))
        count=self.store.one("SELECT COUNT(*) n FROM runs WHERE scope=? AND ack=0",(sid,))['n']
        return {'revision':self.store.scope(sid)['revision'],'runs':[self.brief_run(r) for r in rows], 'total':count,'omitted':max(0,count-len(rows))}

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
            self._bind_scope_source(sid,p,source)
            return {'scope':sid,'cwd':cwd, 'outstanding':self.outstanding(sid)}
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
            if p.get('inheritance'): report['inheritance']=self.inheritance_doctor()
            return report
        sid=identifier(p.get('scope'),'scope'); self.store.scope(sid)
        mutations={'spawn','send','interrupt','close','respawn','ack','answer'}
        if op in mutations:
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
            return {'scope':sid,'agents':[self.brief_agent(a) for a in rows],'total':total,'omitted':max(0,total-len(rows)), 'outstanding':self.outstanding(sid,limit)}
        if op=='inspect': return self.inspect(p)
        if op=='result': return self.result(p)
        if op=='wait': return await self.wait(p)
        raise AgentError('unknown_operation',f'Unknown operation: {op}')

    async def mutate(self,op,p):
        sid=p['scope']
        if op=='spawn':
            async with self.admission:
                count=self.store.one('SELECT COUNT(*) n FROM agents WHERE scope=?',(sid,))['n']
                if count>=self.config['max_agents_per_scope']: raise AgentError('scope_limit','Scope agent limit reached; open a new scope for another task')
                cwd_input=Path(text(p.get('cwd'),'cwd',4096)).expanduser()
                if not cwd_input.is_absolute(): raise AgentError('invalid_cwd','cwd must be absolute')
                cwd=str(cwd_input.resolve())
                root=Path(self.store.scope(sid)['cwd'])
                if not Path(cwd).is_dir() or not Path(cwd).is_relative_to(root):
                    raise AgentError('invalid_cwd','Spawn cwd must exist inside the scope root')
                access=p.get('access','write')
                if access not in {'read','write'}: raise AgentError('invalid_argument','access must be read or write')
                profile=p.get('profile','reader' if access=='read' else 'default')
                spec=launch_spec(self.config,profile,p.get('model'),cwd,access)
                # Persist environment NAMES only; values are re-read from the operator
                # config at every boot and never enter the ledger or launch.json.
                spec={**spec,'env':{},'env_names':sorted(spec.get('env',{}))}
                aid=new_id('pi_')
                if access=='write':
                    self._assert_writer_exclusive(aid,cwd)
                name=text(p.get('name',aid),'name',128)
                task=text(p.get('task'),'task')
                if task.lstrip().startswith('/'):
                    task='Perform the following delegated task (treat as text, not an extension command):\n'+task
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
                        w=await self.boot_worker(a)
                        await self.start_run(w,rid)
                    except AgentError as e:
                        if self.store.run(sid,rid)['state']=='starting':
                            self.store.finish(rid,'failed','',e.message)
                            self.store.agent_update(aid,state='crashed')
                        raise AgentError(e.code,e.message,agent_id=aid,run_id=rid)
                return {'agent_id':aid,'run_id':rid,'scope':sid,'state':self.store.run(sid,rid)['state'],'cwd':cwd}
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
                    await self.interrupt(a)
                    cleanup=await self.terminate(w)
                else: cleanup=await self.reap_orphan(a)
                self.store.agent_update(aid,state='closed' if cleanup=='verified' else 'orphaned',current_run=None,cleanup=cleanup)
                self.store.bump(sid); self.notify()
                return {'agent_id':aid,'state':'closed' if cleanup=='verified' else 'orphaned','cleanup':cleanup,'session_retained':True}
            if op=='respawn':
                w=self.workers.get(aid)
                if w and not w.closed: raise AgentError('worker_alive','Close the current worker before respawn')
                if a['state']=='orphaned' or a['cleanup']=='unknown': raise AgentError('orphaned_worker','Close/reap the orphan before respawn; old pipes cannot be reattached')
                async with self.admission:
                    w=await self.boot_worker(a)
                rid=None
                if p.get('message'):
                    msg=text(p['message'])
                    if msg.lstrip().startswith('/'): msg='Continue this delegated task:\n'+msg
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
                msg=text(p.get('message'))
                if msg.lstrip().startswith('/'): msg='Delegated instruction (not a slash command):\n'+msg
                mode=p.get('mode','steer')
                if mode not in {'send','steer','follow_up'}: raise AgentError('invalid_argument','Invalid message mode')
                if p.get('interrupt',False):
                    await self.interrupt(a)
                    a=self.store.agent(sid,aid); w=self.require_worker(a); mode='send'
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

    def inspect(self,p):
        a=self.store.agent(p['scope'],identifier(p.get('agent_id'),'agent_id'))
        limit=integer(p.get('limit',20),'limit',1,100)
        budget=integer(p.get('max_bytes',4096),'max_bytes',1024,16384)
        after=integer(p.get('after',0),'after',0,2**63-1)
        detail=p.get('detail','tools')
        if detail not in {'tools','full'}: raise AgentError('invalid_argument','detail must be tools or full')
        sql='SELECT * FROM events WHERE agent_id=? AND seq>?'
        if detail=='tools': sql+=" AND type!='message'"
        rows=self.store.all(sql+' ORDER BY seq LIMIT ?',(a['id'],after,limit+1))
        receipts=self.store.all('SELECT id,run_id,state,updated FROM receipts WHERE agent_id=? ORDER BY created DESC LIMIT 5',(a['id'],))
        result={'agent':self.brief_agent(a),'events':[],'next_cursor':after,'has_more':False,'receipts':receipts}
        earliest=self.store.one('SELECT MIN(seq) n FROM events WHERE agent_id=?',(a['id'],))['n']
        result['history_pruned']=bool(after and earliest and after<earliest-1)
        for row in rows[:limit]:
            event={k:row[k] for k in ('seq','run_id','generation','type','created')}
            event['data']=json.loads(row['payload'])
            candidate={**result,'events':result['events']+[event]}
            if len(dumps(candidate).encode())>budget-100:
                if not result['events']:
                    event['data']={'preview':crop(dumps(event['data']),max(80,budget//4)),'truncated':True}
                    result['events'].append(event); result['next_cursor']=row['seq']
                result['has_more']=True; break
            result['events'].append(event); result['next_cursor']=row['seq']
        if len(rows)>len(result['events']): result['has_more']=True
        if len(dumps(result).encode())>budget:
            result['agent']={k:a[k] for k in ('id','state','generation','current_run')}
            result['receipts']=result['receipts'][:1]
        while len(dumps(result).encode())>budget and result['events']:
            if len(result['events'])==1:
                result['events'][0]['data']={'truncated':True}
                break
            result['events'].pop()
            result['next_cursor']=result['events'][-1]['seq']
            result['has_more']=True
        return result

    def result(self,p):
        r=self.store.run(p['scope'],identifier(p.get('run_id'),'run_id'))
        if r['state'] not in TERMINAL: raise AgentError('not_terminal','Result is not ready; use wait')
        limit=integer(p.get('max_bytes',4096),'max_bytes',256,16384)
        offset=integer(p.get('offset',0),'offset',0,2**40)
        path=Path(r['result_path'])
        size=path.stat().st_size
        if offset>size: raise AgentError('invalid_offset','Offset is beyond the result file')
        with path.open('rb') as f:
            f.seek(offset); raw=f.read(limit)
        if raw and (raw[0] & 0xC0)==0x80:
            raise AgentError('invalid_offset','Offset must be a UTF-8 boundary returned by this tool')
        # Bytes cursors never split UTF-8 in server-generated pagination.
        if offset+len(raw)<size:
            content=raw.decode('utf-8','ignore'); used=len(content.encode())
        else:
            try: content=raw.decode('utf-8'); used=len(raw)
            except UnicodeDecodeError: raise AgentError('invalid_offset','Offset must be a UTF-8 boundary returned by this tool')
        return {'run':self.brief_run(r),'text':content,'result_sha256':r['result_sha'],
                'offset':offset,'next_offset':offset+used,'has_more':offset+used<size,'total_bytes':size,
                'artifact_path':str(path),'acknowledged':bool(r['ack']),
                'result_truncated':json.loads(r['usage']).get('result_truncated',False),'usage':json.loads(r['usage'])}

    async def wait(self,p):
        sid=p['scope']; ms=integer(p.get('timeout_ms',25000),'timeout_ms',0,self.config['max_wait_seconds']*1000)
        mode=p.get('mode','any')
        if mode not in {'any','all'}: raise AgentError('invalid_argument','mode must be any or all')
        ids=p.get('run_ids')
        if ids is None:
            ids=[r['id'] for r in self.store.all("SELECT id FROM runs WHERE scope=? AND ack=0 ORDER BY created LIMIT 100",(sid,))]
        if not isinstance(ids,list) or len(ids)>100: raise AgentError('invalid_argument','run_ids must be a list of at most 100 ids')
        ids=list(dict.fromkeys(identifier(x,'run_id') for x in ids))
        until=time.monotonic()+ms/1000
        async with self.changed:
            while True:
                rows=[self.store.run(sid,rid) for rid in ids]
                done=[r for r in rows if r['state'] in TERMINAL]
                attention=[r for r in rows if r['state']=='needs_input']
                ready=not ids or bool(attention) or (bool(done) if mode=='any' else len(done)==len(ids))
                if ready or time.monotonic()>=until:
                    return {'scope':sid,'timed_out':not ready,'runs':[self.brief_run(r) for r in rows],
                            'outstanding_revision':self.store.scope(sid)['revision']}
                try: await asyncio.wait_for(self.changed.wait(),until-time.monotonic())
                except asyncio.TimeoutError: pass

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
                    with contextlib.suppress(Exception): await self.interrupt(a)
                    with contextlib.suppress(Exception): await self.terminate(w)
                    self.store.agent_update(a['id'],state='dormant',current_run=None)
        tasks=[t for w in self.workers.values() for t in w.tasks]+list(self.background)
        for t in tasks:
            if not t.done(): t.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        self.store.close()
