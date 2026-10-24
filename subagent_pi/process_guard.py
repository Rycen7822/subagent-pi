"""Windows-owned process tree for short-lived Codex and inherited MCP clients."""
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from subagent_pi import windows
from subagent_pi.executables import argv

if __name__=='__main__':
    windows.create_job(os.getpid())  # Never spawn an unowned child.
    child=subprocess.Popen(argv(sys.argv[1],*sys.argv[2:]),stdin=0,stdout=1,stderr=2)
    code=child.wait()
    windows.terminate_job(os.getpid(),code if code>=0 else 1)
