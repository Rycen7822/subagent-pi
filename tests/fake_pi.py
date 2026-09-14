#!/usr/bin/env python3
"""Deterministic Pi RPC simulator. No models, credentials, network or repository edits."""
from __future__ import annotations
import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

p=argparse.ArgumentParser(add_help=False)
p.add_argument('--hold-eof',action='store_true'); p.add_argument('--session'); p.add_argument('--session-dir'); p.add_argument('--mode'); p.add_argument('--no-clear',action='store_true'); p.add_argument('--ignore-abort',action='store_true')
a,_=p.parse_known_args()
path=Path(a.session) if a.session else Path(a.session_dir)/'test-session.jsonl'; current=None; queue=[]; ui={}; count=0

def read_bootstrap():
    """Emulate the real bridge: consume the anonymous pipe, emit the ready marker."""
    raw=os.environ.get('PI_AGENTS_BOOTSTRAP_FD')
    if not raw or not raw.isdigit(): return None
    fd=int(raw); chunks=[]
    while True:
        try: data=os.read(fd,65536)
        except OSError: break
        if not data: break
        chunks.append(data)
    os.close(fd)
    try:
        payload=json.loads(b''.join(chunks))
        servers=payload.get('mcp',{}).get('servers',[])
        names=','.join(s['name'] for s in servers)
        print(f'subagent-pi-bridge ready servers={len(servers)} names={names}',file=sys.stderr,flush=True)
        for s in servers:
            if 'FAKE_TEST_ECHO' in s.get('env',{}):
                print(f'bridge-env {s["name"]}={s["env"]["FAKE_TEST_ECHO"]}',file=sys.stderr,flush=True)
        return payload
    except Exception:
        print('subagent-pi-bridge ready servers=0 error=malformed',file=sys.stderr,flush=True)
        return None

BRIDGE=read_bootstrap()

def emit(e):
    print(json.dumps(e,ensure_ascii=False),flush=True)

def msg(role,body,**extra):
    m={'role':role,'content':[{'type':'text','text':body}],**extra}
    path.parent.mkdir(parents=True,exist_ok=True)
    if not path.exists(): path.write_text(json.dumps({'type':'session','id':'fake-session','version':3})+'\n')
    with path.open('a') as f: f.write(json.dumps({'type':'message','message':m},ensure_ascii=False)+'\n')
    emit({'type':'message_end','message':m})

def response(r,success=True,data=None,error=None):
    emit({'type':'response','id':r.get('id'),'command':r['type'],'success':success,**({'data':data} if data is not None else {}),**({'error':error} if error else {})})

async def run(task):
    global count
    count+=1
    emit({'type':'agent_start'})
    msg('user',task)
    try:
        if task=='CRASH': os._exit(9)
        if task=='SPAWN_CHILD':
            proc=subprocess.Popen(['sleep','120'])
            emit({'type':'tool_execution_start','toolName':'bash','toolCallId':'sleep','args':{'command':'sleep 120','pid':proc.pid}})
            await asyncio.sleep(120)
        if task=='UI_CONFIRM':
            f=asyncio.get_running_loop().create_future(); ui['ui-1']=f
            emit({'type':'extension_ui_request','id':'ui-1','method':'confirm','title':'Allow this test operation?','message':'Test-only confirmation'})
            accepted=await f; ui.pop('ui-1',None)
            output='confirmed='+str(accepted)
        else:
            delay=.1
            if task.startswith('delay='):
                raw,task=task.split('|',1); delay=float(raw.split('=',1)[1])
            emit({'type':'tool_execution_start','toolName':'read','toolCallId':'read-1','args':{'path':'src/example.py','sample':'界'*5000 if task=='BIG' else ''}})
            await asyncio.sleep(delay/2)
            if task!='NO_CONSUME':
                while queue: msg('user',queue.pop(0))
            emit({'type':'tool_execution_end','toolName':'read','toolCallId':'read-1','isError':False,'result':{'content':[{'type':'text','text':'sample'}]}})
            await asyncio.sleep(delay/2)
            if task!='NO_CONSUME':
                while queue: msg('user',queue.pop(0))
            else: queue.clear()
            output=('汉字🙂\u2028\u2029'*3000) if task=='BIG' else 'Completed: '+task
        msg('assistant',output,stopReason='stop',usage={'input':100,'output':10,'totalTokens':110})
    except asyncio.CancelledError:
        msg('assistant','Partial work before interruption',stopReason='aborted')
    finally:
        emit({'type':'agent_end','messages':[]})

async def main():
    global current
    reader=asyncio.StreamReader(); transport,_=await asyncio.get_running_loop().connect_read_pipe(lambda:asyncio.StreamReaderProtocol(reader),sys.stdin.buffer)
    while line:=await reader.readline():
        r=json.loads(line); kind=r['type']
        if kind=='get_state': response(r,data={'sessionFile':str(path),'sessionId':'fake-session','isStreaming':bool(current and not current.done()),'pendingMessageCount':len(queue),'model':{'id':'fake','provider':'test'}})
        elif kind=='prompt':
            if current and not current.done(): response(r,False,error='Already streaming')
            else: response(r); current=asyncio.create_task(run(r['message']))
        elif kind=='steer': queue.append(r['message']); response(r)
        elif kind=='clear_queue':
            if a.no_clear: response(r,False,error='Unknown command: clear_queue')
            else: old=list(queue); queue.clear(); response(r,data={'steering':old,'followUp':[]})
        elif kind=='abort':
            if not a.ignore_abort and current and not current.done(): current.cancel(); await current
            response(r)
        elif kind=='extension_ui_response':
            f=ui.get(r['id'])
            if f and not f.done(): f.set_result(r.get('confirmed',False))
        else: response(r,False,error='Unknown command')
    if a.hold_eof: await asyncio.sleep(120)
    if current and not current.done(): current.cancel(); await current
    transport.close()

asyncio.run(main())
