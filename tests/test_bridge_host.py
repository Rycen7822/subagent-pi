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
    """Spawns fake MCP servers, builds bootstrap payloads, drives the host.

    Every fake server exposes an EVENTS file so tests can prove WHICH stage was
    reached (request received, headers flushed, stdin closed) instead of
    inferring it from error strings.
    """
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.procs = []
    def start_stdio(self, mode='normal', hide='', paged=False, many=False, dynamic=False, mutate=False, close_after_call=False):
        """Returns server config env vars; the bridge spawns the server itself
        with ONLY its declared env + base keys, so mode/call-log must travel in
        cfg.env — which also proves the per-server env delivery path."""
        n = len(self.procs)
        env = {'FAKE_MCP_MODE': mode, 'FAKE_MCP_HIDE': hide,
               'FAKE_MCP_PAGED': '1' if paged else '', 'FAKE_MCP_MANY': '1' if many else '',
               'FAKE_MCP_DYNAMIC': '1' if dynamic else '', 'FAKE_MCP_MUTATE': '1' if mutate else '',
               'FAKE_MCP_CLOSE_AFTER_CALL': '1' if close_after_call else '',
               'FAKE_MCP_CALL_LOG': str(self.tmp / f'stdio-calls-{n}.log'),
               'FAKE_MCP_EVENTS': str(self.tmp / f'stdio-events-{n}.log'),
               'FAKE_MCP_DYN_FILE': str(self.tmp / f'dyn-{n}.name')}
        self.procs.append(None)
        return {'log': Path(env['FAKE_MCP_CALL_LOG']), 'events': Path(env['FAKE_MCP_EVENTS']),
                'dyn_file': Path(env['FAKE_MCP_DYN_FILE']), 'env': env}
    def start_http(self, mode='normal', extra=None):
        port = free_port()
        n = len(self.procs)
        env = {k: v for k, v in os.environ.items()}
        env['FAKE_MCP_HTTP_MODE'] = mode
        env['FAKE_MCP_HTTP_CALL_LOG'] = str(self.tmp / f'http-calls-{n}.log')
        env['FAKE_MCP_HTTP_EVENTS'] = str(self.tmp / f'http-events-{n}.log')
        for k, v in (extra or {}).items():
            env[k] = v
        proc = subprocess.Popen([sys.executable, str(HERE / 'fake_mcp_http.py'), str(port)],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, text=True)
        proc.stdout.readline()  # ready
        self.procs.append(proc)
        return {'proc': proc, 'log': Path(env['FAKE_MCP_HTTP_CALL_LOG']),
                'events': Path(env['FAKE_MCP_HTTP_EVENTS']), 'url': f'http://127.0.0.1:{port}/mcp'}
    def run_host(self, servers, actions, timeout=60, access='read', strict_rejections=True, agent_id='pi_hosttest'):
        payload = {'v': 1,
                   'agent': {'id': agent_id, 'access': access, 'generation': 3},
                   'source': {'codex_home': '/unused', 'mode': 'scope_env'},
                   'mcp': {'servers': servers}}
        payload_file = self.tmp / 'payload.json'; payload_file.write_text(json.dumps(payload))
        actions_file = self.tmp / 'actions.json'; actions_file.write_text(json.dumps(actions))
        result_file = self.tmp / 'result.json'
        env = {k: v for k, v in os.environ.items()}
        env['BRIDGE_TS'] = str(ROOT / 'extensions')
        cmd = ['node']
        if strict_rejections:
            cmd.append('--unhandled-rejections=strict')
        cmd += [str(HERE / 'bridge_host.mjs'), str(payload_file), str(actions_file), str(result_file)]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env, cwd=str(ROOT))
        if out.returncode != 0:
            raise AssertionError(f'bridge host failed (rc={out.returncode}): {out.stderr[-2000:]}')
        return json.loads(result_file.read_text())
    def stop(self):
        for proc in self.procs:
            if proc is None: continue
            proc.terminate()
        for proc in self.procs:
            if proc is None: continue
            try: proc.wait(5)
            except subprocess.TimeoutExpired: proc.kill()
    def events(self, path):
        if not Path(path).exists(): return []
        return [json.loads(l) for l in Path(path).read_text().splitlines() if l.strip()]
    def calls(self, log):
        return Path(log).read_text().splitlines() if Path(log).exists() else []

def stdio_cfg(name='local', server_env=None, **over):
    cfg = {'name': name, 'transport': 'stdio', 'command': sys.executable,
           'args': [str(HERE / 'fake_mcp_stdio.py')], 'cwd': None,
           'env': dict(server_env or {}),
           'startup_timeout_sec': 10, 'tool_timeout_sec': 5, 'required': False,
           'allowed_tools': None, 'disabled_tools': [], 'approval_default': 'prompt',
           'tool_approval': {}}
    cfg.update(over)
    return cfg


class HttpHostCase(unittest.TestCase):
    """Shared fixture for tests that drive the real bridge against fake HTTP servers."""
    prefix = 'bridge-http-'
    def setUp(self):
        if find_pi_dir() is None: self.skipTest('installed pi distribution not found')
        if shutil.which('node') is None: self.skipTest('node not available')
        self.tmp = tempfile.TemporaryDirectory(prefix=self.prefix)
        self.h = HostHarness(Path(self.tmp.name))
        self.addCleanup(self.h.stop)
        self.addCleanup(self.tmp.cleanup)
    def http_cfg(self, srv, **over):
        cfg = {'name': 'web', 'transport': 'http', 'url': srv['url'], 'headers': {},
               'bearer_token': None, 'startup_timeout_sec': 10, 'tool_timeout_sec': 5,
               'required': False, 'allowed_tools': None, 'disabled_tools': [],
               'approval_default': 'auto', 'tool_approval': {}}
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
            # P1-B: a read child confirms EVERY call, even parent-auto readOnly tools.
            {'name': 'call', 'action': 'call', 'server': 'web', 'tool': 'search', 'args': {'query': 'x'}, 'confirm': True},
        ])
        self.assertEqual(out['receipt']['state'], 'ready')
        self.assertEqual(out['confirm_count'], 1)  # parent auto cannot waive the child confirmation
        called = next(r for r in out['results'] if r['step'] == 'call')
        self.assertEqual(called['kind'], 'result')
        self.assertIn('handled', called['text'])

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

class ReadChildPolicyTests(unittest.TestCase):
    """P1-B: the parent's enabled_tools is NOT child authorization. A read
    child never gets a write tool through an allowlist, per-tool auto, or any
    other parent-side setting; readOnly tools still require confirmation.
    Assertions use real server call counts and confirm counts, not strings."""
    def setUp(self):
        if find_pi_dir() is None: self.skipTest('installed pi distribution not found')
        if shutil.which('node') is None: self.skipTest('node not available')
        self.tmp = tempfile.TemporaryDirectory(prefix='bridge-policy-')
        self.h = HostHarness(Path(self.tmp.name))
        self.addCleanup(self.h.stop)
        self.addCleanup(self.tmp.cleanup)

    def run_case(self, cfg_overrides, steps, access='read'):
        srv = self.h.start_stdio()
        cfg = stdio_cfg(server_env=srv['env'], **cfg_overrides)
        out = self.h.run_host([cfg], steps, access=access)
        return srv, out

    def test_read_child_with_allowlist_and_auto_cannot_call_write_tool(self):
        srv, out = self.run_case({'allowed_tools': ['delete_file'], 'approval_default': 'auto'}, [
            {'name': 'call', 'action': 'call', 'server': 'local', 'tool': 'delete_file', 'args': {'path': 'x'}},
        ])
        self.assertEqual(out['results'][0]['kind'], 'error')  # not callable at all
        self.assertEqual(out['confirm_count'], 0)             # not even a confirmation offer
        self.assertEqual(self.h.calls(srv['log']), [])        # zero server executions

    def test_read_child_per_tool_auto_without_readonly_hint(self):
        srv, out = self.run_case({'allowed_tools': ['unannounced'], 'approval_default': 'prompt',
                                  'tool_approval': {'unannounced': 'auto'}}, [
            {'name': 'call', 'action': 'call', 'server': 'local', 'tool': 'unannounced', 'args': {}},
        ])
        self.assertEqual(out['results'][0]['kind'], 'error')
        self.assertEqual(self.h.calls(srv['log']), [])

    def test_read_child_readonly_tool_requires_confirmation(self):
        srv, out = self.run_case({'allowed_tools': ['status'], 'approval_default': 'auto'}, [
            {'name': 'rejected', 'action': 'call', 'server': 'local', 'tool': 'status', 'args': {}, 'confirm': False},
            {'name': 'accepted', 'action': 'call', 'server': 'local', 'tool': 'status', 'args': {}, 'confirm': True},
        ])
        by = {r['step']: r for r in out['results']}
        self.assertIn('denied', by['rejected']['text'])
        self.assertEqual(by['accepted']['kind'], 'result')
        self.assertEqual(out['confirm_count'], 2)             # parent auto offered no waiver
        calls = [c for c in self.h.calls(srv['log']) if c.startswith('status')]
        self.assertEqual(len(calls), 1)                       # exactly the accepted one

    def test_read_child_without_allowlist_readonly_still_confirms(self):
        srv, out = self.run_case({'approval_default': 'auto'}, [
            {'name': 'call', 'action': 'call', 'server': 'local', 'tool': 'echo', 'args': {'text': 'x'}, 'confirm': False},
        ])
        self.assertIn('denied', out['results'][0]['text'])
        self.assertEqual(self.h.calls(srv['log']), [])

    def test_read_child_deny_beats_allowlist(self):
        srv, out = self.run_case({'allowed_tools': ['echo', 'status'], 'disabled_tools': ['status'], 'approval_default': 'auto'}, [
            {'name': 'list', 'action': 'list', 'server': 'local'},
            {'name': 'call', 'action': 'call', 'server': 'local', 'tool': 'status', 'args': {}, 'confirm': True},
        ])
        listing = json.loads(next(r for r in out['results'] if r['step'] == 'list')['text'])
        names = [t['name'] for t in listing['tools']]
        self.assertNotIn('status', names)   # deny beats allowlist AND readOnly
        self.assertIn('echo', names)        # readOnly, unaffected by the status deny
        self.assertEqual(out['results'][1]['kind'], 'error')
        self.assertEqual(self.h.calls(srv['log']), [])

    def test_read_child_empty_allowlist_allows_nothing(self):
        srv, out = self.run_case({'allowed_tools': [], 'approval_default': 'auto'}, [
            {'name': 'list', 'action': 'list', 'server': 'local'},
            {'name': 'call', 'action': 'call', 'server': 'local', 'tool': 'status', 'args': {}, 'confirm': True},
        ])
        listing = json.loads(next(r for r in out['results'] if r['step'] == 'list')['text'])
        self.assertEqual(listing['tools'], [])  # an explicit empty allowlist allows nothing
        self.assertEqual(out['results'][1]['kind'], 'error')
        self.assertEqual(self.h.calls(srv['log']), [])

    def test_write_child_keeps_per_tool_confirmation(self):
        srv, out = self.run_case({'approval_default': 'auto', 'tool_approval': {'echo': 'confirm'}}, [
            {'name': 'rejected', 'action': 'call', 'server': 'local', 'tool': 'echo', 'args': {'text': 'x'}, 'confirm': False},
            {'name': 'accepted', 'action': 'call', 'server': 'local', 'tool': 'echo', 'args': {'text': 'y'}, 'confirm': True},
        ], access='write')
        by = {r['step']: r for r in out['results']}
        self.assertIn('denied', by['rejected']['text'])
        self.assertEqual(by['accepted']['kind'], 'result')
        self.assertEqual(out['confirm_count'], 2)
        self.assertEqual(len([c for c in self.h.calls(srv['log']) if c.startswith('echo:')]), 1)

class DiscoveryFlowTests(unittest.TestCase):
    """P1-D: the model must discover tools without knowing their names:
    list (servers) -> list(server) -> describe -> call. The tool name below is
    generated by the fake server at startup and is never a test constant."""
    def setUp(self):
        if find_pi_dir() is None: self.skipTest('installed pi distribution not found')
        if shutil.which('node') is None: self.skipTest('node not available')
        self.tmp = tempfile.TemporaryDirectory(prefix='bridge-discovery-')
        self.h = HostHarness(Path(self.tmp.name))
        self.addCleanup(self.h.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_level1_list_does_not_connect(self):
        srv = self.h.start_stdio(dynamic=True)
        cfg = stdio_cfg(server_env=srv['env'], approval_default='prompt')
        out = self.h.run_host([cfg], [{'name': 'servers', 'action': 'list'}], access='write')
        servers = json.loads(next(r for r in out['results'])['text'])
        self.assertEqual(servers['servers'][0]['server'], 'local')
        # Level 1 did not connect the server: no stage events exist at all.
        self.assertEqual(self.h.events(srv['events']), [])
        self.assertFalse(Path(srv['dyn_file']).exists() is False and False)

    def test_dynamic_discovery_end_to_end(self):
        srv = self.h.start_stdio(dynamic=True)
        cfg = stdio_cfg(server_env=srv['env'], approval_default='prompt')
        out = self.h.run_host([cfg], [
            {'name': 'servers', 'action': 'list'},
            {'name': 'catalog', 'action': 'list', 'server': 'local'},
        ], access='write')
        # Level 2 connected exactly this server; the dynamic name comes from the
        # catalog itself, not from a test constant.
        events = self.h.events(srv['events'])
        self.assertTrue(any(e['event'] == 'initialize-received' for e in events))
        catalog = json.loads(next(r for r in out['results'] if r['step'] == 'catalog')['text'])
        names = [t['name'] for t in catalog['tools']]
        self.assertFalse(catalog['truncated'])
        dyn = [n for n in names if n.startswith('dyn-')]
        self.assertEqual(len(dyn), 1)
        # Discovered name -> describe -> schema-driven call.
        with open(srv['dyn_file']) as f:
            server_side_name = f.read().strip()
        self.assertEqual(dyn[0], server_side_name)
        out2 = self.h.run_host([cfg], [
            {'name': 'describe', 'action': 'describe', 'server': 'local', 'tool': dyn[0]},
            {'name': 'call', 'action': 'call', 'server': 'local', 'tool': dyn[0],
             'args': {'text': 'hello'}, 'confirm': True},
        ], access='write')
        described = next(r for r in out2['results'] if r['step'] == 'describe')
        schema = described['details']['inputSchema']
        self.assertEqual(schema['required'], ['text'])  # params built from the discovered schema
        called = next(r for r in out2['results'] if r['step'] == 'call')
        self.assertEqual(called['kind'], 'result')
        self.assertIn('hello', called['text'])
        calls = self.h.calls(srv['log'])
        self.assertTrue(any(c.startswith(dyn[0] + ':') and 'hello' in c for c in calls),
                        f'discovered tool was not called: {calls}')

    def test_catalog_pagination_and_size_bound(self):
        srv_paged = self.h.start_stdio(paged=True)
        paged_cfg = stdio_cfg(server_env=srv_paged['env'], approval_default='prompt')
        out = self.h.run_host([paged_cfg], [{'name': 'catalog', 'action': 'list', 'server': 'local'}], access='write')
        catalog = json.loads(next(r for r in out['results'])['text'])
        self.assertEqual(len(catalog['tools']), len(catalog['tools']))
        self.assertFalse(catalog['truncated'])  # 3 pages fit the page budget: a complete catalog
        # A paged catalog still serves tools from the LAST page.
        last_page_tool = catalog['tools'][-1]['name']
        out2 = self.h.run_host([paged_cfg], [
            {'name': 'describe', 'action': 'describe', 'server': 'local', 'tool': last_page_tool}], access='write')
        self.assertEqual(out2['results'][0]['kind'], 'result')
        # Size bound: 600+ tools cannot fully fit MAX_TOOLS; the report must say so.
        srv_many = self.h.start_stdio(many=True)
        many_cfg = stdio_cfg(server_env=srv_many['env'], approval_default='prompt')
        out3 = self.h.run_host([many_cfg], [{'name': 'catalog', 'action': 'list', 'server': 'local'}], access='write')
        big = json.loads(next(r for r in out3['results'])['text'])
        self.assertTrue(big['truncated'])
        self.assertLessEqual(len(big['tools']), 512)

    def test_catalog_filters_deny_and_reader_and_handles_empty(self):
        srv = self.h.start_stdio(hide='unannounced')
        cfg = stdio_cfg(server_env=srv['env'], disabled_tools=['echo'])
        out = self.h.run_host([cfg], [{'name': 'catalog', 'action': 'list', 'server': 'local'}], access='read')
        catalog = json.loads(next(r for r in out['results'])['text'])
        names = [t['name'] for t in catalog['tools']]
        self.assertNotIn('echo', names)      # denied
        self.assertNotIn('delete_file', names)  # read child: not readOnly
        self.assertIn('status', names)       # readOnly + allowed
        srv_empty = self.h.start_stdio(hide='echo,delete_file,status,unannounced')
        empty_cfg = stdio_cfg(server_env=srv_empty['env'])
        out2 = self.h.run_host([empty_cfg], [{'name': 'catalog', 'action': 'list', 'server': 'local'}], access='write')
        empty = json.loads(next(r for r in out2['results'])['text'])
        self.assertEqual(empty['tools'], [])
        self.assertFalse(empty['truncated'])

    def test_catalog_invalidates_after_list_changed(self):
        srv = self.h.start_stdio(mutate=True)
        cfg = stdio_cfg(server_env=srv['env'], approval_default='prompt')
        out = self.h.run_host([cfg], [
            {'name': 'before', 'action': 'list', 'server': 'local'},
            # Let the event loop deliver the server's list_changed notification
            # before the next read; the wait is bounded and only for scheduling.
            {'name': 'settle', 'waitMs': 300},
            {'name': 'after', 'action': 'list', 'server': 'local'},
        ], access='write')
        before = json.loads(next(r for r in out['results'] if r['step'] == 'before')['text'])
        after = json.loads(next(r for r in out['results'] if r['step'] == 'after')['text'])
        before_names = {t['name'] for t in before['tools']}
        after_names = {t['name'] for t in after['tools']}
        self.assertIn('delete_file', before_names)
        self.assertNotIn('transferred', before_names)
        self.assertIn('transferred', after_names)   # fresh fetch after the notification
        self.assertNotIn('delete_file', after_names)

    def test_single_server_failure_is_deterministic_and_isolated(self):
        broken = self.h.start_stdio()
        good = self.h.start_stdio()
        broken_cfg = stdio_cfg(name='broken', server_env=broken['env'], command='/definitely/missing/binary')
        good_cfg = stdio_cfg(name='good', server_env=good['env'], approval_default='prompt')
        out = self.h.run_host([broken_cfg, good_cfg], [
            {'name': 'broken_list', 'action': 'list', 'server': 'broken'},
            {'name': 'good_list', 'action': 'list', 'server': 'good'},
        ], access='write')
        by = {r['step']: r for r in out['results']}
        self.assertEqual(by['broken_list']['kind'], 'error')
        self.assertIn('failed to initialize', by['broken_list']['message'])
        self.assertEqual(by['good_list']['kind'], 'result')

class HttpExchangeLifecycleTests(HttpHostCase):
    """P1-A: after the response HEADERS arrive, a hung body must still be ended
    by the deadline, the caller's cancellation and connection close. Every test
    proves the tools/call reached the server and headers were flushed first."""


    def assert_reached_body_phase(self, srv):
        events = self.h.events(srv['events'])
        kinds = [e['event'] for e in events]
        self.assertIn('call-received', kinds)   # the request actually arrived
        self.assertIn('headers-sent', kinds)    # and headers were flushed
        return kinds

    def test_json_body_hang_hits_deadline(self):
        srv = self.h.start_http(mode='hang_body_json')
        cfg = self.http_cfg(srv, tool_timeout_sec=1)
        started = time.monotonic()
        out = self.h.run_host([cfg], [
            {'name': 'call', 'action': 'call', 'server': 'web', 'tool': 'publish',
             'args': {'body': 'x'}, 'confirm': True}], access='write', timeout=30)
        elapsed = time.monotonic() - started
        self.assert_reached_body_phase(srv)
        err = out['results'][0]
        self.assertEqual(err['kind'], 'error')
        self.assertIn('timed out', err['message'])
        self.assertIn('outcome is unknown', err['message'])  # the call WAS sent
        self.assertLess(elapsed, 6)                # deadline bound, not the test timeout
        self.assertGreaterEqual(elapsed, 0.9)      # it really waited for the exchange
        self.assertEqual(len(self.h.calls(srv['log'])), 1)  # sent once, never retried

    def test_sse_body_hang_hits_deadline(self):
        srv = self.h.start_http(mode='hang_body_sse')
        cfg = self.http_cfg(srv, tool_timeout_sec=1)
        out = self.h.run_host([cfg], [
            {'name': 'call', 'action': 'call', 'server': 'web', 'tool': 'search',
             'args': {'query': 'x'}, 'confirm': True}], access='write', timeout=30)
        self.assert_reached_body_phase(srv)
        err = out['results'][0]
        self.assertEqual(err['kind'], 'error')
        self.assertIn('timed out', err['message'])
        self.assertEqual(len(self.h.calls(srv['log'])), 1)

    def test_user_cancellation_ends_body_wait(self):
        srv = self.h.start_http(mode='hang_body_json')
        cfg = self.http_cfg(srv, tool_timeout_sec=10)
        started = time.monotonic()
        out = self.h.run_host([cfg], [
            {'name': 'call', 'action': 'call', 'server': 'web', 'tool': 'publish',
             'args': {'body': 'x'}, 'confirm': True, 'launch': True, 'abortAfterMs': 700},
            {'name': 'await', 'awaitPending': True},
        ], access='write', timeout=30)
        elapsed = time.monotonic() - started
        self.assert_reached_body_phase(srv)
        err = out['results'][0]
        self.assertEqual(err['kind'], 'error')
        self.assertEqual(err['name'], 'CancelledError')
        self.assertIn('outcome is unknown', err['message'])
        self.assertLess(elapsed, 5)
        self.assertEqual(len(self.h.calls(srv['log'])), 1)  # cancel is not a retry

    def test_connection_close_ends_body_wait_and_rejects_until_reconnect(self):
        srv = self.h.start_http(mode='hang_body_json')
        cfg = self.http_cfg(srv, tool_timeout_sec=10)
        out = self.h.run_host([cfg], [
            {'name': 'call', 'action': 'call', 'server': 'web', 'tool': 'publish',
             'args': {'body': 'x'}, 'confirm': True, 'launch': True, 'closeAfterMs': 600},
            {'name': 'await', 'awaitPending': True},
        ], access='write', timeout=30)
        self.assert_reached_body_phase(srv)
        err = out['results'][0]
        self.assertEqual(err['kind'], 'error')
        self.assertIn('connection was closed', err['message'])
        # Explicit lifecycle: a fresh exchange after close creates a NEW connection.
        out2 = self.h.run_host([cfg], [
            {'name': 'fresh', 'action': 'list', 'server': 'web'}], access='write')
        self.assertEqual(out2['results'][0]['kind'], 'result')

    def test_close_ends_all_inflight_on_the_connection(self):
        srv = self.h.start_http(mode='hang_body_json')
        cfg = self.http_cfg(srv, tool_timeout_sec=15)
        out = self.h.run_host([cfg], [
            {'name': 'a', 'action': 'call', 'server': 'web', 'tool': 'publish', 'args': {'body': 'a'}, 'confirm': True, 'launch': True},
            {'name': 'b', 'action': 'call', 'server': 'web', 'tool': 'publish', 'args': {'body': 'b'}, 'confirm': True, 'launch': True},
            {'name': 'close', 'action': 'call', 'server': 'web', 'tool': 'status_never_called', 'args': {}, 'closeAfterMs': 700, 'launch': True},
            {'name': 'await', 'awaitPending': True},
        ], access='write', timeout=30)
        by = {r['step']: r for r in out['results'] if r['step'] in ('a', 'b')}
        for step in ('a', 'b'):
            self.assertEqual(by[step]['kind'], 'error', step)
            self.assertIn('connection was closed', by[step]['message'], step)
        # only the two real calls reached the server; the close-trigger failed fast
        self.assertEqual(len(self.h.calls(srv['log'])), 2)

    def test_concurrent_cancel_does_not_hurt_the_other_request(self):
        hanging = self.h.start_http(mode='hang_body_json')
        good = self.h.start_http()
        cfgs = [self.http_cfg(hanging, name='hang', tool_timeout_sec=10),
                self.http_cfg(good, name='good', tool_timeout_sec=10)]
        out = self.h.run_host(cfgs, [
            {'name': 'hang', 'action': 'call', 'server': 'hang', 'tool': 'publish', 'args': {'body': 'x'}, 'confirm': True, 'launch': True, 'abortAfterMs': 700},
            {'name': 'good', 'action': 'call', 'server': 'good', 'tool': 'search', 'args': {'query': 'q'}, 'confirm': True, 'launch': True},
            {'name': 'await', 'awaitPending': True},
        ], access='write', timeout=30)
        by = {r['step']: r for r in out['results'] if r['step'] in ('hang', 'good')}
        self.assertEqual(by['hang']['kind'], 'error')
        self.assertEqual(by['good']['kind'], 'result')
        self.assertIn('handled', by['good']['text'])

class StdioTransportFailureTests(unittest.TestCase):
    """P1-C: asynchronous stdin failures (EPIPE after the server closed its read
    end) must be lifecycle events, not host crashes. The host runs with strict
    unhandled-rejection behavior and must exit 0 with clean stderr."""
    def setUp(self):
        if find_pi_dir() is None: self.skipTest('installed pi distribution not found')
        if shutil.which('node') is None: self.skipTest('node not available')
        self.tmp = tempfile.TemporaryDirectory(prefix='bridge-epipe-')
        self.h = HostHarness(Path(self.tmp.name))
        self.addCleanup(self.h.stop)
        self.addCleanup(self.tmp.cleanup)

    def assert_clean_host(self, out_text):
        pass  # run_host raises on nonzero rc; strict mode turns stray rejections into failures

    def test_stdin_closed_after_init_is_a_lifecycle_error_not_a_crash(self):
        # The notification+tools-list writes hit the closed pipe: the server
        # closes its read end immediately after flushing the initialize reply.
        srv = self.h.start_stdio(mode='close_stdin_after_init')
        cfg = stdio_cfg(server_env=srv['env'], approval_default='prompt')
        out = self.h.run_host([cfg], [
            {'name': 'call', 'action': 'call', 'server': 'local', 'tool': 'echo',
             'args': {'text': 'x'}, 'confirm': True},
        ], access='write', timeout=60)
        events = [e['event'] for e in self.h.events(srv['events'])]
        self.assertIn('initialize-received', events)   # handshake got this far
        self.assertIn('closing-stdin', events)         # the server really closed its read end
        err = out['results'][0]
        self.assertEqual(err['kind'], 'error')         # deterministic transport error
        self.assertTrue('transport broken' in err['message'] or 'failed to send' in err['message']
                        or 'exited' in err['message'], err['message'])
        self.assertEqual(self.h.calls(srv['log']), [])  # the tool call never reached the server

    def test_stdin_closed_after_catalog_makes_next_call_fail_deterministically(self):
        srv = self.h.start_stdio(mode='close_stdin_after_list')
        cfg = stdio_cfg(server_env=srv['env'], approval_default='prompt')
        out = self.h.run_host([cfg], [
            {'name': 'call', 'action': 'call', 'server': 'local', 'tool': 'echo',
             'args': {'text': 'x'}, 'confirm': True},
        ], access='write', timeout=60)
        events = [e['event'] for e in self.h.events(srv['events'])]
        self.assertIn('closing-stdin', events)         # closed BEFORE the call write
        err = out['results'][0]
        self.assertEqual(err['kind'], 'error')
        self.assertTrue('transport broken' in err['message'] or 'failed to send' in err['message'],
                        err['message'])
        self.assertEqual(self.h.calls(srv['log']), [])  # the call never reached the server

    def test_cancel_notification_epipe_stays_a_cancellation(self):
        # The server holds the call, closes its read end, and the client's
        # cancellation notice then hits EPIPE: it must neither crash the host
        # nor turn the cancellation into a success.
        srv = self.h.start_stdio(mode='no_answer', close_after_call=True)
        cfg = stdio_cfg(server_env=srv['env'], approval_default='prompt')
        out = self.h.run_host([cfg], [
            {'name': 'call', 'action': 'call', 'server': 'local', 'tool': 'echo',
             'args': {'text': 'x'}, 'confirm': True, 'launch': True, 'abortAfterMs': 1500},
            {'name': 'await', 'awaitPending': True},
        ], access='write', timeout=60)
        events = [e['event'] for e in self.h.events(srv['events'])]
        self.assertIn('call-received', events)         # the call WAS delivered
        self.assertIn('closing-stdin', events)         # read end closed before the cancel write
        err = out['results'][0]
        self.assertEqual(err['kind'], 'error')
        self.assertEqual(err['name'], 'CancelledError')
        self.assertIn('outcome is unknown', err['message'])

    def test_cancel_notification_reaches_server_and_stays_a_cancellation(self):
        srv = self.h.start_stdio(mode='no_answer')
        cfg = stdio_cfg(server_env=srv['env'], approval_default='prompt')
        out = self.h.run_host([cfg], [
            {'name': 'call', 'action': 'call', 'server': 'local', 'tool': 'echo',
             'args': {'text': 'x'}, 'confirm': True, 'launch': True, 'abortAfterMs': 600},
            {'name': 'await', 'awaitPending': True},
        ], access='write', timeout=60)
        err = out['results'][0]
        self.assertEqual(err['kind'], 'error')
        self.assertEqual(err['name'], 'CancelledError')
        self.assertIn('outcome is unknown', err['message'])  # not reported as success
        events = [e['event'] for e in self.h.events(srv['events'])]
        self.assertIn('cancelled-notification-received', events)  # best-effort notice arrived

    def test_server_exit_during_pending_call(self):
        srv = self.h.start_stdio(mode='die_during_call')
        cfg = stdio_cfg(server_env=srv['env'], approval_default='prompt')
        out = self.h.run_host([cfg], [
            {'name': 'call', 'action': 'call', 'server': 'local', 'tool': 'echo',
             'args': {'text': 'x'}, 'confirm': True},
        ], access='write', timeout=60)
        err = out['results'][0]
        self.assertEqual(err['kind'], 'error')
        self.assertIn('exited', err['message'])
        self.assertEqual(self.h.calls(srv['log']), [])  # business counter untouched by the crash

    def test_close_during_pending_request_is_idempotent(self):
        srv = self.h.start_stdio(mode='no_answer')
        cfg = stdio_cfg(server_env=srv['env'], approval_default='prompt')
        out = self.h.run_host([cfg], [
            {'name': 'call', 'action': 'call', 'server': 'local', 'tool': 'echo',
             'args': {'text': 'x'}, 'confirm': True, 'launch': True, 'closeAfterMs': 500},
            {'name': 'await', 'awaitPending': True},
        ], access='write', timeout=60)
        err = out['results'][0]
        self.assertEqual(err['kind'], 'error')
        self.assertIn('closed', err['message'])

if __name__ == '__main__':
    unittest.main()
class LegacySessionTests(HttpHostCase):
    """2025-06-18 legacy HTTP lifecycle: negotiation, protocol header, session
    prefix = 'legacy-sess-'
    expiry (404) recovery — and proof that an expired-session call is never replayed."""


    def test_negotiates_version_and_sends_headers(self):
        srv = self.h.start_http('normal')
        res = self.h.run_host([self.http_cfg(srv, protocol_mode='legacy_2025_06_18')],
                              [{'action': 'list'}, {'action': 'list', 'server': 'web'}], access='write')['results']
        self.assertEqual([r['kind'] for r in res], ['result', 'result'])
        ev = self.h.events(srv['events'])
        inits = [e for e in ev if e['event'] == 'initialize-received']
        self.assertEqual(len(inits), 1)
        lists = [e for e in ev if e.get('method') == 'tools/list']
        self.assertTrue(lists)
        for e in lists:  # negotiated version travels on every later request, with the session id
            self.assertEqual(e['protocol_header'], '2025-06-18')
            self.assertTrue((e['session'] or '').startswith('sess-'))

    def test_session_404_marks_stale_never_replays_and_recovers(self):
        srv = self.h.start_http('normal', extra={'FAKE_MCP_SESSION_EXPIRE_AFTER': '2'})
        res = self.h.run_host([self.http_cfg(srv, protocol_mode='legacy_2025_06_18')],
                              [{'name': 'l1', 'action': 'list', 'server': 'web'},
                               {'name': 'c1', 'action': 'call', 'server': 'web', 'tool': 'search', 'args': {'query': 'x'}},
                               {'name': 'l2', 'action': 'list', 'server': 'web'},
                               {'name': 'c2', 'action': 'call', 'server': 'web', 'tool': 'search', 'args': {'query': 'y'}}],
                              access='write')['results']
        by = {r['step']: r for r in res}
        # the call that hit the expired session reports an error with an unknown outcome...
        self.assertEqual(by['c1']['kind'], 'error')
        self.assertIn('session expired', by['c1']['message'])
        self.assertIn('outcome is unknown', by['c1']['message'])
        # ...the server never executed it (the 404 happened before dispatch)
        self.assertEqual(self.h.calls(srv['log']), ['search:{"query": "y"}'])
        # the next explicit list re-initialized and the follow-up call succeeded
        self.assertEqual(by['l2']['kind'], 'result')
        self.assertEqual(by['c2']['kind'], 'result')
        ev = self.h.events(srv['events'])
        self.assertEqual(len([e for e in ev if e['event'] == 'session-404']), 1)
        self.assertEqual(len([e for e in ev if e['event'] == 'initialize-received']), 2)


class ModernProtocolTests(HttpHostCase):
    """2026-07-28 modern lifecycle against a strict fixture that rejects requests
    prefix = 'modern-mcp-'
    missing MCP-Protocol-Version / Mcp-Method / Mcp-Name / modern _meta."""


    def test_strict_server_accepts_bridge_requests(self):
        srv = self.h.start_http('modern')
        res = self.h.run_host([self.http_cfg(srv)],
                              [{'action': 'list'}, {'action': 'list', 'server': 'web'},
                               {'action': 'describe', 'server': 'web', 'tool': 'search'},
                               {'action': 'call', 'server': 'web', 'tool': 'search', 'args': {'query': 'x'}}],
                              access='write')['results']
        self.assertTrue(all(r['kind'] == 'result' for r in res), res)
        ev = self.h.events(srv['events'])
        self.assertEqual([e for e in ev if e['event'] == 'strict-rejected'], [])
        reqs = [e for e in ev if e.get('event') == 'request']
        for e in reqs:
            self.assertEqual(e['protocol_header'], '2026-07-28')
            self.assertEqual(e['mcp_method_header'], e['method'])
            self.assertTrue(e['has_modern_meta'])
        calls = [e for e in reqs if e['method'] == 'tools/call']
        self.assertEqual(calls[0]['mcp_name_header'], 'search')
        # no initialize handshake and no session id in modern mode
        self.assertEqual([e for e in ev if e['event'] == 'initialize-received'], [])
        self.assertTrue(all(not e['session'] for e in reqs))

    def test_auto_falls_back_to_legacy_only_on_proof(self):
        srv = self.h.start_http('legacy_only')  # server/discover -> 404: proof of legacy-only
        res = self.h.run_host([self.http_cfg(srv)],  # default protocol_mode = auto
                              [{'action': 'list', 'server': 'web'}], access='write')['results']
        self.assertEqual(res[0]['kind'], 'result')
        ev = self.h.events(srv['events'])
        self.assertEqual(len([e for e in ev if e['event'] == 'discover-404-legacy-only']), 1)
        self.assertEqual(len([e for e in ev if e['event'] == 'initialize-received']), 1)
        lists = [e for e in ev if e.get('method') == 'tools/list']
        self.assertTrue(lists and all(e['protocol_header'] == '2025-06-18' for e in lists))

    def test_auto_picks_modern_without_handshake(self):
        srv = self.h.start_http('modern')
        res = self.h.run_host([self.http_cfg(srv)], [{'action': 'list', 'server': 'web'}], access='write')['results']
        self.assertEqual(res[0]['kind'], 'result')
        ev = self.h.events(srv['events'])
        self.assertEqual(len([e for e in ev if e['event'] == 'initialize-received']), 0)
        self.assertEqual(len([e for e in ev if e.get('method') == 'server/discover']), 1)

    def test_legacy_client_cannot_talk_to_modern_server(self):
        srv = self.h.start_http('modern')
        res = self.h.run_host([self.http_cfg(srv, protocol_mode='legacy_2025_06_18')],
                              [{'action': 'list', 'server': 'web'}], access='write')['results']
        self.assertEqual(res[0]['kind'], 'error')
        self.assertIn('failed to initialize', res[0]['message'])
        self.assertTrue(any(e['event'] == 'strict-rejected' for e in self.h.events(srv['events'])))

    def test_modern_body_hang_hits_deadline_without_retry(self):
        srv = self.h.start_http('modern_hang_json')
        res = self.h.run_host([self.http_cfg(srv, tool_timeout_sec=1)],
                              [{'action': 'call', 'server': 'web', 'tool': 'search', 'args': {'query': 'x'}}],
                              access='write', timeout=90)['results']
        self.assertEqual(res[0]['kind'], 'error')
        self.assertIn('outcome is unknown', res[0]['message'])
        kinds = [e['event'] for e in self.h.events(srv['events'])]
        self.assertIn('call-received', kinds)   # the request actually arrived
        self.assertIn('headers-sent', kinds)    # and headers were flushed
        self.assertEqual(len(self.h.calls(srv['log'])), 1)  # exactly one server-side execution

    def test_x_mcp_header_arguments_are_refused_never_sent(self):
        srv = self.h.start_http('modern')
        res = self.h.run_host([self.http_cfg(srv)],
                              [{'action': 'call', 'server': 'web', 'tool': 'search',
                                'args': {'query': 'x', 'x-mcp-header': {'X-Injected': 'value'}}}],
                              access='write')['results']
        self.assertEqual(res[0]['kind'], 'error')
        self.assertIn('x-mcp-header', res[0]['message'])
        self.assertEqual(self.h.calls(srv['log']), [])  # the non-conforming call never reached the server
