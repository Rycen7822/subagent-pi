"""One managed Pi child: its JSONL RPC channel, boot handoff fds and process-group
ownership. The Runtime owns state; this module owns the process-facing mechanics."""
from __future__ import annotations
import asyncio
import contextlib
import json
import os
from pathlib import Path
import signal
import sys
import time

from .common import (MAX_FRAME, AgentError, atomic_json, bounded, crop, dumps, group_members,
    live_identity, new_id, now, private_dir, process_identity, read_frame)

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

def write_all(fd, data):
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]

async def boot_worker(rt, a):
    """Start the guard/Pi child for one agent generation, hand it the private
    bootstrap payload, and only report success after Pi confirms the managed
    session and the bridge reports readiness for this exact generation."""
    from .binding import child_env, inheritance_plan, merge_bridge_tool

    aid=a['id']
    if len([w for w in rt.workers.values() if not w.closed]) >= rt.config['max_resident_agents']:
        raise AgentError('capacity_exceeded','Resident Pi limit reached; close an idle agent first')
    spec=json.loads(a['launch'])
    if spec.get('access')=='write':
        rt.assert_writer_exclusive(aid,a['cwd'])
    directory=rt.home/'agents'/aid
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
    plan=inheritance_plan(rt,a,spec,generation)
    argv=[*argv,*plan['argv']]
    if plan['bridge']:
        argv=merge_bridge_tool(argv)  # never a second --tools flag
    payload={**spec,'argv':argv,'generation':generation}
    atomic_json(directory/'launch.json',payload)
    if plan['diagnostics']:
        rt.store.event(aid,None,generation,'inheritance_diagnostics',
            bounded({'source':plan.get('source'),'servers':plan['servers'],'diagnostics':plan['diagnostics']},4096))
    bootstrap_r=None; bootstrap_w=None; receipt_r=None; receipt_w=None
    if plan['payload'] is not None:
        body=dumps(plan['payload']).encode()
        if len(body)>BOOTSTRAP_MAX:
            raise AgentError('bootstrap_too_large','Inherited MCP configuration exceeds the private channel limit')
        bootstrap_r,bootstrap_w=os.pipe()
        receipt_r,receipt_w=os.pipe()
    handed_off=False
    try:
        rt.store.agent_update(aid,state='starting',generation=generation,cleanup='pending')
        guard=Path(__file__).with_name('worker_guard.py')
        guard_env=child_env(rt,a['scope'],spec)
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
        rt.store.agent_update(aid,pid=proc.pid,identity=identity)
        a=rt.store.agent(a['scope'],aid)
        w=Worker(rt,a,proc); rt.workers[aid]=w; w.start()
        if bootstrap_r is not None:
            os.close(bootstrap_r)  # daemon keeps only the payload write end
            os.close(receipt_w)    # child keeps the receipt write end
            bootstrap_r=None; receipt_w=None
            handed_off=True
            rt.spawn_task(write_bootstrap(rt,bootstrap_w,aid,generation,body))
        try:
            state=await w.rpc('get_state',timeout=rt.config['startup_timeout_seconds'])
            actual=state.get('sessionFile')
            if not actual or not Path(actual).is_absolute():
                raise AgentError('session_mismatch','Pi did not report an absolute persistent session path')
            actual_path=Path(actual).resolve()
            if first_launch:
                if not actual_path.is_relative_to((directory/'sessions').resolve()):
                    raise AgentError('session_mismatch','Pi session escaped its managed session directory')
                rt.store.agent_update(aid,session_file=str(actual_path))
            elif actual_path!=session.resolve():
                raise AgentError('session_mismatch','Pi did not select the managed session path')
            if state.get('isStreaming'):
                raise AgentError('unexpected_activity','Pi started a model turn without an explicit task')
            if plan['payload'] is not None:
                # get_state success does not prove the bridge loaded; require its
                # structured receipt for this exact generation. Ownership transfers
                # before the await: read_receipt closes the fd exactly once, so
                # failure cleanup below must not close it again.
                fd, receipt_r = receipt_r, None
                await read_receipt(rt,fd,aid,generation,min(rt.config['startup_timeout_seconds'],20))
            # Pin the model Pi actually selected so a later global default
            # change does not silently alter a recovered agent.
            resolved_model=state.get('model')
            if isinstance(resolved_model,dict) and resolved_model.get('id'):
                if '--model' not in spec['argv']:
                    spec['argv'] += ['--model',resolved_model['id']]
                if '--provider' not in spec['argv'] and resolved_model.get('provider'):
                    spec['argv'] += ['--provider',resolved_model['provider']]
                spec['resolved_model']={'id':resolved_model['id'],'provider':resolved_model.get('provider')}
                rt.store.agent_update(aid,launch=dumps(spec))
            rt.store.agent_update(aid,state='idle',cleanup='not_checked')
            rt.event(w,'worker_ready',{'pi_session_id':state.get('sessionId'),'model':bounded(state.get('model'),1000)})
            return w
        except BaseException:
            w.stopping=True
            await terminate(rt,w)
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

async def write_bootstrap(rt, fd, agent_id, generation, data):
    """Write the payload from a worker thread. The thread owns the fd and closes
    it when the write finishes or breaks; a timeout abandons the wait, never the
    write mid-close (closing an fd another thread still uses risks writing into
    a reused descriptor)."""
    def _write_and_close():
        try:
            write_all(fd, data)
            return 'ok'
        except OSError:
            return 'broken'
        finally:
            with contextlib.suppress(OSError):
                os.close(fd)
    try:
        status = await asyncio.wait_for(asyncio.get_running_loop().run_in_executor(None, _write_and_close), 30)
        if status == 'broken':  # store writes stay on the event-loop thread (sqlite is single-thread bound)
            rt.store.event(agent_id, None, generation, 'bootstrap_write_failed', {'error': 'BrokenPipeError'})
    except asyncio.TimeoutError:
        rt.store.event(agent_id, None, generation, 'bootstrap_write_timeout', {})

async def read_receipt(rt, fd, aid, generation, timeout):
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
    rt.store.event(aid, None, generation, 'bridge_receipt', bounded(receipt, 4096))
    failed_required = [s.get('name') for s in receipt.get('servers', [])
                       if isinstance(s, dict) and s.get('required') and s.get('status') != 'ready']
    if failed_required:
        raise AgentError('inheritance_required_server_failed',
                         'required MCP server(s) failed to initialize in the child: ' + ', '.join(sorted(map(str, failed_required))))
    if receipt.get('state') != 'ready':
        raise AgentError('bridge_unavailable', 'Managed MCP bridge reported failure to start; inspect the agent stderr log')
    return receipt

def ownership(directory: Path, a) -> dict:
    """The one owner-record verdict for reaping, reconciliation and terminate:
    'gone' | 'live' | 'unknown'. A missing or unreadable record proves nothing
    about the old writer, so only a positively dead leader with no surviving
    process group is gone, and only positively matched identities are live."""
    try:
        record = json.loads((directory/'owner.json').read_text())
    except FileNotFoundError:
        record = None
    except (ValueError, OSError):
        record = {'spawning': True}
    if record is None:
        if not a.get('pid'):
            if a.get('cleanup') != 'pending':
                return {'status': 'gone', 'reason': 'No child was ever launched for this agent', 'record': None}
            return {'status': 'unknown', 'reason': 'Owner record missing and the launch may have forked; manual process inspection required', 'record': None}
        if live_identity(a['pid'],a.get('identity')) is False and not group_members(a['pid']):
            return {'status': 'gone', 'reason': 'Verified leader is dead and no process group remains', 'record': None}
        return {'status': 'unknown', 'reason': 'Owner record missing; manual process inspection required', 'record': None}
    matches = {k: live_identity(record.get(k+'_pid'),record.get(k+'_identity')) for k in ('guard','pi')}
    if record.get('spawning') or any(v is None for v in matches.values()):
        return {'status': 'unknown', 'reason': 'Cannot safely prove orphan process identity', 'record': record}
    if any(matches.values()):
        return {'status': 'live', 'reason': 'A verified session owner is still running', 'record': record}
    if record.get('guard_pid') and group_members(record['guard_pid']):
        return {'status': 'unknown', 'reason': 'A process group remains but its leaders cannot be verified; manual inspection required', 'record': record}
    return {'status': 'gone', 'reason': 'Verified leaders are dead and no process group remains', 'record': record}

async def terminate(rt, w):
    w.stopping=True
    pgid=w.proc.pid
    owned=live_identity(w.proc.pid,w.agent.get('identity')) is True
    if not owned:
        owned=ownership(rt.home/'agents'/w.agent['id'],w.agent)['status']=='live'
    if not owned and group_members(pgid):
        rt.store.agent_update(w.agent['id'],cleanup='unknown')
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
    rt.store.agent_update(w.agent['id'],cleanup=cleanup)
    return cleanup

async def reap_orphan(rt, a):
    verdict=ownership(rt.home/'agents'/a['id'],a)
    if verdict['status']=='gone': return 'verified'
    if verdict['status']=='unknown':
        raise AgentError('ownership_unknown',verdict['reason'])
    pgid=verdict['record'].get('guard_pid')
    if not pgid: raise AgentError('ownership_unknown','No verified process group')
    for sig,delay in ((signal.SIGTERM,1.5),(signal.SIGKILL,1.0)):
        with contextlib.suppress(ProcessLookupError): os.killpg(pgid,sig)
        until=time.monotonic()+delay
        while group_members(pgid) and time.monotonic()<until: await asyncio.sleep(.03)
    return 'unknown' if group_members(pgid) else 'verified'
