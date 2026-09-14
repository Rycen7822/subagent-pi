#!/usr/bin/env python3
"""Build a reproducible source ZIP from this directory (does not run paid-model tests)."""
import argparse
import hashlib
from pathlib import Path
import sys
import zipfile
ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from subagent_pi import __version__

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--output',type=Path,default=ROOT.parent/f'subagent-pi-{__version__}.zip')
a=p.parse_args()
files=[f for f in sorted(ROOT.rglob('*')) if f.is_file() and not any(part in {'__pycache__','.git','.pytest_cache','.venv','.work','.pi','.codex','.claude','node_modules'} for part in f.parts)
       and f.suffix not in {'.pyc','.zip','.sqlite','.sqlite-wal','.sqlite-shm'} and f.name!='FILES.sha256'
       and f.name not in {'daemon.log','daemon.previous.log'}]
checks=''.join(hashlib.sha256(f.read_bytes()).hexdigest()+'  '+str(f.relative_to(ROOT))+'\n' for f in files)
(ROOT/'FILES.sha256').write_text(checks)
files.append(ROOT/'FILES.sha256')
with zipfile.ZipFile(a.output,'w',zipfile.ZIP_DEFLATED,compresslevel=9) as z:
    for f in sorted(files):
        info=zipfile.ZipInfo('subagent-pi/'+str(f.relative_to(ROOT)),date_time=(2026,9,14,0,0,0))
        info.create_system=3
        executable=f.name=='subagent-pi' or f.parent.name=='scripts' and f.suffix=='.py'
        info.external_attr=((0o100755 if executable else 0o100644)<<16)
        info.compress_type=zipfile.ZIP_DEFLATED
        z.writestr(info,f.read_bytes())
sha=hashlib.sha256(a.output.read_bytes()).hexdigest()
a.output.with_suffix(a.output.suffix+'.sha256').write_text(sha+'  '+a.output.name+'\n')
print(a.output)
print(sha)
