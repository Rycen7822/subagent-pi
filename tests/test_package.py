import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from subagent_pi.common import live_identity, crop
from subagent_pi import __version__
from unittest.mock import patch

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
if __name__=='__main__': unittest.main()
