"""Agent selectors, automatic result delivery and old-ledger migration boundaries."""
import asyncio
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from subagent_pi.client import request
from subagent_pi.common import AgentError
from subagent_pi.store import Store
from test_transport import McpHarness


class Operations(McpHarness, unittest.IsolatedAsyncioTestCase):
    async def cli(self,*args):
        proc=await asyncio.create_subprocess_exec(sys.executable,str(ROOT/'bin/subagent-pi'),
            '--home',str(self.home),*args,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        out,err=await asyncio.wait_for(proc.communicate(),8)
        self.assertEqual(proc.returncode,0,err.decode())
        return json.loads(out)

    async def test_named_selectors_delivery_and_cli_history(self):
        await self.initialize()
        catalog=(await self.rpc('tools/list'))['result']['tools']
        self.assertEqual(len(catalog),9)
        self.assertNotIn('pi_ack_result',[t['name'] for t in catalog])
        a=await self.tool('pi_spawn_agent',{'cwd':str(self.workspace),'task':'BIG',
            'name':'review-alpha','access':'read','request_id':'first'})
        page=await self.tool('pi_wait_agent',{'agent_ids':['review-alpha']})
        self.assertEqual(page['runs'][0]['id'],a['run_id'])
        self.assertTrue(page['runs'][0]['result']['has_more'])
        self.assertEqual((await self.tool('pi_list_agents',{}))['outstanding']['total'],0)
        first=await self.tool('pi_agent_result',{'agent_id':'review-alpha','max_bytes':256})
        self.assertNotIn('acknowledged',first)
        b=await self.tool('pi_followup_task',{'agent_id':'review-alpha','message':'second',
            'request_id':'second'})
        await self.cli('wait','--agents','review-alpha','--scope',a['scope'])
        latest=await self.cli('result','--agent','review-alpha','--scope',a['scope'])
        self.assertEqual(latest['run']['id'],b['run_id'])
        old=await self.tool('pi_agent_result',{'run_id':first['run']['id'],'offset':first['next_offset']})
        self.assertEqual(old['result_sha256'],first['result_sha256'])
        listing=await self.cli('list','--scope',a['scope'],'--query','REVIEW-ALPHA','--sort','created')
        self.assertEqual((listing['total'],listing['matched'],listing['has_more']),(1,1,False))
        self.assertTrue(listing['agents'][0]['updated_at'].endswith('Z'))
        for args,code in (({},'invalid_argument'),
                          ({'run_id':a['run_id'],'agent_id':'review-alpha'},'invalid_argument'),
                          ({'agent_id':'review-alpha','offset':first['next_offset']},'invalid_offset')):
            value=self.unpack(await self.rpc('tools/call',{'name':'pi_agent_result','arguments':args}))
            self.assertEqual(value['error']['code'],code)
        value=self.unpack(await self.rpc('tools/call',{'name':'pi_wait_agent',
            'arguments':{'run_ids':[a['run_id']],'agent_ids':['review-alpha']}}))
        self.assertEqual(value['error']['code'],'invalid_argument')
        with self.assertRaises(AgentError) as retired:
            await request(self.home,'ack',{'scope':a['scope'],'run_id':a['run_id']})
        self.assertEqual(retired.exception.code,'unknown_operation')
        for op in ('pi_watch','pi_view','pi_claim','pi_observe','pi_release','pi_uncertain','pi_detach'):
            with self.subTest(op=op), self.assertRaises(AgentError) as native:
                await request(self.home,op,{'scope':a['scope']})
            self.assertEqual(native.exception.code,'unknown_operation')

    async def test_no_output_receipt_and_status_reads_preserve_attention(self):
        sid=await self.open_scope(); run=await self.spawn(sid,'short')
        await request(self.home,'wait',{'scope':sid,'run_ids':[run['run_id']]})
        for op,extra in (('list',{}),('inspect',{'agent_id':run['agent_id']}),
                         ('result',{'run_id':run['run_id']})):
            await request(self.home,op,{'scope':sid,**extra})
            status=await request(self.home,'list',{'scope':sid})
            self.assertEqual(status['outstanding']['total'],1)
        def failed(_): raise BrokenPipeError('output closed')
        async def output(value): failed(value)
        with self.assertRaises(BrokenPipeError):
            await request(self.home,'result',{'scope':sid,'run_id':run['run_id']},on_result=output)
        self.assertEqual((await request(self.home,'list',{'scope':sid}))['outstanding']['total'],1)
        ready=await self.cli('wait','--scope',sid)
        self.assertEqual(ready['runs'][0]['id'],run['run_id'])
        empty=await self.cli('wait','--scope',sid,'--timeout-seconds','0')
        self.assertEqual((empty['reason'],empty['runs']),('empty',[]))


class DeliveryMigration(unittest.TestCase):
    def test_schema6_preserves_old_ack_wait_delivery_and_queued_recall(self):
        with tempfile.TemporaryDirectory(prefix='delivery-migration-') as tmp:
            home=Path(tmp); store=Store(home)
            store.execute("INSERT INTO scopes(id,cwd,label,created) VALUES('scope_old',?,'old',1)",(tmp,))
            store.execute("INSERT INTO agents(id,scope,name,cwd,state,session_file,launch,created,updated) "
                "VALUES('pi_old','scope_old','old',?,'closed','old.jsonl','{}',1,1)",(tmp,))
            ids=('run_acked','run_unread','run_waited','run_queued')
            hashes={}
            for rid in ids:
                store.execute("INSERT INTO runs(id,agent_id,scope,state,task,created) "
                    "VALUES(?,'pi_old','scope_old','running','old',1)",(rid,))
                store.finish(rid,'completed',rid)
                hashes[rid]=store.run('scope_old',rid)['result_sha']
            store.close()
            with sqlite3.connect(home/'registry.sqlite') as db:
                db.execute('ALTER TABLE runs ADD COLUMN ack INTEGER NOT NULL DEFAULT 0')
                db.execute("UPDATE meta SET value='6' WHERE key='schema'")
                db.execute("UPDATE runs SET ack=1 WHERE id IN ('run_acked','run_queued')")
                db.execute("UPDATE parent_notifications SET handled=1,state='observed' WHERE run_id='run_waited'")
                db.execute("UPDATE parent_notifications SET state='queued',queued_id='exact_receipt' WHERE run_id='run_queued'")
                # Older unbound scopes did not have notification rows.
                db.execute("DELETE FROM parent_notifications WHERE run_id='run_unread'")
            store=Store(home)
            try:
                self.assertNotIn('ack',[c['name'] for c in store.all('PRAGMA table_info(runs)')])
                self.assertEqual(store.one("SELECT value FROM meta WHERE key='schema'")['value'],'7')
                for rid in ids:
                    row=store.one("SELECT handled,state,queued_id FROM parent_notifications WHERE run_id=?",(rid,))
                    self.assertEqual(row['handled'],int(rid!='run_unread'))
                    self.assertEqual(store.run('scope_old',rid)['result_sha'],hashes[rid])
                    self.assertEqual((home/'results'/f'{rid}.txt').read_text(),rid)
                self.assertEqual(store.one("SELECT state,queued_id FROM parent_notifications WHERE run_id='run_queued'"),
                    {'state':'queued','queued_id':'exact_receipt'})
            finally: store.close()
