"""Dependency-free MCP JSON-RPC stdio subset: initialization, tools, ping, cancellation.

No notifications are advertised as model wakeups. No network transport or sampling.
"""
from __future__ import annotations
import asyncio
import json
import os
import sys
from . import __version__
from .client import request
from .common import MAX_FRAME, AgentError, dumps
from .schema import TOOLS, BY_NAME, validate

VERSIONS=('2025-06-18','2025-03-26','2024-11-05')

async def serve_mcp(home):
    reader=asyncio.StreamReader(limit=MAX_FRAME)
    protocol=asyncio.StreamReaderProtocol(reader)
    transport,_=await asyncio.get_running_loop().connect_read_pipe(lambda:protocol,sys.stdin.buffer)
    tasks={}; output_lock=asyncio.Lock(); initialized=False
    bound_scopes={}
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
                        'instructions':'Use pi_context with the actual workspace cwd, then reuse its scope. Tasks run outside the Codex native subagent runtime. Use wait/list to collect results; notifications do not wake Codex. Keep mutation request_id stable for retries.'}
            elif method=='ping': result={}
            elif not initialized: await error(rid,-32002,'Initialize first'); return
            elif method=='tools/list': result={'tools':[{k:v for k,v in t.items() if k!='_op'} for t in TOOLS]}
            elif method=='tools/call':
                spec=BY_NAME.get(p.get('name'))
                if not spec: await error(rid,-32602,'Unknown tool'); return
                args=p.get('arguments',{})
                validate(args,spec['inputSchema'])
                args=dict(args)
                if spec['_op']=='scope_open' and not args.get('scope'):
                    existing=os.environ.get('PI_AGENTS_SCOPE') or bound_scopes.get(args.get('cwd'))
                    if existing: args['scope']=existing
                timeout=max(45,args.get('timeout_ms',0)/1000+10)
                source=None
                if spec['_op']=='scope_open':
                    # Attach the trusted source snapshot from this Codex-spawned process;
                    # the model never sees or fills these values.
                    from .inheritance import scope_source_snapshot
                    source=scope_source_snapshot(home,dict(os.environ))
                value=await request(home,spec['_op'],args,timeout=timeout,source=source)
                if spec['_op']=='scope_open': bound_scopes[args['cwd']]=value['scope']
                result={'content':[{'type':'text','text':dumps(value)}],'isError':False}
            else: await error(rid,-32601,'Method not found'); return
            await output({'jsonrpc':'2.0','id':rid,'result':result})
        except AgentError as e:
            if method=='tools/call':
                await output({'jsonrpc':'2.0','id':rid,'result':{'content':[{'type':'text','text':dumps({'error':e.as_dict()})}],'isError':True}})
            else: await error(rid,-32602,e.message)
        except asyncio.CancelledError:
            # Cancelling the client wait never cancels Pi. The daemon owns admitted mutations.
            return
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
