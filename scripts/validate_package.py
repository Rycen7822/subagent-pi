#!/usr/bin/env python3
"""Local structural checks. Not a substitute for Codex host installation tests."""
import json
from pathlib import Path
import sys
root=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(root))
from subagent_pi.schema import TOOLS
from subagent_pi import __version__

portable=json.loads((root/'plugin.json').read_text())
legacy=json.loads((root/'.codex-plugin/plugin.json').read_text())
assert portable['name']==legacy['name']=='subagent-pi'
assert portable['version']==legacy['version']==__version__
assert portable['$schema'].endswith('/plugin.schema.json')
assert 'hooks' not in portable.get('extensions',{}).get('com.openai',{})
assert not (root/'hooks').exists()
for filename in ('mcp.json','.mcp.json'):
    servers=json.loads((root/filename).read_text())['mcpServers']
    assert list(servers)==['subagent-pi']
    assert servers['subagent-pi']['command']
    assert servers['subagent-pi']['args'][-1]=='mcp'
assert len((root/'skills/pi-subagents/SKILL.md').read_text().splitlines())<=35
assert len({t['name'] for t in TOOLS})==len(TOOLS)
for t in TOOLS:
    assert t['inputSchema']['additionalProperties'] is False
    assert set(t['inputSchema']['required'])<=set(t['inputSchema']['properties'])
for f in ('getting-started','lifecycle','configuration','recovery','troubleshooting','cli','architecture','testing','sources','inheritance'):
    assert (root/'docs'/f'{f}.md').is_file(),f
assert (root/'extensions/codex-mcp-bridge.ts').is_file()
print(f'Package structure OK; {len(TOOLS)} MCP tools; no hooks. Host/schema-registry validation remains separate.')
