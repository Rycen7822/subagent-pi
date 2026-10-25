"""Resolve npm's Windows shims to Node without a command shell or quoting loss."""
from pathlib import Path
import re
import shutil

from .common import IS_WINDOWS, AgentError


def argv(command, *args):
    if IS_WINDOWS:
        command=shutil.which(str(command)) or command
    if not IS_WINDOWS or Path(command).suffix.lower() not in ('.cmd','.bat'):
        return [str(command),*args]
    path=Path(command)
    # npm shims have one Node entrypoint relative to %dp0%. Do not execute batch
    # text or place user task/messages on cmd.exe's command line.
    source=path.read_text(encoding='utf-8')
    entries=re.findall(r'"%dp0%[\\/]([^"\r\n]+\.(?:c?js|mjs))"',source,re.I)
    entries=list(dict.fromkeys(entries))
    if len(entries)!=1:
        raise AgentError('unsupported_launcher','Use an executable or an npm-generated Node shim; arbitrary batch launchers are unsupported')
    script=path.parent/Path(entries[0].replace('\\','/'))
    node=path.parent/'node.exe'
    if not node.is_file(): node=shutil.which('node')
    if not node or not script.is_file():
        raise AgentError('executable_unavailable','Node or npm shim entrypoint is missing')
    return [str(node),str(script),*args]
