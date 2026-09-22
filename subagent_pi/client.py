from __future__ import annotations
import asyncio
import contextlib
import os
from pathlib import Path
import subprocess
import sys
import time
from .common import MAX_FRAME, DEFAULT_WAIT_MS, AgentError, dumps, private_dir, read_frame, socket_path
from . import PROTOCOL_VERSION

BASE_TIMEOUT = 45
# Booting or reaping a worker can legitimately outlast BASE_TIMEOUT: spawn and
# respawn wait for the Pi handshake plus the bridge readiness receipt, and close
# reaps a process group with TERM then KILL. The daemon budgets those stages from
# startup_timeout_seconds, so the client must scale with it instead of reporting a
# committed mutation as a failure while the daemon is still working.
BOOT_OPS = frozenset({'spawn','respawn','close','interrupt'})

def boot_budget(home):
    """Daemon worst case for a boot/reap call: handshake + receipt (capped at 20s)
    + rpc + terminate reap, with headroom for a slow interpreter start."""
    startup = 30
    if home is not None:
        try:
            from .config import load_config
            startup = load_config(home)['startup_timeout_seconds']
        except Exception:
            pass
    return 2 * startup + 30

def call_timeout(op, params, home=None):
    """Client-side wait for one IPC call, derived from the daemon's own budget.
    For boot ops the run deadline (timeout_ms) is irrelevant: the call returns as
    soon as the worker is up, regardless of how long the task may then run."""
    if op in BOOT_OPS:
        return max(BASE_TIMEOUT, boot_budget(home))
    ms=params.get('timeout_ms',DEFAULT_WAIT_MS if op=='wait' else 0)
    return max(BASE_TIMEOUT, (ms or 0)/1000 + 10)

async def request(home,op,params,timeout=45,autostart=True,source=None):
    sock=socket_path(home)
    async def connect(): return await asyncio.open_unix_connection(str(sock),limit=MAX_FRAME)
    try: reader,writer=await connect()
    except (FileNotFoundError,ConnectionRefusedError):
        if not autostart: raise AgentError('daemon_unavailable','Daemon is not running')
        private_dir(home)
        log=home/'daemon.log'
        if log.exists() and log.stat().st_size>512*1024:
            log.replace(home/'daemon.previous.log')
        entry=Path(__file__).resolve().parent.parent/'bin/subagent-pi'
        env=os.environ.copy(); env['PI_AGENTS_HOME']=str(home)
        with log.open('ab') as f:
            subprocess.Popen([sys.executable,str(entry),'daemon','run'],stdin=subprocess.DEVNULL,
                             stdout=f,stderr=f,start_new_session=True,env=env,close_fds=True)
        until=time.monotonic()+8
        while True:
            try: reader,writer=await connect(); break
            except (FileNotFoundError,ConnectionRefusedError):
                if time.monotonic()>until: raise AgentError('daemon_start_failed',f'Cannot start daemon; inspect {log}')
                await asyncio.sleep(.05)
    try:
        frame={'v':PROTOCOL_VERSION,'op':op,'params':params}
        if source is not None: frame['source']=source
        writer.write((dumps(frame)+'\n').encode()); await writer.drain()
        response=await asyncio.wait_for(read_frame(reader),timeout)
        if not response: raise AgentError('connection_lost','No response; mutation may have committed. Retry the same request_id.')
        if not response.get('ok'): raise AgentError(**response.get('error',{'code':'protocol_error','message':'Invalid reply'}))
        return response['result']
    except asyncio.TimeoutError:
        raise AgentError('client_timeout','Client stopped waiting. Pi was NOT cancelled; query state or retry the SAME mutation request_id.')
    finally:
        writer.close()
        with contextlib.suppress(Exception): await writer.wait_closed()
