import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from subagent_pi.common import live_identity, crop
from subagent_pi.client import call_timeout, boot_budget
from subagent_pi import __version__
from unittest.mock import patch

class ClientTimeoutBudget(unittest.TestCase):
    """A slow-but-healthy boot must not be reported as a failed mutation: the
    client wait has to cover the daemon's own boot budget."""
    def test_boot_ops_cover_daemon_boot_budget(self):
        budget=boot_budget(None)                       # startup_timeout_seconds default 30
        self.assertGreaterEqual(call_timeout('spawn',{},None),budget)
        self.assertGreaterEqual(call_timeout('respawn',{},None),budget)
        self.assertGreaterEqual(call_timeout('close',{},None),budget)
        self.assertGreater(budget,45)                  # the old flat 45s was the bug
    def test_boot_timeout_ignores_run_deadline(self):
        # timeout_seconds is the RUN deadline, not a boot budget: a one-week run
        # must not make the spawn call itself wait a week.
        self.assertEqual(call_timeout('spawn',{'timeout_ms':604800000},None),
                         call_timeout('spawn',{},None))
    def test_wait_scales_with_its_own_timeout(self):
        self.assertGreater(call_timeout('wait',{'timeout_ms':120000},None),
                           call_timeout('wait',{'timeout_ms':25000},None))
        self.assertEqual(call_timeout('list',{},None),45)

class PackageTests(unittest.TestCase):
    def test_manifest_structure_and_no_hooks(self):
        result=subprocess.run([sys.executable,str(ROOT/'scripts/validate_package.py')],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
    def test_real_local_install_and_portable_mcp_launcher(self):
        with tempfile.TemporaryDirectory(prefix='subagent-pi-install-') as tmp:
            base=Path(tmp); dest=base/'marketplace'; bins=base/'bin'; home=base/'state'
            cmd=[sys.executable,str(ROOT/'scripts/install.py'),'--dest',str(dest),'--bin-dir',str(bins),'--state-home',str(home)]
            r=subprocess.run(cmd,capture_output=True,text=True,timeout=20)
            self.assertEqual(r.returncode,0,r.stderr)
            plugin=dest/'plugins/subagent-pi'
            config=json.loads((plugin/'mcp.json').read_text())
            server=config['mcpServers']['subagent-pi']
            self.assertEqual(server['command'],'./bin/subagent-pi')
            self.assertEqual(server['args'],['mcp'])
            launcher=plugin/server['command']
            self.assertEqual(launcher.read_text().splitlines()[0],'#!'+str(Path(sys.executable).resolve()))
            legacy=json.loads((plugin/'.mcp.json').read_text())['mcpServers']['subagent-pi']
            self.assertEqual(legacy['command'],str(Path(sys.executable).resolve()))
            self.assertEqual(legacy['args'],[str(plugin/'bin/subagent-pi'),'mcp'])
            self.assertEqual(server['env']['PI_AGENTS_HOME'],str(home))
            self.assertFalse((plugin/'hooks').exists())
            from scripts.ship_manifest import ship_files
            shipped={f.relative_to(ROOT) for f in ship_files(ROOT)}
            installed={f.relative_to(plugin) for f in plugin.rglob('*') if f.is_file()}
            self.assertEqual(installed,shipped)
            self.assertFalse((plugin/'node_modules').exists())
            self.assertFalse((plugin/'.work').exists())
            version=subprocess.run([str(launcher),'--version'],cwd=base,capture_output=True,text=True)
            self.assertEqual(version.returncode,0,version.stderr)
            self.assertEqual(version.stdout.strip(),__version__)
            r2=subprocess.run(cmd,capture_output=True,text=True,timeout=20)
            self.assertNotEqual(r2.returncode,0)
            self.assertIn('Already installed',r2.stderr)
    def test_pid_reuse_is_not_live_owner(self):
        with patch('subagent_pi.common.process_identity',return_value='boot:new-tick'):
            self.assertFalse(live_identity(123,'boot:old-tick'))
            self.assertTrue(live_identity(123,'boot:new-tick'))
        with patch('subagent_pi.common.process_identity',return_value='unknown'):
            self.assertIsNone(live_identity(123,'boot:old-tick'))
    def test_utf8_crop(self):
        self.assertEqual(crop('🙂字',4),'🙂')
        self.assertEqual(crop('🙂字',3),'')
    def test_skill_stays_small(self):
        content=(ROOT/'skills/pi-subagents/SKILL.md').read_text()
        self.assertLessEqual(len(content.splitlines()),35)
        self.assertIn('../../docs/',content)
        self.assertNotIn('spawn_agent schema',content)

class ShipManifest(unittest.TestCase):
    """The ZIP and the installed plugin must ship exactly one file set; local
    caches, drafts and private notes must not reach either."""
    def ship(self):
        sys.path.insert(0,str(ROOT/'scripts'))
        import ship_manifest
        return ship_manifest
    def test_local_and_generated_files_are_excluded(self):
        m=self.ship()
        strays=['.mypy_cache/cache.json','.ruff_cache/x.json','.cursor/notes/secret.md',
                'dist/junk.txt','x.egg-info/PKG-INFO','CLAUDE.md','scratch.local.md',
                'daemon.log','.work/note.md','subagent_pi/__pycache__/x.pyc','build/out.bin']
        for stray in strays:
            self.assertTrue(m.excluded(ROOT/stray,ROOT),f'{stray} would be shipped')
    def test_runtime_files_are_included(self):
        m=self.ship()
        for needed in ['subagent_pi/runtime.py','subagent_pi/worker.py','subagent_pi/views.py',
                       'subagent_pi/binding.py','runtime/pi-sdk.mjs','runtime/task-queue.mjs','bin/subagent-pi','extensions/codex-mcp-bridge.ts',
                       'plugin.json','docs/architecture.md','skills/pi-subagents/SKILL.md',
                       '.github/workflows/ci.yml','scripts/ship_manifest.py']:
            self.assertFalse(m.excluded(ROOT/needed,ROOT),f'{needed} would be missing')
    def test_selection_is_deterministic_and_unique(self):
        m=self.ship()
        files=m.ship_files(ROOT)
        self.assertEqual(files,sorted(files))
        self.assertEqual(len(files),len(set(files)))

class RuntimeModuleBoundaries(unittest.TestCase):
    """runtime.py orchestrates state; the process, binding and projection mechanics
    live in worker/binding/views. Re-absorbing them is how the module became a god
    object, so pin the direction of every dependency that matters."""
    def test_runtime_does_not_import_process_mechanics(self):
        src=(ROOT/'subagent_pi/runtime.py').read_text()
        for forbidden in ('import signal','import fcntl','os.pipe(','create_subprocess_exec',
                          'killpg','connect_read_pipe','atomic_json','run_in_executor'):
            self.assertNotIn(forbidden,src,f'runtime.py re-acquired {forbidden!r}')
    def test_no_module_imports_runtime_into_the_mechanics(self):
        for module in ('worker','binding','views'):
            src=(ROOT/f'subagent_pi/{module}.py').read_text()
            self.assertNotIn('from .runtime',src,f'{module}.py must not depend on runtime.py')
    def test_local_import_breaks_the_boot_cycle(self):
        # worker.boot_worker imports binding lazily so binding can stay importable
        # without a Runtime instance (and no module-level cycle appears).
        src=(ROOT/'subagent_pi/worker.py').read_text()
        self.assertIn('from .binding import child_env, inheritance_plan',src)

class DevSetupStaging(unittest.TestCase):
    """scripts/dev-setup.mjs runs on every npm install/setup and stages Pi's type
    declarations for typecheck. It must survive its own previous output and must
    never delete a directory a symlink points at."""
    def setUp(self):
        if not shutil.which('node'): raise unittest.SkipTest('node not available')
        self.tmp=tempfile.TemporaryDirectory(prefix='dev-setup-'); self.root=Path(self.tmp.name)
        self.pi=self.root/'lib/node_modules/pi-coding-agent'
        (self.pi/'dist').mkdir(parents=True)
        (self.pi/'dist/index.d.ts').write_text('export declare const version: string;\n')
        cli=self.pi/'dist/cli.js'; cli.write_text('#!/usr/bin/env node\n'); cli.chmod(0o755)
        (self.pi/'node_modules/typebox').mkdir(parents=True)
        (self.pi/'node_modules/typebox/index.d.ts').write_text('export type T = 1;\n')
        (self.pi/'node_modules/@types/node').mkdir(parents=True)
        (self.pi/'node_modules/@types/node/index.d.ts').write_text('declare const nodeMarker: 1;\n')
        self.bin=self.root/'bin'; self.bin.mkdir()
        (self.bin/'pi').symlink_to(cli)          # the executable symlink must resolve into the dist tree
        self.cwd=self.root/'project'; (self.cwd/'node_modules/@types').mkdir(parents=True)
    def tearDown(self): self.tmp.cleanup()
    def setup_script(self):
        env={**os.environ,'PATH':str(self.bin)+os.pathsep+os.environ.get('PATH','')}
        return subprocess.run(['node',str(ROOT/'scripts/dev-setup.mjs')],cwd=self.cwd,env=env,
            capture_output=True,text=True,timeout=120)
    def test_repeated_runs_replace_stale_directories_and_refresh_types(self):
        first=self.setup_script()
        self.assertEqual(first.returncode,0,first.stderr)
        staged=self.cwd/'node_modules/@types/node/index.d.ts'
        self.assertTrue(staged.is_file())
        (self.pi/'dist/index.d.ts').write_text('export declare const version: "refreshed";\n')
        second=self.setup_script()
        self.assertEqual(second.returncode,0,second.stderr)
        self.assertIn('refreshed',(self.cwd/'node_modules/pi-host/dist/index.d.ts').read_text())
        self.assertTrue((self.cwd/'node_modules/pi-host/node_modules/typebox/index.d.ts').is_file())
        self.assertIn('staged Pi types from',second.stdout)
    def test_symlinked_targets_are_unlinked_not_deleted(self):
        outside=self.root/'outside'; (outside/'keep').mkdir(parents=True)
        (outside/'keep/data.txt').write_text('must survive\n')
        (self.cwd/'node_modules/@types/node').symlink_to(outside)
        (self.cwd/'node_modules/pi-host').symlink_to(outside)
        for run in range(2):
            done=self.setup_script()
            self.assertEqual(done.returncode,0,f'run {run+1}: {done.stderr}')
        self.assertTrue((outside/'keep/data.txt').is_file(),'setup deleted the directory a symlink pointed at')
        self.assertFalse((self.cwd/'node_modules/@types/node').is_symlink())
        self.assertTrue((self.cwd/'node_modules/@types/node/index.d.ts').is_file())


class SdkTransport(unittest.TestCase):
    def test_task_ownership_contract(self):
        if not shutil.which('node'): self.skipTest('Node.js not installed')
        done=subprocess.run(['node','--test',str(ROOT/'tests/task-queue.test.mjs')],capture_output=True,text=True)
        self.assertEqual(done.returncode,0,done.stdout+done.stderr)

    def test_stock_pi_command_resolves_only_to_plugin_files(self):
        from subagent_pi.worker import managed_command
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); (root/'dist/bundle').mkdir(parents=True)
            manifest=root/'package.json'; manifest.write_text('{"name":"@earendil-works/pi-coding-agent"}')
            cli=root/'dist/bundle/cli.js'; cli.write_text('unchanged cli')
            sdk=root/'dist/index.js'; sdk.write_text('unchanged sdk')
            before={p:p.read_bytes() for p in (manifest,cli,sdk)}
            with patch('shutil.which',return_value='/usr/bin/node'):
                argv=managed_command([str(cli),'--mode','rpc'])
            self.assertEqual(argv[1:],[str(ROOT/'runtime/pi-sdk.mjs'),str(sdk),'--mode','rpc'])
            self.assertEqual({p:p.read_bytes() for p in before},before)
