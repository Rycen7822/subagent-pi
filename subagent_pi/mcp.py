"""Dependency-free MCP JSON-RPC stdio subset: initialize, tools, ping, cancellation."""
from __future__ import annotations
import asyncio
import json
import os
import sys
from . import __version__
from .client import call_timeout, request
from .common import MAX_FRAME, AgentError, dumps
from .schema import TOOLS, BY_NAME, validate, validate_op
from .parent import capture

VERSIONS=('2025-06-18','2025-03-26','2024-11-05')

async def serve_mcp(home):
    reader=asyncio.StreamReader(limit=MAX_FRAME)
    protocol=asyncio.StreamReaderProtocol(reader)
    transport,_=await asyncio.get_running_loop().connect_read_pipe(lambda:protocol,sys.stdin.buffer)
    tasks={}; output_lock=asyncio.Lock(); initialized=False
    bound_scopes={}; active_scopes={}
    async def output(value):
        async with output_lock:
            sys.stdout.write(dumps(value)+'\n'); sys.stdout.flush()
    async def error(rid,code,message):
        await output({'jsonrpc':'2.0','id':rid,'error':{'code':code,'message':message}})
    async def dispatch(msg):
        nonlocal initialized
        rid=msg.get('id'); method=msg.get('method'); p=msg.get('params') or {}
        try:
            if not isinstance(p,dict): await error(rid,-32602,'params must be an object'); return
            if method=='initialize':
                offered=p.get('protocolVersion')
                version=offered if offered in VERSIONS else VERSIONS[0]
                initialized=True
                result={'protocolVersion':version,'capabilities':{'tools':{'listChanged':False}},
                        'serverInfo':{'name':'subagent-pi','version':__version__},
                        'instructions':'Use pi_context with the actual workspace cwd, this connection then reuses its scope and spawn cwd. Tasks run outside the Codex native subagent runtime. Bound Codex parents receive queued attention on completion, failure, stop or questions. Check parent_notifications in pi_context; unbound callers must use wait. Keep mutation request_id stable for retries.'}
            elif method=='ping': result={}
            elif not initialized: await error(rid,-32002,'Initialize first'); return
            elif method=='tools/list': result={'tools':[{k:v for k,v in t.items() if k!='_op'} for t in TOOLS]}
            elif method=='tools/call':
                spec=BY_NAME.get(p.get('name'))
                if not spec: await error(rid,-32602,'Unknown tool'); return
                meta=p.get('_meta') or {}
                caller=meta.get('threadId') if isinstance(meta,dict) else None
                parent=capture(dict(os.environ),caller)
                caller=parent['thread_id'] if parent else None
                args=p.get('arguments',{})
                validate(args,spec['inputSchema'])
                args=dict(args)
                if spec['_op']=='scope_open' and not args.get('scope'):
                    existing=os.environ.get('PI_AGENTS_SCOPE') or bound_scopes.get((caller,args.get('cwd')))
                    if existing: args['scope']=existing
                elif spec['_op']!='scope_open' and not args.get('scope') and caller in active_scopes:
                    args['scope']=active_scopes[caller]
                validate_op(spec['_op'],args)
                timeout=call_timeout(spec['_op'],args,home)
                source={'env':{},'parent':parent} if parent else None
                if spec['_op']=='scope_open':
                    from .inheritance import scope_source_snapshot  # trusted values never pass through the model
                    source=scope_source_snapshot(home,{k:v for k,v in os.environ.items() if k!='CODEX_THREAD_ID'})
                    source['parent']=parent
                async def write_result(value):
                    await output({'jsonrpc':'2.0','id':rid,'result':{'content':[{'type':'text','text':dumps(value)}],'isError':False}})
                if spec['_op']=='wait':
                    await request(home,'wait',args,timeout=timeout,source=source,on_result=write_result)
                    return
                value=await request(home,spec['_op'],args,timeout=timeout,source=source)
                if spec['_op']=='scope_open':
                    active_scopes[caller]=value['scope']; bound_scopes[(caller,args['cwd'])]=value['scope']
                result={'content':[{'type':'text','text':dumps(value)}],'isError':False}
            else: await error(rid,-32601,'Method not found'); return
            await output({'jsonrpc':'2.0','id':rid,'result':result})
        except AgentError as e:
            if method=='tools/call':
                await output({'jsonrpc':'2.0','id':rid,'result':{'content':[{'type':'text','text':dumps({'error':e.as_dict()})}],'isError':True}})
            else: await error(rid,-32602,e.message)
        except asyncio.CancelledError:
            return  # cancelling the client wait never cancels the daemon-owned mutation
        except Exception as e:
            print(f'MCP: {type(e).__name__}: {e}',file=sys.stderr)
            await error(rid,-32603,'Internal error; check the local daemon log')
    try:
        while True:
            try: line=await reader.readline()
            except ValueError: await error(None,-32700,'Frame too large'); break
            if not line: break
            try: msg=json.loads(line)
            except (ValueError,UnicodeError): await error(None,-32700,'Parse error'); continue
            if not isinstance(msg,dict) or msg.get('jsonrpc')!='2.0' or not isinstance(msg.get('method'),str):
                await error(None,-32600,'Invalid request'); continue
            method=msg['method']
            if method=='notifications/cancelled':
                params=msg.get('params') or {}; target=params.get('requestId') if isinstance(params,dict) else None
                if isinstance(target,(str,int)) and target in tasks: tasks[target].cancel()
                continue
            if 'id' not in msg: continue
            rid=msg['id']
            if isinstance(rid,bool) or not isinstance(rid,(int,str)):
                await error(None,-32600,'Request id must be a string or integer'); continue
            if rid in tasks: await error(rid,-32600,'Duplicate in-flight request id'); continue
            t=asyncio.create_task(dispatch(msg)); tasks[rid]=t
            t.add_done_callback(lambda _,key=rid:tasks.pop(key,None))
    finally:
        transport.close()
        for task in list(tasks.values()): task.cancel()
        await asyncio.gather(*list(tasks.values()),return_exceptions=True)
