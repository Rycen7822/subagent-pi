#!/usr/bin/env python3
"""Install a self-contained local Codex marketplace. Never edits Pi config or registers hooks."""
from __future__ import annotations
import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

SOURCE=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(SOURCE))
sys.path.insert(0,str(SOURCE/'scripts'))
from ship_manifest import ship_files
from subagent_pi import __version__
from subagent_pi.common import atomic_json, atomic_write, state_home

def install(args):
    if not sys.platform.startswith('linux'): raise RuntimeError('Version 0.1 supports Linux/WSL2. Run this script inside WSL.')
    if sys.version_info<(3,11): raise RuntimeError('Python 3.11 or newer is required')
    destination=args.dest.expanduser().resolve()
    if destination==SOURCE or destination.is_relative_to(SOURCE):
        raise RuntimeError('Installation destination must be outside the source directory')
    plugin=destination/'plugins/subagent-pi'
    home=args.state_home.expanduser().resolve() if args.state_home else state_home()
    if plugin.exists():
        if not args.force: raise RuntimeError('Already installed. Drain the daemon and use --force for an explicit replacement.')
        lock=home/'daemon.lock'
        if lock.exists():
            with lock.open('r+') as f:
                try: fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError: raise RuntimeError('Daemon is running. Close agents and run subagent-pi daemon stop before upgrading.')
    destination.mkdir(parents=True,exist_ok=True)
    (destination/'plugins').mkdir(exist_ok=True)
    stage=Path(tempfile.mkdtemp(prefix='.subagent-pi-stage-',dir=destination))
    try:
        # Install the exact release file set; never traverse private/cache trees.
        for source in ship_files(SOURCE):
            target = stage/'payload'/source.relative_to(SOURCE)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        entry=str(plugin/'bin/subagent-pi')
        # Resolve paths at install time. Do not depend on undocumented plugin-root substitutions or host cwd.
        server={'command':str(Path(sys.executable).resolve()),'args':[entry,'mcp'],'env':{'PI_AGENTS_HOME':str(home)}}
        portable={'$schema':'https://agent-plugins.org/schemas/1.0.0/mcp.schema.json','mcpServers':{'subagent-pi':{'type':'stdio',**server}}}
        atomic_json(stage/'payload/mcp.json',portable)
        atomic_json(stage/'payload/.mcp.json',{'mcpServers':{'subagent-pi':server}})
        backup=destination/'plugins/subagent-pi.previous'
        if backup.exists(): shutil.rmtree(backup)
        if plugin.exists(): plugin.rename(backup)
        try: (stage/'payload').rename(plugin)
        except BaseException:
            if backup.exists() and not plugin.exists(): backup.rename(plugin)
            raise
        os.chmod(plugin/'bin/subagent-pi',0o755)
    finally: shutil.rmtree(stage,ignore_errors=True)
    bin_dir=args.bin_dir.expanduser().resolve(); bin_dir.mkdir(parents=True,exist_ok=True)
    link=bin_dir/'subagent-pi'
    if link.exists() or link.is_symlink():
        same=link.is_symlink() and link.resolve()==(plugin/'bin/subagent-pi').resolve()
        if not same and not args.force: raise RuntimeError(f'{link} already exists. Refusing to replace it without --force.')
        link.unlink()
    link.symlink_to(plugin/'bin/subagent-pi')
    market=destination/'.agents/plugins/marketplace.json'
    atomic_json(market,{'name':'subagent-pi-local','interface':{'displayName':'Subagent Pi Local'},'plugins':[
        {'name':'subagent-pi','source':{'source':'local','path':'./plugins/subagent-pi'},
         'policy':{'installation':'AVAILABLE','authentication':'ON_INSTALL'},'category':'Productivity'}]})
    if args.pi:
        pi=Path(args.pi).expanduser().resolve()
        if not pi.is_file() or not os.access(pi,os.X_OK): raise RuntimeError(f'Pi executable is not executable: {pi}')
        config=home/'config.toml'
        if config.exists():
            print(f'Existing {config} preserved. Edit pi_command there to change the executable.')
        else: atomic_write(config,('pi_command = '+json.dumps([str(pi)])+'\n').encode())
    print(f'Installed Subagent Pi {__version__}: {plugin}')
    print(f'CLI: {link}')
    print(f'State: {home}')
    print('No hooks were installed. No ~/.pi or ~/.codex files were edited directly.')
    if args.register:
        codex=shutil.which('codex')
        if not codex: raise RuntimeError('Files are installed, but codex was not found. Register the marketplace later.')
        subprocess.run([codex,'plugin','marketplace','add',str(destination)],check=True)
    else:
        import shlex
        print('Register with: codex plugin marketplace add '+shlex.quote(str(destination)))
    print('Next: open /plugins in Codex, install Subagent Pi from Subagent Pi Local, then start a new session.')
    print('Add your bin directory to PATH if needed: export PATH="'+str(bin_dir)+':$PATH"')

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--dest',type=Path,default=Path.home()/'.local/share/subagent-pi-marketplace')
    p.add_argument('--bin-dir',type=Path,default=Path.home()/'.local/bin')
    p.add_argument('--state-home',type=Path)
    p.add_argument('--pi',help='Absolute path to an already installed Pi executable')
    p.add_argument('--register',action='store_true',help='Also call codex plugin marketplace add')
    p.add_argument('--force',action='store_true',help='Explicitly replace an existing installation; refuses a live daemon')
    try: install(p.parse_args())
    except (OSError,RuntimeError,subprocess.CalledProcessError) as e:
        print('Install error: '+str(e),file=sys.stderr); sys.exit(1)
if __name__=='__main__': main()
