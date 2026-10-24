"""Owned subprocess launch/cleanup for Codex notification clients."""
import asyncio
import contextlib
from pathlib import Path
import signal
import subprocess

from .common import IS_WINDOWS, group_members, python_executable, live_identity


async def spawn(command,*args,**kwargs):
    if IS_WINDOWS:
        from . import windows
        proc=await asyncio.create_subprocess_exec(
            python_executable(),str(Path(__file__).with_name('process_guard.py')),command,*args,
            creationflags=subprocess.CREATE_NO_WINDOW,**kwargs)
        popen=proc._transport.get_extra_info('subprocess')
        proc._subagent_identity=None
        if popen is not None:
            with contextlib.suppress(OSError):
                proc._subagent_identity=windows.handle_identity(popen._handle)
        return proc
    return await asyncio.create_subprocess_exec(command,*args,start_new_session=True,**kwargs)


async def finish(proc):
    if not proc: return
    if IS_WINDOWS:
        from . import windows
        # The old PID may already have been reused after a short queue client
        # exited. Open the Job before the identity check so termination retains
        # that object rather than resolving the PID again after verification.
        with contextlib.suppress(windows.JobUnavailable), windows.Job(proc.pid) as job:
            if live_identity(proc.pid,proc._subagent_identity) is True:
                job.terminate(1)
    elif group_members(proc.pid):
        import os
        with contextlib.suppress(ProcessLookupError): os.killpg(proc.pid,signal.SIGKILL)
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError): proc.kill()
    try:
        await asyncio.wait_for(proc.communicate(),2)
    except (TimeoutError,RuntimeError):
        # A detached descendant may still hold an inherited pipe. Retire our
        # transports without waiting indefinitely for somebody else's EOF.
        for fd in (1,2):
            pipe=proc._transport.get_pipe_transport(fd)
            if pipe: pipe.close()
        with contextlib.suppress(TimeoutError): await asyncio.wait_for(proc.wait(),1)
