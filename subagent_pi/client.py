from __future__ import annotations
import asyncio
import contextlib
import os
from pathlib import Path
import subprocess
import sys
import time
from .common import MAX_FRAME, AgentError, dumps, private_dir, read_frame, socket_path
from . import PROTOCOL_VERSION

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
