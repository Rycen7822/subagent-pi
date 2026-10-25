"""One managed Pi child: its JSONL SDK transport, boot handoff fds and process-group
ownership. The Runtime owns state; this module owns the process-facing mechanics."""
from __future__ import annotations
import asyncio
import contextlib
import json
import os
import re
from pathlib import Path
import signal
import subprocess
import sys
import time

from .common import (MAX_FRAME, AgentError, atomic_json, bounded, crop, dumps, group_members,
    live_identity, new_id, now, private_dir, process_identity, read_frame, IS_WINDOWS, python_executable)
from . import locks
if IS_WINDOWS:
    from . import windows

RESULT_CAP = 1024 * 1024
BOOTSTRAP_MAX = 4 * 1024 * 1024
# How long the built-in surface report may take after Pi answered get_state. The
# shipped extension writes it during session_start, so a missing report means it
# did not load or did not run; the boot then fails instead of claiming a
# built-in restriction that was never applied.
SURFACE_TIMEOUT_SECONDS = 10.0
SURFACE_PREFIX = 'subagent-pi-surface '
CONTEXT_READY = 'subagent-pi-context ready'

def message_text(message):
    content = message.get('content',[])
    if isinstance(content,str): return content
    if not isinstance(content,list): return ''
    return '\n'.join(str(p.get('text','')) for p in content if isinstance(p,dict) and p.get('type')=='text')

class Worker:
    def __init__(self, agent, proc, stderr_path, rpc_timeout, idle_timeout,
                 spawn_task, on_event, on_failure, on_exit, on_retire):
        self.agent, self.proc = agent, proc
        self.stderr_path, self.rpc_timeout = stderr_path, rpc_timeout
        self.spawn_task, self.on_event = spawn_task, on_event
        self.on_failure, self.on_exit, self.on_retire = on_failure, on_exit, on_retire
        self.generation = agent['generation']
        self.pending = {}
        self.run_id = None
        self.last_text = ''
        self.error = None
        self.usage = {}
        self.stopping = False
        self.closed = False
        self.ui = {}
        self.active_tools = {}
        self.last_activity = now()
        self.last_progress = time.monotonic()
        self.idle_since = None
        self.cancelled_run = False
        self.idle_timeout_seconds = idle_timeout
        self.events_written = 0
        self.tasks = []
        self.write_lock = asyncio.Lock()
        self.surface = None          # parsed report from extensions/managed-surface.ts
        self.surface_ready = asyncio.Event()
        self.context_ready = asyncio.Event()
        self.surface_buf = b''
        # Set when the host starts work no daemon run owns (see Runtime.stop_unowned):
        # the worker is being stopped and none of its output may be absorbed.
        self.tainted = None
    @property
    def current_tool(self):
        return next(iter(self.active_tools.values()),None)
    def idle_seconds(self):
        # Tool execution and parent input are owned waits, not model silence.
        if not self.run_id or self.active_tools or self.ui: return None
        return max(0,time.monotonic()-self.last_progress)
    def start(self):
        self.tasks = [self.spawn_task(self.read_stdout()),self.spawn_task(self.read_stderr()),self.spawn_task(self.watch_exit())]
        for task in self.tasks: task.add_done_callback(self.retire)
    def retire(self, _task):
        # Exit reconciliation can await the agent lock while a replacement boots.
        # Only release this generation after every reader and callback is done.
        if self.closed and all(task.done() for task in self.tasks):
            self.on_retire(self)
    async def rpc(self, kind, timeout=None, **params):
        if self.closed or self.proc.returncode is not None:
            raise AgentError('worker_unavailable','Pi process is not connected; inspect then respawn')
        rid = new_id('rpc_')
        future = asyncio.get_running_loop().create_future()
        self.pending[rid] = future
        try:
            timeout = timeout or self.rpc_timeout
            async with asyncio.timeout(timeout):
                await self.raw({'id':rid,'type':kind,**params},timeout=timeout)
                response = await asyncio.shield(future)
            if not response.get('success'):
                raise AgentError('pi_rejected',crop(str(response.get('error','Pi rejected the command')),2000),command=kind)
            return response.get('data') or {}
        except asyncio.TimeoutError:
            raise AgentError('rpc_timeout','Pi command outcome is uncertain; do not blindly retry',command=kind)
        finally:
            self.pending.pop(rid,None)
            if not future.done(): future.cancel()
    async def raw(self, value, timeout=None):
        try:
            async with asyncio.timeout(timeout or self.rpc_timeout):
                async with self.write_lock:
                    # A timed-out write may still be buffered. Drain it before
                    # accepting more bytes so repeated timeouts cannot grow RAM.
                    await self.proc.stdin.drain()
                    self.proc.stdin.write((dumps(value)+'\n').encode()); await self.proc.stdin.drain()
        except asyncio.TimeoutError:
            raise AgentError('rpc_timeout','Pi command outcome is uncertain; do not blindly retry',command=value.get('type'))
        except (BrokenPipeError, ConnectionResetError):
            raise AgentError('worker_unavailable','Pi RPC channel closed')
    async def read_stdout(self):
        try:
            while True:
                try: e = await read_frame(self.proc.stdout)
                except json.JSONDecodeError:
                    self.on_event(self,{'type':'protocol_warning','message':'Non-JSON stdout line omitted'})
                    continue
                if e is None: break
                if not isinstance(e,dict): continue
                if e.get('type') == 'response':
                    f = self.pending.get(e.get('id'))
                    if f and not f.done(): f.set_result(e)
                else:
                    self.on_event(self,e)
        except asyncio.CancelledError: raise
        except Exception as exc:
            self.error = f'RPC reader failed: {type(exc).__name__}: {exc}'
            with contextlib.suppress(Exception): self.on_event(self,{'type':'protocol_error','message':crop(self.error,1000)})
            self.spawn_task(self.on_failure(self,self.error))
    async def read_stderr(self):
        with self.stderr_path.open('ab') as f:
            total = f.tell()
            while True:
                chunk = await self.proc.stderr.read(4096)
                if not chunk: break
                if total < 512*1024:
                    data = chunk[:512*1024-total]; f.write(data); f.flush(); total += len(data)
                self.note_surface(chunk)
    def note_surface(self, chunk):
        """Collect the built-in surface report the shipped extension writes after
        reading Pi's live registry back. Only lines the daemon asked for (a
        profile that restricts the built-in surface) are meaningful; a no-plan
        line means the extension had nothing to apply."""
        parts = (self.surface_buf + chunk).split(b'\n')
        self.surface_buf = parts.pop()[-4096:]
        for raw in parts:
            line = raw.decode('utf-8','replace').rstrip('\r')
            if line == CONTEXT_READY:
                self.context_ready.set()
                continue
            if not line.startswith(SURFACE_PREFIX): continue
            words = line.split()
            if words[1:2] != ['applied']: continue
            self.surface = parse_surface_line(line)
            self.surface_ready.set()
    async def watch_exit(self):
        # asyncio.Process.wait() also waits for pipe EOF. A Pi descendant can
        # inherit stdout/stderr after the guard exits, so observe the reaped
        # leader directly and bound the final event drain.
        while self.proc.returncode is None:
            await asyncio.sleep(.05)
        code = self.proc.returncode
        readers = self.tasks[:2]
        _, pending = await asyncio.wait(readers,timeout=2)
        for task in pending: task.cancel()
        await asyncio.gather(*pending,return_exceptions=True)
        # Cancelling a StreamReader task alone leaves its pipe transport open.
        # No descendant may keep a retired Worker resident through that FD.
        for fd in (1,2):
            pipe = self.proc._transport.get_pipe_transport(fd)
            if pipe: pipe.close()
        for future in list(self.pending.values()):
            if not future.done(): future.set_exception(AgentError('worker_exited',f'Pi guard exited ({code})'))
        # A dead process is not yet available for replacement: finish its runs
        # and queue under the agent lock before publishing this handoff flag.
        await self.on_exit(self,code)
        self.closed = True

def close_quietly(fd):
    """The fd owner closes exactly once; a second close or an already-gone pipe is
    never an error here."""
    if fd is None: return
    if IS_WINDOWS and not isinstance(fd,int):
        fd.close(); return
    with contextlib.suppress(OSError): os.close(fd)

def _skill_file(path: str) -> Path:
    """--skill takes a skill directory; Pi reports the SKILL.md it registered."""
    entry = Path(path)
    if entry.name != 'SKILL.md': entry = entry / 'SKILL.md'
    try: return entry.resolve()
    except (OSError, RuntimeError): return entry


async def resolve_skills(w, plan):
    """Describe how Pi actually resolved the inherited skill paths.

    Pi resolves skill name collisions itself and keeps the skill it discovered
    from the user's own Pi configuration (discovered skills are registered before
    CLI --skill paths), so an inherited skill that duplicates a Pi skill is
    dropped at Pi's loading boundary. Asking the live child which skills its
    registry holds is the only honest way to report which inherited entries lost
    and which Pi entry was kept; a failure here never fails the boot.
    """
    from .inheritance import pi_skill_name
    try:
        data = await w.rpc('get_commands')
    except AgentError as exc:
        return bounded({'source':plan.get('source'),'error':exc.code,'message':crop(exc.message,300)})
    registered = {}
    for entry in data.get('commands') or []:
        if not isinstance(entry,dict) or entry.get('source') != 'skill': continue
        name = str(entry.get('name') or '').removeprefix('skill:')
        info = entry.get('sourceInfo') if isinstance(entry.get('sourceInfo'),dict) else {}
        path = info.get('path') or entry.get('path')
        if name and isinstance(path,str): registered[name] = {'path':path,'real':_skill_file(path)}
    by_real = {e['real']: n for n,e in registered.items()}
    records, ours = [], set()
    for path in plan['skills']:
        real = _skill_file(path)
        ours.add(real)
        if real in by_real:
            records.append({'path':path,'name':by_real[real],'state':'loaded'})
            continue
        name = pi_skill_name(real)
        kept = registered.get(name)
        if kept:
            records.append({'path':path,'name':name,'state':'skipped','kept':kept['path']})
        else:
            records.append({'path':path,'name':name,'state':'not_loaded'})
    pi_owned = [{'name':n,'path':e['path']} for n,e in registered.items() if e['real'] not in ours]
    return bounded({'source':plan.get('source'),'inherited':records,'pi_skills':pi_owned},4096)


def parse_surface_line(text):
    """Parse one `subagent-pi-surface applied ...` evidence line into fields."""
    fields = {}
    for part in text.split()[2:]:
        key,_,value = part.partition('=')
        fields[key] = value
    fields['ok'] = fields.get('ok') == 'true'
    return fields

async def surface_report(w, timeout=SURFACE_TIMEOUT_SECONDS):
    """Wait for Pi's built-in surface report before a restricted profile boots.

    The applied set is read back from Pi's live registry inside the child, so the
    daemon learns whether the profile's built-in surface really took effect (and
    which built-ins the child ended up with) instead of trusting argv. A missing
    or not-ok report fails the launch: a read-only worker is never handed a
    write-capable built-in that this plugin could not confirm it removed."""
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(w.surface_ready.wait(),timeout)
    return w.surface

def check_surface(report):
    if report is None:
        raise AgentError('tool_surface_unavailable',
            'Managed Pi never reported its built-in tool surface; check that the shipped extension loads (see the agent stderr log)')
    if not report['ok']:
        raise AgentError('tool_surface_unapplied',
            f"Managed Pi did not apply the profile's built-in tool surface (applied={report.get('builtins','')} expected={report.get('expected','')})")

def managed_command(argv):
    """Locate the SDK beside the chosen Pi executable; never edit that install.

    Explicit non-Pi commands are protocol implementations (principally the offline
    fake in tests). Boot verifies the plugin protocol before accepting either.
    """
    import shutil
    executable = Path(argv[0]).resolve()
    roots=list(executable.parents)[:5]
    if IS_WINDOWS:
        from .executables import argv as executable_argv
        resolved=executable_argv(str(executable))
        if len(resolved)>1:
            roots=list(Path(resolved[1]).resolve().parents)[:5]+roots
        roots=[candidate for directory in roots for candidate in (directory,directory/'node_modules/@earendil-works/pi-coding-agent')]
    for directory in roots:
        manifest = directory/'package.json'
        if not manifest.is_file(): continue
        try: package = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError): continue
        if package.get('name') != '@earendil-works/pi-coding-agent': continue
        version = package.get('version')
        match = re.fullmatch(r'([0-9]+)\.([0-9]+)\.([0-9]+)(?:\+[0-9A-Za-z.-]+)?', version) if isinstance(version, str) else None
        if not match or tuple(map(int, match.groups())) < (0, 99, 1):
            raise AgentError('unsupported_pi_version', f'Pi >=0.99.1 is required; found SDK version {version!r}')
        sdk = directory/'dist/index.js'
        node = shutil.which('node')
        if not sdk.is_file() or not node:
            raise AgentError('sdk_unavailable','Pi SDK and Node.js are required; reinstall the official Pi package')
        return [node,str(Path(__file__).resolve().parent.parent/'runtime/pi-sdk.mjs'),str(sdk),*argv[1:]]
    return argv

def session_argv(home, a, spec):
    directory=home/'agents'/a['id']
    private_dir(directory)
    session=Path(a['session_file'])
    if session.is_symlink(): raise AgentError('unsafe_session','Managed session path must not be a symlink')
    if a['generation'] and (not session.exists() or session.stat().st_size==0):
        raise AgentError('session_unavailable','Previous Pi session is missing or empty; create a new agent explicitly')
    from .config import managed_context_argv
    argv=managed_context_argv(spec['argv'])
    if not a['generation']:
        private_dir(directory/'sessions')
        argv += ['--session-dir',str(directory/'sessions')]
    else: argv += ['--session',str(session)]
    return directory,session,argv

def write_launch(directory, spec, argv, generation):
    atomic_json(directory/'launch.json',{**spec,'argv':managed_command(argv),'generation':generation})

def bootstrap_body(payload):
    if payload is None: return None
    body=dumps(payload).encode()
    if len(body)>BOOTSTRAP_MAX:
        raise AgentError('bootstrap_too_large','Inherited MCP configuration exceeds the private channel limit')
    return body

async def start_guard(directory, spec, guard_env, body, before_spawn):
    """Own launch pipes until handoff; returned fds belong to the caller."""
    if IS_WINDOWS:
        from .bootstrap import BootstrapChannel
        channel=await BootstrapChannel.open() if body is not None else None
        try:
            if channel: guard_env={**guard_env,'PI_AGENTS_BOOTSTRAP_PIPE':channel.name}
            guard_env={**{k:os.environ[k] for k in ('SYSTEMROOT','WINDIR') if k in os.environ},**guard_env}
            before_spawn()
            proc=await asyncio.create_subprocess_exec(python_executable(),str(Path(__file__).with_name('worker_guard.py')),
                str(directory/'launch.json'),stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,limit=MAX_FRAME,cwd=spec['cwd'],env=guard_env,
                creationflags=subprocess.CREATE_NO_WINDOW)
            if channel: channel.pid=proc.pid
            return proc,channel,channel
        except BaseException:
            if channel: channel.close()
            raise
    bootstrap_r=bootstrap_w=receipt_r=receipt_w=None
    try:
        if body is not None:
            bootstrap_r,bootstrap_w=os.pipe()
            receipt_r,receipt_w=os.pipe()
            guard_env={**guard_env,'PI_AGENTS_BOOTSTRAP_FD':str(bootstrap_r),
                       'PI_AGENTS_BRIDGE_RECEIPT_FD':str(receipt_w)}
        before_spawn()  # reserve generation only after all launch pipes exist
        proc=await asyncio.create_subprocess_exec(sys.executable,str(Path(__file__).with_name('worker_guard.py')),
            str(directory/'launch.json'),stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,start_new_session=True,limit=MAX_FRAME,
            cwd=spec['cwd'],env=guard_env,pass_fds=(bootstrap_r,receipt_w) if body is not None else ())
        close_quietly(bootstrap_r); close_quietly(receipt_w)
        bootstrap_r=receipt_w=None
        handoff=(proc,bootstrap_w,receipt_r)
        bootstrap_w=receipt_r=None
        return handoff
    finally:
        for fd in (bootstrap_r,bootstrap_w,receipt_r,receipt_w): close_quietly(fd)

async def write_bootstrap(fd, data):
    """Write the payload from a worker thread. The thread owns the fd and closes
    it when the write finishes or breaks; a timeout abandons the wait, never the
    write mid-close (closing an fd another thread still uses risks writing into
    a reused descriptor)."""
    if IS_WINDOWS:
        return await fd.send(data)
    def _write_and_close():
        try:
            with os.fdopen(fd, 'wb') as pipe:
                pipe.write(data)
            return 'ok'
        except OSError:
            return 'broken'
    try:
        return await asyncio.wait_for(asyncio.get_running_loop().run_in_executor(None, _write_and_close), 30)
    except asyncio.TimeoutError:
        return 'timeout'

async def read_receipt(fd, aid, generation, timeout):
    """Read and parse the bridge's structured receipt from the pipe (in-memory,
    not the size-capped stderr.log). Ownership: the caller hands over the fd
    before awaiting; this method closes it exactly once on every path (success,
    malformed receipt, required-server failure, timeout, cancellation,
    transport-creation failure). The caller must not close it again — a second
    close during failure cleanup could hit an fd another connection reclaimed."""
    if IS_WINDOWS:
        receipt=await fd.read(timeout)
        return validate_receipt(receipt,aid,generation)
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
    return validate_receipt(receipt,aid,generation)

def validate_receipt(receipt,aid,generation):
    if not isinstance(receipt, dict) or receipt.get('kind') != 'subagent-pi-bridge-receipt':
        raise AgentError('bridge_unavailable', 'Managed MCP bridge receipt was malformed')
    if receipt.get('agent') != aid or receipt.get('generation') != generation:
        raise AgentError('bridge_unavailable', 'Managed MCP bridge receipt did not match this agent generation')
    return receipt

def check_receipt(receipt):
    failed_required = [s.get('name') for s in receipt.get('servers', [])
                       if isinstance(s, dict) and s.get('required') and s.get('status') != 'ready']
    if failed_required:
        raise AgentError('inheritance_required_server_failed',
                         'required MCP server(s) failed to initialize in the child: ' + ', '.join(sorted(map(str, failed_required))))
    if receipt.get('state') != 'ready':
        raise AgentError('bridge_unavailable', 'Managed MCP bridge reported failure to start; inspect the agent stderr log')

def members(pgid):
    """Live members of one owned group/job, or None when that cannot be proved.

    An unreadable job is not an empty one: only None (unknown) is returned, so no
    caller can turn a permission or OS error into "cleanup verified"."""
    try:
        return group_members(pgid)
    except (AgentError, OSError):
        return None


def publish_job_cleanup(directory: Path, generation):
    """Persist Windows job proof for one dead generation.

    POSIX still needs the guard's receipt: an empty PGID cannot prove that
    descendants which detached from that group have exited.
    """
    if not IS_WINDOWS or generation is None:
        return None
    try:
        with open(directory / "session.lock", "a+b") as lease:
            if not locks.try_lock(lease.fileno()):
                return None
            try:
                record = json.loads((directory / "owner.json").read_text(encoding="utf-8"))
                if record.get("generation") != generation or not record.get("guard_pid"):
                    return None
                if any(live_identity(record.get(k + "_pid"), record.get(k + "_identity")) is not False
                       for k in ("guard", "pi")) or members(record["guard_pid"]) != []:
                    return None
                updated = {
                    **record, "spawning": False, "descendants_cleanup": "verified",
                    "cleanup_proof": "job-empty",
                    "cleanup_confirmation": {"generation": generation, "by": "daemon", "at": time.time()},
                }
                atomic_json(directory / "owner.json", updated)
                return updated
            finally:
                locks.unlock(lease.fileno())
    except (OSError, ValueError):
        return None


def ownership(directory: Path, a) -> dict:
    """The one owner-record verdict for reaping, reconciliation and terminate:
    'gone' | 'live' | 'unknown'. A missing or unreadable record proves nothing
    about the old writer, so only a positively dead leader with no surviving
    process group and recorded descendants are gone. A launched worker without
    its owner record stays unknown; only positively matched identities are live."""
    try:
        record = json.loads((directory/'owner.json').read_text(encoding="utf-8"))
    except FileNotFoundError:
        record = None
    except (ValueError, OSError):
        record = {'spawning': True}
    if record is None:
        if not a.get('pid'):
            if a.get('cleanup') != 'pending':
                return {'status': 'gone', 'reason': 'No child was ever launched for this agent', 'record': None}
            return {'status': 'unknown', 'reason': 'Owner record missing and the launch may have forked; manual process inspection required', 'record': None}
        return {'status': 'unknown', 'reason': 'Owner record missing; manual process inspection required', 'record': None}
    matches = {k: live_identity(record.get(k+'_pid'),record.get(k+'_identity')) for k in ('guard','pi')}
    if IS_WINDOWS and all(v is False for v in matches.values()) and (record.get('spawning') or record.get('descendants_cleanup')=='pending'):
        record=publish_job_cleanup(directory,record.get('generation')) or record
    if record.get('spawning') or any(v is None for v in matches.values()):
        return {'status': 'unknown', 'reason': 'Cannot safely prove orphan process identity', 'record': record}
    if any(matches.values()):
        return {'status': 'live', 'reason': 'A verified session owner is still running', 'record': record}
    survivors=members(record['guard_pid']) if record.get('guard_pid') else []
    if survivors is None:
        return {'status':'unknown','reason':'Cannot query the old process group/job','record':record}
    if survivors:
        return {'status': 'unknown', 'reason': 'A process group remains but its leaders cannot be verified; manual inspection required', 'record': record}
    if record.get('descendants_cleanup','verified')!='verified':
        return {'status': 'unknown', 'reason': 'Guard exited without confirming descendant cleanup; manual inspection required', 'record': record}
    return {'status': 'gone', 'reason': 'Verified leaders are dead and no process group remains', 'record': record}

async def stop_group(pgid, graceful_guard=False, identity=None):
    """Stop and verify an already-owned group.

    graceful_guard selects POSIX signal forwarding only. Windows has no TERM;
    explicit interruption terminates the Job, including detached descendants.
    """
    if IS_WINDOWS:
        # Open before checking identity, then retain that exact Job throughout
        # termination. A later lookup by PID could resolve a replacement guard.
        until = time.monotonic() + 8.0
        try:
            while time.monotonic() < until:
                with windows.Job(pgid) as job:
                    match = await asyncio.to_thread(live_identity, pgid, identity)
                    if match is not True:
                        return "verified" if match is False and job.members() == [] else "unknown"
                    if job.handle is not None:
                        while time.monotonic() < until:
                            job.terminate(1)
                            if job.members() == [] and live_identity(pgid, identity) is False:
                                return "verified"
                            await asyncio.sleep(0.05)
                await asyncio.sleep(0.05)  # Guard has not created its Job yet.
        except (windows.JobUnavailable, OSError):
            return "unknown"
        return "unknown"
    for sig, wait in ((signal.SIGTERM, 1.5), (signal.SIGKILL, 1.0)):
        if await asyncio.to_thread(members, pgid) == []:
            break
        # A current guard forwards TERM and reaps tools; avoid sending Pi a
        # duplicate signal while its once-only shutdown handler is disposing.
        with contextlib.suppress(ProcessLookupError):
            if graceful_guard and sig == signal.SIGTERM:
                os.kill(pgid, signal.SIGCONT)
            (os.kill if graceful_guard and sig == signal.SIGTERM else os.killpg)(pgid, sig)
        until = time.monotonic() + wait
        while time.monotonic() < until:
            current = await asyncio.to_thread(members, pgid)
            if current == []:
                break
            if current is None:
                return "unknown"
            await asyncio.sleep(0.025)
    return "verified" if await asyncio.to_thread(members, pgid) == [] else "unknown"

async def stop_owned_process(directory, agent, proc):
    """A just-spawned guard still needs identity proof before group signalling."""
    pgid = proc.pid
    # The daemon spawned this process, so its PID is the owner even before the
    # ledger row is updated with it. Without a recorded identity only an owner
    # record naming exactly this PID can prove the group is ours.
    agent = {**agent, "pid": agent.get("pid") or pgid}
    owned = live_identity(pgid, agent.get("identity")) is True
    record = ownership(directory, agent).get("record") or {}
    identity = record.get("guard_identity") if record.get("guard_pid") == pgid else agent.get("identity")
    if not owned:
        owned = record.get("guard_pid") == pgid and live_identity(pgid, identity) is True
    if not owned and await asyncio.to_thread(members, pgid) != []:
        return "unknown", False
    guarded = (
        record.get("guard_pid") == pgid
        and "descendants_cleanup" in record
        and live_identity(pgid, identity) is True
    )
    cleanup = await stop_group(pgid, graceful_guard=guarded, identity=identity)
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(proc.wait(), 2)
    # An empty group whose leader is still alive proves nothing (on Windows the
    # job may simply not exist yet), so require the owner itself to be gone.
    if cleanup == "verified" and live_identity(pgid, identity) is not False:
        cleanup = "unknown"
    if cleanup == "verified":
        publish_job_cleanup(directory, record.get("generation"))
        if ownership(directory, agent)["status"] != "gone":
            cleanup = "unknown"
    return cleanup, True

async def terminate(directory, w):
    w.stopping=True
    cleanup,attempted=await stop_owned_process(directory,w.agent,w.proc)
    if attempted: w.closed=True
    return cleanup

async def reap_orphan(directory, a):
    verdict=ownership(directory,a)
    if verdict['status']=='gone': return 'verified'
    if verdict['status']=='unknown':
        raise AgentError('ownership_unknown',verdict['reason'])
    pgid=verdict['record'].get('guard_pid')
    if not pgid: raise AgentError('ownership_unknown','No verified process group')
    record=verdict['record']
    guarded=('descendants_cleanup' in record and live_identity(pgid,record.get('guard_identity')) is True)
    cleanup=await stop_group(pgid,graceful_guard=guarded,identity=record.get('guard_identity'))
    if cleanup=='verified': publish_job_cleanup(directory,record.get('generation'))
    return cleanup if ownership(directory,a)['status']=='gone' else 'unknown'
