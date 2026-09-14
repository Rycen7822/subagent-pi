"""One compact schema source shared by the MCP adapter and IPC validator."""
from .common import AgentError
S={'type':'string'}
ID={'type':'string','minLength':1,'maxLength':128}
SCOPE={'scope':{**ID,'description':'Scope returned by pi_context.'}}
REQ={'request_id':{**ID,'description':'Stable key for this mutation; reuse only for identical retries.'}}
AGENT={**SCOPE,'agent_id':ID}

def obj(properties,required=()):
    return {'type':'object','properties':properties,'required':list(required),'additionalProperties':False}
def tool(name,op,description,properties,required,read=False):
    return {'name':name,'description':description,'inputSchema':obj(properties,required),
            'annotations':{'readOnlyHint':read,'destructiveHint':not read,'idempotentHint':read or 'request_id' in properties,'openWorldHint':not read},'_op':op}
TOOLS=[
 tool('pi_context','scope_open','Open a Pi delegation scope in an explicit workspace, or resume a known scope. Reuse it for subsequent calls.',
      {'cwd':{**S,'description':'Absolute current workspace directory, never the daemon directory.'},'scope':ID,'label':S,
       'inheritance':{'type':'boolean','description':'Explicitly enable or disable Codex skill/MCP inheritance for this scope.'},
       'codex_home':{**S,'description':'Explicit trusted Codex home directory; rebinds this scope as a management action.'}},['cwd']),
 tool('pi_spawn_agent','spawn','Start an asynchronous Pi agent. Returns an agent and run ID. No native Codex /agents integration.',
      {**SCOPE,**REQ,'cwd':S,'task':S,'name':S,'profile':S,'model':S,'access':{'type':'string','enum':['read','write'],'default':'write'},'timeout_seconds':{'type':'integer','minimum':1,'maximum':604800}},
      ['scope','request_id','cwd','task']),
 tool('pi_send_input','send','Send a new task to an idle agent, steer running work, or queue a follow-up. interrupt=true stops before sending a new task. Queued is not consumed.',
      {**AGENT,**REQ,'message':S,'mode':{'type':'string','enum':['send','steer','follow_up'],'default':'steer'},'interrupt':{'type':'boolean','default':False}},
      ['scope','agent_id','request_id','message']),
 tool('pi_wait_agent','wait','Wait for terminal runs or input requests, not ordinary progress. Timeout/cancellation never stops Pi. Omit run_ids to snapshot unacknowledged work.',
      {**SCOPE,'run_ids':{'type':'array','items':ID,'maxItems':100},'mode':{'type':'string','enum':['any','all'],'default':'any'},'timeout_ms':{'type':'integer','minimum':0,'maximum':600000,'default':25000}},['scope'],True),
 tool('pi_list_agents','list','List this scope and outstanding runs, including completed results not yet acknowledged.',
      {**SCOPE,'limit':{'type':'integer','minimum':1,'maximum':50,'default':20}},['scope'],True),
 tool('pi_inspect_agent','inspect','Read a bounded incremental trace and steering receipts. Reuse next_cursor as after. No raw reasoning or unlimited transcript dump.',
      {**AGENT,'after':{'type':'integer','minimum':0},'limit':{'type':'integer','minimum':1,'maximum':100},'max_bytes':{'type':'integer','minimum':1024,'maximum':16384},'detail':{'type':'string','enum':['tools','full'],'default':'tools'}},['scope','agent_id'],True),
 tool('pi_agent_result','result','Read a terminal result in UTF-8 byte pages. Reading never acknowledges the result. Retain result_sha256 for explicit acknowledgement.',
      {**SCOPE,'run_id':ID,'offset':{'type':'integer','minimum':0},'max_bytes':{'type':'integer','minimum':256,'maximum':16384}},['scope','run_id'],True),
 tool('pi_ack_result','ack','Acknowledge an exact run result only after it has been incorporated or explicitly dismissed. Does not delete session or result files.',
      {**SCOPE,**REQ,'run_id':ID,'result_sha256':S},['scope','request_id','run_id','result_sha256']),
 tool('pi_interrupt_agent','interrupt','Cancel queued work and abort the current run. Preserve the session. Fall back to process termination if RPC abort cannot be confirmed.',
      {**AGENT,**REQ},['scope','agent_id','request_id']),
 tool('pi_close_agent','close','Stop work and terminate the owned process group. Preserve durable session and results. Also reaps a verified orphan.',
      {**AGENT,**REQ},['scope','agent_id','request_id']),
 tool('pi_respawn_agent','respawn','Restore the same logical agent from its persisted Pi session only after the previous writer is gone. Never auto-replay interrupted shell commands.',
      {**AGENT,**REQ,'message':S},['scope','agent_id','request_id']),
 tool('pi_answer_agent','answer','Answer a pending Pi extension input request explicitly. Confirmations require a boolean; select/input/editor use text.',
      {**AGENT,**REQ,'ui_request_id':S,'answer':{'anyOf':[{'type':'string'},{'type':'boolean'}]}},['scope','agent_id','request_id','ui_request_id','answer']),
]
BY_NAME={t['name']:t for t in TOOLS}
BY_OP={t['_op']:t for t in TOOLS}

def validate(value,schema,path='arguments'):
    if 'anyOf' in schema:
        for option in schema['anyOf']:
            try: validate(value,option,path); return
            except AgentError: pass
        raise AgentError('invalid_argument',f'{path} has an unsupported type')
    kind=schema.get('type')
    valid={'object':lambda:isinstance(value,dict),'array':lambda:isinstance(value,list),
           'string':lambda:isinstance(value,str),'integer':lambda:isinstance(value,int) and not isinstance(value,bool),
           'boolean':lambda:isinstance(value,bool)}
    if kind in valid and not valid[kind](): raise AgentError('invalid_argument',f'{path} must be {kind}')
    if 'enum' in schema and value not in schema['enum']: raise AgentError('invalid_argument',f'{path} is not an allowed value')
    if kind=='object':
        missing=set(schema.get('required',[]))-set(value)
        if missing: raise AgentError('invalid_argument',f'Missing {path}: {sorted(missing)}')
        unknown=set(value)-set(schema.get('properties',{}))
        if unknown and schema.get('additionalProperties') is False: raise AgentError('invalid_argument',f'Unknown {path}: {sorted(unknown)}')
        for key,item in value.items():
            if key in schema.get('properties',{}): validate(item,schema['properties'][key],path+'.'+key)
    if kind=='array':
        if len(value)>schema.get('maxItems',1000): raise AgentError('invalid_argument',f'{path} has too many items')
        for item in value: validate(item,schema.get('items',{}),path+'[]')
    if kind=='string':
        if len(value)<schema.get('minLength',0) or len(value)>schema.get('maxLength',65536) or '\x00' in value:
            raise AgentError('invalid_argument',f'{path} has invalid length or contains NUL')
    if kind=='integer':
        if value<schema.get('minimum',-2**63) or value>schema.get('maximum',2**63-1): raise AgentError('invalid_argument',f'{path} out of range')

def validate_op(op,p):
    if not isinstance(p,dict): raise AgentError('invalid_argument','params must be an object')
    if op in BY_OP: validate(p,BY_OP[op]['inputSchema'])
    elif op not in {'ping','doctor','scope_list','shutdown'}: raise AgentError('unknown_operation','Unknown operation')
