"""Lease holder for one Pi process. Internal entry point; never consumes stdout."""
from __future__ import annotations
import contextlib
import ctypes
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from subagent_pi.common import IS_WINDOWS, atomic_json, live_identity, process_identity
from subagent_pi import locks

def stop_children():
    """The kernel adopts orphan descendants here, including detached groups.

    Signal only our own children. Unreaped children cannot have their PID reused;
    no other thread or SIGCHLD handler in this guard calls waitpid.
    """
    children=Path(f'/proc/{os.getpid()}/task/{os.getpid()}/children')
    for sig,wait in ((signal.SIGTERM,.4),(signal.SIGKILL,.8)):
        until=time.monotonic()+wait
        while True:
            try:
                while os.waitpid(-1,os.WNOHANG)[0]: pass
            except ChildProcessError: pass
            try: pids=[int(pid) for pid in children.read_text(encoding="utf-8").split()]
            except (OSError,ValueError): return 'unknown'
            if not pids: return 'verified'
            for pid in pids:
                with contextlib.suppress(ProcessLookupError): os.kill(pid,sig)
            if time.monotonic()>=until: break
            time.sleep(.025)
    return 'unknown'

def posix_main():
    spec_path = Path(sys.argv[1])
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    lock = os.open(spec_path.parent/'session.lock',os.O_RDWR|os.O_CREAT,0o600)
    if not locks.try_lock(lock): raise RuntimeError("Session lease is already owned")
    libc=ctypes.CDLL(None,use_errno=True)
    if libc.prctl(36,ctypes.c_ulong(1),0,0,0)!=0:  # PR_SET_CHILD_SUBREAPER, Linux 3.4+
        raise OSError(ctypes.get_errno(),'Cannot enable managed child reaping')
    owner_path = spec_path.parent/'owner.json'
    if owner_path.exists():
        old = json.loads(owner_path.read_text(encoding="utf-8"))
        if old.get('spawning'): raise RuntimeError('Previous writer launch is unverified; manual reconciliation required')
        if old.get('descendants_cleanup','verified')!='verified':
            raise RuntimeError('Previous descendant cleanup is unverified; manual reconciliation required')
        for kind in ('guard','pi'):
            if live_identity(old.get(kind+'_pid'),old.get(kind+'_identity')) is not False:
                raise RuntimeError('Previous session owner is still live or unverifiable')
    owner = {'guard_pid':os.getpid(),'guard_identity':process_identity(os.getpid()),'generation':spec['generation'],'spawning':True,'descendants_cleanup':'pending'}
    atomic_json(owner_path,owner)
    env = os.environ.copy()
    env.update(spec.get('env',{}))
    env['PI_AGENTS_MANAGED_CHILD']='1'
    # fd numbers travel in env; pipes never touch disk. close_fds stays on, but
    # pass_fds preserves the numbers across the guard->Pi hop.
    fds=[]
    for name in ('PI_AGENTS_BOOTSTRAP_FD','PI_AGENTS_BRIDGE_RECEIPT_FD'):
        value=os.environ.get(name)
        if value and value.isdigit():
            fds.append(int(value))
    pass_fds=tuple(fds)
    proc=None; stopping=None
    def force_stop(sig,frame):
        if proc and proc.poll() is None:
            with contextlib.suppress(ProcessLookupError): proc.kill()
    def stop(sig,frame):
        nonlocal stopping
        if stopping is None: signal.setitimer(signal.ITIMER_REAL,.4)
        stopping=sig
        if proc and proc.poll() is None:
            with contextlib.suppress(ProcessLookupError): proc.send_signal(sig)
    signal.signal(signal.SIGALRM,force_stop)
    for sig in (signal.SIGTERM,signal.SIGINT,signal.SIGHUP): signal.signal(sig,stop)
    try:
        if stopping: raise SystemExit(128+stopping)
        # Pi shares the guard's group; tools may create independent groups.
        proc = subprocess.Popen(spec['argv'],cwd=spec['cwd'],env=env,stdin=0,stdout=1,stderr=2,close_fds=True,pass_fds=pass_fds)
        for fd in pass_fds:
            with contextlib.suppress(OSError): os.close(fd)
        owner.update(pi_pid=proc.pid,pi_identity=process_identity(proc.pid),spawning=False)
        atomic_json(owner_path,owner)
        if stopping: stop(stopping,None)
        rc = proc.wait()
    finally:
        signal.setitimer(signal.ITIMER_REAL,0)
        owner['descendants_cleanup']=stop_children()
        if owner['descendants_cleanup']=='verified': owner['spawning']=False
        atomic_json(owner_path,owner)
    sys.exit(rc if rc >= 0 else 128-rc)

def check_previous(owner_path: Path):
    """Refuse to reuse a session whose previous owner is not provably settled."""
    if not owner_path.exists():
        return
    old = json.loads(owner_path.read_text(encoding="utf-8"))
    if old.get("spawning"):
        raise RuntimeError("Previous writer launch is unverified; manual reconciliation required")
    if old.get("descendants_cleanup", "verified") != "verified":
        raise RuntimeError("Previous descendant cleanup is unverified; manual reconciliation required")
    for kind in ("guard", "pi"):
        if live_identity(old.get(kind + "_pid"), old.get(kind + "_identity")) is not False:
            raise RuntimeError("Previous session owner is still live or unverifiable")


def windows_main():
    """Own a job object, prove self-membership, then start Pi inside it."""
    from subagent_pi import windows

    spec_path = Path(sys.argv[1])
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    lock = os.open(spec_path.parent/'session.lock',os.O_RDWR|os.O_CREAT,0o600)
    if not locks.try_lock(lock):
        raise RuntimeError('Session lease is already owned')
    owner_path = spec_path.parent/'owner.json'
    check_previous(owner_path)
    owner = {
        "guard_pid": os.getpid(),
        "guard_identity": windows.process_identity(os.getpid()),
        "generation": spec["generation"],
        "spawning": True,
        "descendants_cleanup": "pending",
    }
    atomic_json(owner_path,owner)
    windows.create_job(os.getpid())  # joined and self-verified, or this raises
    env = os.environ.copy()
    env.update(spec.get('env',{}))
    env['PI_AGENTS_MANAGED_CHILD']='1'
    env['PI_AGENTS_PYTHON']=getattr(sys,'_base_executable',None) or sys.executable
    proc = subprocess.Popen(
        spec['argv'], cwd=spec['cwd'], env=env, stdin=0, stdout=1, stderr=2, close_fds=True,
    )
    # Identity from the handle we hold, never a fresh PID lookup.
    owner.update(pi_pid=proc.pid, pi_identity=windows.handle_identity(proc._handle), spawning=False)
    atomic_json(owner_path,owner)
    rc = proc.wait()
    # Pi is gone. The job is the kernel's own record of the rest: when nothing but
    # this guard remains, cleanup is proved and can be published before the job is
    # terminated (terminating it ends this guard too, so nothing may follow).
    try:
        remaining = [pid for pid in windows.job_members(os.getpid()) if pid != os.getpid()]
    except (OSError, windows.JobUnavailable):
        remaining = None
    owner['descendants_cleanup'] = 'verified' if remaining == [] else 'pending'
    atomic_json(owner_path, owner)
    code = rc if rc >= 0 else 128 - rc
    try:
        windows.terminate_job(os.getpid(), code)  # also ends this guard
    except (OSError, windows.JobUnavailable):
        pass
    sys.exit(code)


if __name__=='__main__':
    try: windows_main() if IS_WINDOWS else posix_main()
    except Exception as exc:
        print('subagent-pi worker guard: '+str(exc),file=sys.stderr)
        sys.exit(75)
