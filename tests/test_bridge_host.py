"""Layer-4: the REAL TypeScript bridge, loaded by jiti exactly like Pi loads
extensions, driven against local fake stdio/HTTP MCP servers. No Pi process,
no model call, no disk cache. Skipped (with a documented reason) when the
installed Pi distribution or node is unavailable."""
from __future__ import annotations
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent

def find_pi_dir() -> Path | None:
    exe = shutil.which('pi')
    if not exe:
        return None
    real = Path(exe).resolve()
    for parent in [real.parent, *real.parents]:
        if (parent / 'dist' / 'cli' / 'args.js').is_file():
            return parent
    return None

def free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]

class HostHarness:
    """Spawns fake MCP servers, builds a bootstrap payload, drives the host."""
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.procs = []
    def start_stdio(self, mode='normal', hide=''):
        """Returns server config env vars; the bridge spawns the server itself
        with ONLY its declared env + base keys, so mode/call-log must travel in
        cfg.env — which also proves the per-server env delivery path."""
        env = {'FAKE_MCP_MODE': mode, 'FAKE_MCP_HIDE': hide,
               'FAKE_MCP_CALL_LOG': str(self.tmp / f'stdio-calls-{len(self.procs)}.log')}
        self.tmp_envs = getattr(self, 'tmp_envs', [])
        self.tmp_envs.append(env)
        return {'log': Path(env['FAKE_MCP_CALL_LOG']), 'env': env}
    def start_http(self, mode='normal'):
        port = free_port()
        env = {k: v for k, v in os.environ.items()}
        env['FAKE_MCP_HTTP_MODE'] = mode
        log = self.tmp / f'http-calls-{len(self.procs)}.log'
        env['FAKE_MCP_HTTP_CALL_LOG'] = str(log)
        proc = subprocess.Popen([sys.executable, str(HERE / 'fake_mcp_http.py'), str(port)],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, text=True)
        proc.stdout.readline()  # ready
        self.procs.append(proc)
        return {'proc': proc, 'log': log, 'url': f'http://127.0.0.1:{port}/mcp'}
    def run_host(self, servers, actions, timeout=60):
        payload = {'v': 1,
                   'agent': {'id': 'pi_hosttest', 'access': 'read', 'generation': 3},
                   'source': {'codex_home': '/unused', 'mode': 'scope_env'},
                   'mcp': {'servers': servers}}
        payload_file = self.tmp / 'payload.json'; payload_file.write_text(json.dumps(payload))
        actions_file = self.tmp / 'actions.json'; actions_file.write_text(json.dumps(actions))
        result_file = self.tmp / 'result.json'
        env = {k: v for k, v in os.environ.items()}
        pi_dir = find_pi_dir()
        env['PI_CODING_AGENT_DIR'] = str(pi_dir)
        env['BRIDGE_TS'] = str(ROOT / 'extensions')
        out = subprocess.run(['node', str(HERE / 'bridge_host.mjs'), str(payload_file),
                              str(actions_file), str(result_file)],
                             capture_output=True, text=True, timeout=timeout, env=env, cwd=str(ROOT))
        if out.returncode != 0:
            raise AssertionError(f'bridge host failed: {out.stderr[-2000:]}')
        return json.loads(result_file.read_text())
    def stop(self):
        for proc in self.procs:
            proc.terminate()
        for proc in self.procs:
            try: proc.wait(5)
            except subprocess.TimeoutExpired: proc.kill()

def stdio_cfg(name='local', server_env=None, **over):
    cfg = {'name': name, 'transport': 'stdio', 'command': sys.executable,
           'args': [str(HERE / 'fake_mcp_stdio.py')], 'cwd': None,
           'env': dict(server_env or {}),
           'startup_timeout_sec': 10, 'tool_timeout_sec': 5, 'required': False,
           'allowed_tools': None, 'disabled_tools': [], 'approval_default': 'prompt',
           'tool_approval': {}}
    cfg.update(over)
    return cfg

class BridgeHostTests(unittest.TestCase):
    def setUp(self):
        if find_pi_dir() is None: self.skipTest('installed pi distribution not found')
        if shutil.which('node') is None: self.skipTest('node not available')
        self.tmp = tempfile.TemporaryDirectory(prefix='bridge-host-')
        self.h = HostHarness(Path(self.tmp.name))
        self.addCleanup(self.h.stop)
        self.addCleanup(self.tmp.cleanup)
    def read_calls(self, log):
        return log.read_text().splitlines() if log.exists() else []

    # ---- happy paths ----
    def test_stdio_list_describe_call(self):
        srv = self.h.start_stdio()
        out = self.h.run_host([stdio_cfg(server_env=srv['env'])], [
            {'name': 'list', 'action': 'list'},
            {'name': 'describe', 'action': 'describe', 'server': 'local', 'tool': 'echo'},
            {'name': 'call', 'action': 'call', 'server': 'local', 'tool': 'echo', 'args': {'text': 'abc', 'count': 2}, 'confirm': True},
        ])
        self.assertEqual(out['receipt']['state'], 'ready')  # optional servers stay lazy
        self.assertTrue(out['registered'] and out['registered']['name'] == 'codex_mcp')
        listing = json.loads(next(r for r in out['results'] if r['step'] == 'list')['text'])
        self.assertEqual(listing['servers'][0]['policy']['approval_default'], 'prompt')
        described = next(r for r in out['results'] if r['step'] == 'describe')
        schema = described['details']['inputSchema']
        self.assertEqual(schema['required'], ['text'])
        self.assertEqual(schema['properties']['count']['type'], 'integer')
        called = next(r for r in out['results'] if r['step'] == 'call')
        self.assertIn('abcabc', called['text'])
        self.assertIn('structuredContent', called['text'])
        self.assertIn('echo:{"count": 2, "text": "abc"}', self.read_calls(srv['log']))
    def test_http_json_and_sse(self):
        srv = self.h.start_http()
        cfg = {'name': 'web', 'transport': 'http', 'url': srv['url'],
               'headers': {}, 'bearer_token': None, 'startup_timeout_sec': 10,
               'tool_timeout_sec': 5, 'required': False, 'allowed_tools': ['search'],
               'disabled_tools': [], 'approval_default': 'auto', 'tool_approval': {}}
        out = self.h.run_host([cfg], [
            {'name': 'describe', 'action': 'describe', 'server': 'web', 'tool': 'search'},
            {'name': 'call', 'action': 'call', 'server': 'web', 'tool': 'search', 'args': {'query': 'x'}},
        ])
        self.assertEqual(out['receipt']['state'], 'ready')
        called = next(r for r in out['results'] if r['step'] == 'call')
        self.assertEqual(called['kind'], 'result')
        self.assertIn('published', called['text'])

    # ---- stdio failure modes (F03) ----
    def test_stdio_deadline_rejects_once(self):
        srv = self.h.start_stdio(mode='no_answer')
        out = self.h.run_host([stdio_cfg(server_env=srv['env'], tool_timeout_sec=2)], [
            {'name': 'call', 'action': 'call', 'server': 'local', 'tool': 'echo',
             'args': {'text': 'x'}, 'confirm': True},
        ])
        err = out['results'][0]
        self.assertEqual(err['kind'], 'error')
        self.assertIn('timed out', err['message'])
        # Exactly one request reached the server: no retry, no duplicate.
        self.assertEqual(len(self.read_calls(srv['log'])), 1)
    def test_stdio_exit_mid_call(self):
        srv = self.h.start_stdio(mode='die_after_init')
        out = self.h.run_host([stdio_cfg(server_env=srv['env'])], [
            {'name': 'call', 'action': 'call', 'server': 'local', 'tool': 'echo', 'args': {'text': 'x'}},
        ])
        self.assertEqual(out['results'][0]['kind'], 'error')
    def test_required_server_init_failure_fails_receipt(self):
        # F11: a required stdio server that cannot start must flip the receipt.
        cfg = stdio_cfg(name='core', required=True, command='/definitely/missing/binary')
        out = self.h.run_host([cfg], [{'name': 'call', 'action': 'call', 'server': 'core',
                                       'tool': 'anything', 'args': {}}])
        self.assertEqual(out['receipt']['state'], 'failed')
        entry = next(s for s in out['receipt']['servers'] if s['name'] == 'core')
        self.assertEqual(entry['status'], 'failed')
        self.assertTrue(entry['required'])
        # The broken server is not callable either.
        self.assertEqual(out['results'][0]['kind'], 'error')
    def test_http_hang_bounded(self):
        srv = self.h.start_http(mode='headers_then_hang')
        cfg = {'name': 'web', 'transport': 'http', 'url': srv['url'], 'headers': {},
               'bearer_token': None, 'startup_timeout_sec': 3, 'tool_timeout_sec': 2,
               'required': False, 'allowed_tools': None, 'disabled_tools': [],
               'approval_default': 'auto', 'tool_approval': {}}
        started = time.monotonic()
        out = self.h.run_host([cfg], [{'name': 'call', 'action': 'call', 'server': 'web',
                                       'tool': 'publish', 'args': {'body': 'x'}}], timeout=30)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 20)  # deadline bounds the whole exchange
        self.assertEqual(out['results'][0]['kind'], 'error')

    # ---- policy (F06) ----
    def test_confirmation_flow_and_child_overrides(self):
        srv = self.h.start_stdio()
        cfg = stdio_cfg(server_env=srv['env'], approval_default='prompt',
                        tool_approval={'status': 'auto'})
        out = self.h.run_host([cfg], [
            {'name': 'denied', 'action': 'call', 'server': 'local', 'tool': 'delete_file', 'args': {'path': 'x'}},
            {'name': 'auto_tool', 'action': 'call', 'server': 'local', 'tool': 'status', 'args': {}},
            {'name': 'confirm_rejected', 'action': 'call', 'server': 'local', 'tool': 'echo',
             'args': {'text': 'no'}, 'confirm': False},
            {'name': 'confirm_accepted', 'action': 'call', 'server': 'local', 'tool': 'echo',
             'args': {'text': 'yes'}, 'confirm': True},
        ])
        by = {r['step']: r for r in out['results']}
        self.assertEqual(by['denied']['kind'], 'error')  # no allowlist -> readOnly only
        self.assertEqual(by['auto_tool']['kind'], 'result')  # per-tool auto needs no confirm
        self.assertIn('denied', by['confirm_rejected']['text'])
        self.assertEqual(by['confirm_accepted']['kind'], 'result')
        # delete_file was never sent; echo went out exactly once.
        calls = self.read_calls(srv['log'])
        self.assertNotIn('delete_file', ' '.join(calls))
        self.assertEqual([c for c in calls if c.startswith('echo:')], ['echo:{"text": "yes"}'])
    def test_confirm_all_cannot_be_reaxed_by_parent_auto(self):
        srv = self.h.start_stdio()
        cfg = stdio_cfg(server_env=srv['env'], approval_default='auto', tool_approval={'echo': 'auto'})
        cfg['confirm_all'] = True
        out = self.h.run_host([cfg], [
            {'name': 'call', 'action': 'call', 'server': 'local', 'tool': 'echo',
             'args': {'text': 'x'}, 'confirm': False},
        ])
        # confirm_all forces the confirmation; the host refuses -> nothing sent.
        self.assertIn('denied', out['results'][0]['text'])
        self.assertEqual(self.read_calls(srv['log']), [])
    def test_allowlist_and_deny_shape_describe(self):
        srv = self.h.start_stdio(hide='status')
        cfg = stdio_cfg(server_env=srv['env'], allowed_tools=['echo'], disabled_tools=[])
        out = self.h.run_host([cfg], [
            {'name': 'describe_hidden', 'action': 'describe', 'server': 'local', 'tool': 'status'},
            {'name': 'describe_denied', 'action': 'describe', 'server': 'local', 'tool': 'delete_file'},
            {'name': 'describe_ok', 'action': 'describe', 'server': 'local', 'tool': 'echo'},
        ])
        by = {r['step']: r for r in out['results']}
        self.assertEqual(by['describe_hidden']['kind'], 'error')
        self.assertEqual(by['describe_denied']['kind'], 'error')  # not in allowlist
        self.assertEqual(by['describe_ok']['kind'], 'result')
    def test_recursion_refused_by_bridge(self):
        # F12 belt-and-braces: the bridge itself refuses the management binary.
        cfg = stdio_cfg(command=str(ROOT / 'bin' / 'subagent-pi'), args=['mcp'])
        out = self.h.run_host([cfg], [{'name': 'call', 'action': 'call', 'server': 'local',
                                       'tool': 'anything', 'args': {}}])
        self.assertEqual(out['results'][0]['kind'], 'error')
        self.assertIn('refusing to start the subagent-pi management server', out['results'][0]['message'])

if __name__ == '__main__':
    unittest.main()
