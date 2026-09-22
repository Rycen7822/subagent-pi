"""Bounded read projections: what a client can see about an agent, a run trace and
a terminal result. Nothing here mutates state or acknowledges work."""
from __future__ import annotations
import asyncio
import json
from pathlib import Path
import time

from .common import TERMINAL, DEFAULT_WAIT_MS, AgentError, crop, dumps, identifier, integer

def model_settings(a):
    spec=json.loads(a['launch'])
    return {k:spec[k] for k in ('resolved_model','thinking','available_thinking') if k in spec}

def brief_agent(rt, a):
    w=rt.workers.get(a['id'])
    result={k:a[k] for k in ('id','name','scope','cwd','state','generation','current_run','cleanup')}
    result.update(model_settings(a))
    if w and not w.closed:
        result.update(current_tool=w.current_tool,last_activity=w.last_activity)
        if w.ui: result['pending_input']=list(w.ui.values())[:4]
    return result

def brief_run(r):
    return {k:r[k] for k in ('id','agent_id','state','result_sha','ack','error')}

def outstanding(rt, sid, limit=20):
    rows=rt.store.all("SELECT * FROM runs WHERE scope=? AND ack=0 ORDER BY created DESC LIMIT ?",(sid,limit))
    count=rt.store.one("SELECT COUNT(*) n FROM runs WHERE scope=? AND ack=0",(sid,))['n']
    return {'revision':rt.store.scope(sid)['revision'],'runs':[brief_run(r) for r in rows], 'total':count,'omitted':max(0,count-len(rows))}

def inspect(rt,p):
    a=rt.store.agent(p['scope'],identifier(p.get('agent_id'),'agent_id'))
    limit=integer(p.get('limit',20),'limit',1,100)
    budget=integer(p.get('max_bytes',4096),'max_bytes',1024,16384)
    after=integer(p.get('after',0),'after',0,2**63-1)
    detail=p.get('detail','tools')
    if detail not in {'tools','full'}: raise AgentError('invalid_argument','detail must be tools or full')
    sql='SELECT * FROM events WHERE agent_id=? AND seq>?'
    if detail=='tools': sql+=" AND type!='message'"
    rows=rt.store.all(sql+' ORDER BY seq LIMIT ?',(a['id'],after,limit+1))
    receipts=rt.store.all('SELECT id,run_id,state,updated FROM receipts WHERE agent_id=? ORDER BY created DESC LIMIT 5',(a['id'],))
    result={'agent':brief_agent(rt,a),'events':[],'next_cursor':after,'has_more':False,'receipts':receipts}
    current=a['current_run']
    if current:
        run=rt.store.run(p['scope'],current)
        result['run']={k:run[k] for k in ('id','created','started','ended','deadline')}
    earliest=rt.store.one('SELECT MIN(seq) n FROM events WHERE agent_id=?',(a['id'],))['n']
    result['history_pruned']=bool(after and earliest and after<earliest-1)
    # Grow the page one event at a time and measure the increment, so the byte
    # budget costs one encode per event instead of re-encoding the whole page.
    envelope=len(dumps({**result,'events':[]}).encode())
    for row in rows[:limit]:
        event={k:row[k] for k in ('seq','run_id','generation','type','created')}
        event['data']=json.loads(row['payload'])
        size=len(dumps(event).encode())+(1 if result['events'] else 0)  # + separator
        if envelope+size>budget-100:
            if not result['events']:
                event['data']={'preview':crop(dumps(event['data']),max(80,budget//4)),'truncated':True}
                result['events'].append(event); result['next_cursor']=row['seq']
            result['has_more']=True; break
        envelope+=size
        result['events'].append(event); result['next_cursor']=row['seq']
    if len(rows)>len(result['events']): result['has_more']=True
    if len(dumps(result).encode())>budget:
        result['agent']={k:a[k] for k in ('id','state','generation','current_run')}
        result['receipts']=result['receipts'][:1]
    while len(dumps(result).encode())>budget and result['events']:
        if len(result['events'])==1:
            result['events'][0]['data']={'truncated':True}
            break
        result['events'].pop()
        result['next_cursor']=result['events'][-1]['seq']
        result['has_more']=True
    return result

def result(rt,p):
    r=rt.store.run(p['scope'],identifier(p.get('run_id'),'run_id'))
    if r['state'] not in TERMINAL: raise AgentError('not_terminal','Result is not ready; use wait')
    limit=integer(p.get('max_bytes',4096),'max_bytes',256,16384)
    offset=integer(p.get('offset',0),'offset',0,2**40)
    path=Path(r['result_path'])
    size=path.stat().st_size
    if offset>size: raise AgentError('invalid_offset','Offset is beyond the result file')
    with path.open('rb') as f:
        f.seek(offset); raw=f.read(limit)
    if raw and (raw[0] & 0xC0)==0x80:
        raise AgentError('invalid_offset','Offset must be a UTF-8 boundary returned by this tool')
    # Bytes cursors never split UTF-8 in server-generated pagination.
    if offset+len(raw)<size:
        content=raw.decode('utf-8','ignore'); used=len(content.encode())
    else:
        try: content=raw.decode('utf-8'); used=len(raw)
        except UnicodeDecodeError: raise AgentError('invalid_offset','Offset must be a UTF-8 boundary returned by this tool')
    usage=json.loads(r['usage'])
    return {'run':{**brief_run(r),**{k:r[k] for k in ('created','started','ended','deadline')}},'text':content,'result_sha256':r['result_sha'],
            'offset':offset,'next_offset':offset+used,'has_more':offset+used<size,'total_bytes':size,
            'artifact_path':str(path),'acknowledged':bool(r['ack']),
            'result_truncated':usage.get('result_truncated',False),'usage':usage}

async def wait(rt,p):
    sid=p['scope']; limit=rt.config['max_wait_seconds']*1000
    ms=integer(p.get('timeout_ms',min(DEFAULT_WAIT_MS,limit)),'timeout_ms',0,limit)
    mode=p.get('mode','any')
    if mode not in {'any','all'}: raise AgentError('invalid_argument','mode must be any or all')
    ids=p.get('run_ids')
    if ids is None:
        ids=[r['id'] for r in rt.store.all("SELECT id FROM runs WHERE scope=? AND ack=0 ORDER BY created LIMIT 100",(sid,))]
    if not isinstance(ids,list) or len(ids)>100: raise AgentError('invalid_argument','run_ids must be a list of at most 100 ids')
    ids=list(dict.fromkeys(identifier(x,'run_id') for x in ids))
    until=time.monotonic()+ms/1000
    async with rt.changed:
        while True:
            rows=[rt.store.run(sid,rid) for rid in ids]
            done=[r for r in rows if r['state'] in TERMINAL]
            attention=[r for r in rows if r['state']=='needs_input']
            failures=[r for r in done if r['state']!='completed']
            ready=not ids or bool(attention) or bool(failures) or (bool(done) if mode=='any' else len(done)==len(ids))
            if ready or time.monotonic()>=until:
                # Share a fixed text budget across the page, never 100 full results.
                budget=8192; runs=[]; questions=[]
                for row in rows:
                    item=brief_run(row)
                    if row['state'] in TERMINAL and budget>=256:
                        page=result(rt,{'scope':sid,'run_id':row['id'],'max_bytes':min(2048,budget)})
                        item['result']={k:page[k] for k in ('text','result_sha256','next_offset','has_more','total_bytes','result_truncated')}
                        budget-=max(256,len(page['text'].encode()))
                    elif row['state'] in TERMINAL:
                        item['result']={'result_sha256':row['result_sha'],'next_offset':0,'has_more':True}
                    if row['state']=='needs_input':
                        worker=rt.workers.get(row['agent_id'])
                        if worker:
                            questions.extend({'agent_id':row['agent_id'],'run_id':row['id'],**question}
                                             for question in list(worker.ui.values())[:4])
                    runs.append(item)
                reason='timeout' if not ready else 'needs_input' if attention else 'failed_or_stopped' if failures else 'completed' if done else 'empty' if not ids else 'timeout'
                return {'scope':sid,'timed_out':not ready,'reason':reason,'runs':runs,'questions':questions,
                        'outstanding_revision':rt.store.scope(sid)['revision']}
            try: await asyncio.wait_for(rt.changed.wait(),until-time.monotonic())
            except asyncio.TimeoutError: pass
