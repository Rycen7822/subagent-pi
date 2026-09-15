import json
from pathlib import Path
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
    def test_real_local_install_and_absolute_mcp_paths(self):
        with tempfile.TemporaryDirectory(prefix='subagent-pi-install-') as tmp:
            base=Path(tmp); dest=base/'marketplace'; bins=base/'bin'; home=base/'state'
            cmd=[sys.executable,str(ROOT/'scripts/install.py'),'--dest',str(dest),'--bin-dir',str(bins),'--state-home',str(home)]
            r=subprocess.run(cmd,capture_output=True,text=True,timeout=20)
            self.assertEqual(r.returncode,0,r.stderr)
            plugin=dest/'plugins/subagent-pi'
            config=json.loads((plugin/'mcp.json').read_text())
            server=config['mcpServers']['subagent-pi']
            self.assertTrue(Path(server['command']).is_absolute())
            self.assertEqual(server['args'][0],str(plugin/'bin/subagent-pi'))
            self.assertEqual(server['env']['PI_AGENTS_HOME'],str(home))
            version=subprocess.run([sys.executable,str(bins/'subagent-pi'),'--version'],capture_output=True,text=True)
            self.assertEqual(version.returncode,0,version.stderr)
            self.assertEqual(version.stdout.strip(),__version__)
            self.assertFalse((plugin/'hooks').exists())
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
                       'subagent_pi/binding.py','bin/subagent-pi','extensions/codex-mcp-bridge.ts',
                       'plugin.json','docs/architecture.md','skills/pi-subagents/SKILL.md',
                       '.github/workflows/ci.yml','scripts/ship_manifest.py']:
            self.assertFalse(m.excluded(ROOT/needed,ROOT),f'{needed} would be missing')
    def test_selection_is_deterministic_and_unique(self):
        m=self.ship()
        files=m.ship_files(ROOT)
        self.assertEqual(files,sorted(files))
        self.assertEqual(len(files),len(set(files)))
    def test_package_and_install_share_one_rule(self):
        package=(ROOT/'scripts/package.py').read_text()
        install=(ROOT/'scripts/install.py').read_text()
        self.assertIn('ship_files',package)
        self.assertIn('excluded',install)

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
        self.assertIn('from .binding import child_env, inheritance_plan, merge_bridge_tool',src)

if __name__=='__main__': unittest.main()
