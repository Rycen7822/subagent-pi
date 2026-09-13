#!/usr/bin/env python3
"""Opt-in real Pi smoke test. Starts one read-only task and MAY USE YOUR PAID MODEL."""
import argparse
import asyncio
from pathlib import Path
import sys
import tempfile
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from subagent_pi.client import request
from subagent_pi.common import state_home

async def run(args):
    home=state_home()
    with tempfile.TemporaryDirectory(prefix='subagent-pi-live-') as temp:
        root=Path(temp); (root/'marker.txt').write_text('subagent-pi-live-ok\n')
        scope=(await request(home,'scope_open',{'cwd':temp,'label':'Real Pi opt-in smoke'}))['scope']
        a=None
        try:
            a=await request(home,'spawn',{'scope':scope,'cwd':temp,'access':'read','task':'Read marker.txt and reply with its exact contents. Do not access other files.',
                            'request_id':'smoke-spawn',**({'model':args.model} if args.model else {})})
            print('Spawned:',a)
            terminal=await request(home,'wait',{'scope':scope,'run_ids':[a['run_id']],'timeout_ms':120000},timeout=130)
            print('Wait:',terminal)
            if terminal['timed_out']: raise RuntimeError('Smoke wait timed out; inspect the preserved run')
            result=await request(home,'result',{'scope':scope,'run_id':a['run_id']})
            print('Result:',result)
            assert result['run']['state']=='completed'
            assert 'subagent-pi-live-ok' in result['text']
            await request(home,'ack',{'scope':scope,'run_id':a['run_id'],'result_sha256':result['result_sha256'],'request_id':'smoke-ack'})
        finally:
            if a:
                print('Close:',await request(home,'close',{'scope':scope,'agent_id':a['agent_id'],'request_id':'smoke-close'}))
        print('Real Pi smoke passed. This verifies Pi RPC, not Codex plugin loading.')

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--allow-model-call',action='store_true',required=True)
p.add_argument('--model')
a=p.parse_args()
asyncio.run(run(a))
