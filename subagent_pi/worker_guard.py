"""Lease holder for one Pi process. Internal entry point; never consumes stdout."""
from __future__ import annotations
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from subagent_pi.common import atomic_json, live_identity, process_identity

def main():
    spec_path = Path(sys.argv[1])
    spec = json.loads(spec_path.read_text())
    lock = os.open(spec_path.parent/'session.lock',os.O_RDWR|os.O_CREAT,0o600)
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    owner_path = spec_path.parent/'owner.json'
    if owner_path.exists():
        old = json.loads(owner_path.read_text())
        if old.get('spawning'): raise RuntimeError('Previous writer launch is unverified; manual reconciliation required')
        for kind in ('guard','pi'):
            if live_identity(old.get(kind+'_pid'),old.get(kind+'_identity')) is not False:
                raise RuntimeError('Previous session owner is still live or unverifiable')
    owner = {'guard_pid':os.getpid(),'guard_identity':process_identity(os.getpid()),'generation':spec['generation'],'spawning':True}
    atomic_json(owner_path,owner)
    env = os.environ.copy()
    env.update(spec.get('env',{}))
    env['PI_AGENTS_MANAGED_CHILD']='1'
    # The Pi subprocess shares our new process group. Neither stdin nor stdout is a shell.
    proc = subprocess.Popen(spec['argv'],cwd=spec['cwd'],env=env,stdin=0,stdout=1,stderr=2,close_fds=True)
    owner.update(pi_pid=proc.pid,pi_identity=process_identity(proc.pid),spawning=False)
    atomic_json(owner_path,owner)
    rc = proc.wait()
    sys.exit(rc if rc >= 0 else 128-rc)

if __name__=='__main__':
    try: main()
    except Exception as exc:
        print('subagent-pi worker guard: '+str(exc),file=sys.stderr)
        sys.exit(75)
