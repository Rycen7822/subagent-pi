from __future__ import annotations
import hashlib
import json
from pathlib import Path
import sqlite3
from .common import AgentError, ACTIVE, TERMINAL, atomic_write, dumps, now, private_dir

class Store:
    def __init__(self, home: Path):
        self.home = home
        private_dir(home)
        self.db = sqlite3.connect(home/"registry.sqlite", isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE IF NOT EXISTS scopes(id TEXT PRIMARY KEY,cwd TEXT NOT NULL,label TEXT NOT NULL,created REAL NOT NULL,revision INTEGER NOT NULL DEFAULT 0,
            codex_home TEXT,codex_source TEXT,inheritance INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS agents(id TEXT PRIMARY KEY,scope TEXT NOT NULL REFERENCES scopes(id),name TEXT NOT NULL,cwd TEXT NOT NULL,state TEXT NOT NULL,generation INTEGER NOT NULL DEFAULT 0,session_file TEXT NOT NULL,launch TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL,current_run TEXT,pid INTEGER,identity TEXT,cleanup TEXT NOT NULL DEFAULT 'verified',UNIQUE(scope,name));
        CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,agent_id TEXT NOT NULL REFERENCES agents(id),scope TEXT NOT NULL REFERENCES scopes(id),state TEXT NOT NULL,task TEXT NOT NULL,created REAL NOT NULL,started REAL,ended REAL,deadline REAL,result_path TEXT,result_sha TEXT,ack INTEGER NOT NULL DEFAULT 0,error TEXT,usage TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE IF NOT EXISTS requests(scope TEXT NOT NULL,key TEXT NOT NULL,digest TEXT NOT NULL,op TEXT NOT NULL,state TEXT NOT NULL,response TEXT,created REAL NOT NULL,PRIMARY KEY(scope,key));
        CREATE TABLE IF NOT EXISTS receipts(id TEXT PRIMARY KEY,agent_id TEXT NOT NULL,run_id TEXT NOT NULL,scope TEXT NOT NULL,message TEXT NOT NULL,state TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,agent_id TEXT NOT NULL,run_id TEXT,generation INTEGER NOT NULL,type TEXT NOT NULL,payload TEXT NOT NULL,created REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS runs_scope_state ON runs(scope,state,ack,created);
        CREATE INDEX IF NOT EXISTS runs_agent_state ON runs(agent_id,state,created);
        CREATE INDEX IF NOT EXISTS events_agent_seq ON events(agent_id,seq);
        CREATE INDEX IF NOT EXISTS receipts_run ON receipts(run_id,state);
        ''')
        v = self.one("SELECT value FROM meta WHERE key='schema'")
        if v and v['value'] == '1':
            # Transactional migration to schema 2: non-secret inheritance source columns.
            self.db.execute('BEGIN IMMEDIATE')
            try:
                self.db.execute('ALTER TABLE scopes ADD COLUMN codex_home TEXT')
                self.db.execute('ALTER TABLE scopes ADD COLUMN codex_source TEXT')
                self.db.execute('ALTER TABLE scopes ADD COLUMN inheritance INTEGER NOT NULL DEFAULT 1')
                self.db.execute("UPDATE meta SET value='2' WHERE key='schema'")
                self.db.execute('COMMIT')
            except BaseException:
                self.db.execute('ROLLBACK'); raise
        elif v and v['value'] != '2': raise AgentError('version_mismatch', 'Unsupported database schema')
        self.db.execute("INSERT OR REPLACE INTO meta VALUES('schema','2')")

    def one(self, sql, args=()):
        r = self.db.execute(sql, args).fetchone()
        return dict(r) if r else None
    def all(self, sql, args=()):
        return [dict(r) for r in self.db.execute(sql, args)]
    def execute(self, sql, args=()): return self.db.execute(sql, args)
    def bump(self, scope): self.execute("UPDATE scopes SET revision=revision+1 WHERE id=?", (scope,))
    def scope(self, sid):
        row = self.one("SELECT * FROM scopes WHERE id=?", (sid,))
        if not row: raise AgentError("scope_not_found", "Unknown scope; use pi_context to open or resume one")
        return row
    def agent(self, sid, aid):
        self.scope(sid)
        row = self.one("SELECT * FROM agents WHERE id=? AND scope=?", (aid,sid))
        if not row: raise AgentError("agent_not_found", "Agent not found in this scope")
        return row
    def run(self, sid, rid):
        self.scope(sid)
        row = self.one("SELECT * FROM runs WHERE id=? AND scope=?", (rid,sid))
        if not row: raise AgentError("run_not_found", "Run not found in this scope")
        return row
    def agent_update(self, aid, **fields):
        fields['updated'] = now()
        self.execute('UPDATE agents SET '+','.join(f'{k}=?' for k in fields)+' WHERE id=?', (*fields.values(),aid))
    def event(self, aid, rid, generation, kind, payload):
        return self.execute("INSERT INTO events(agent_id,run_id,generation,type,payload,created) VALUES(?,?,?,?,?,?)", (aid,rid,generation,kind,dumps(payload),now())).lastrowid
    def finish(self, rid, state, output, error=None, usage=None):
        row = self.one("SELECT * FROM runs WHERE id=?", (rid,))
        if not row or row['state'] in TERMINAL: return
        body = output.encode('utf-8')
        path = self.home/'results'/f'{rid}.txt'
        atomic_write(path, body)  # artifact durable before referencing transaction
        sha = hashlib.sha256(body).hexdigest()
        self.execute("BEGIN IMMEDIATE")
        try:
            self.execute("UPDATE runs SET state=?,ended=?,result_path=?,result_sha=?,error=?,usage=? WHERE id=?", (state,now(),str(path),sha,error,dumps(usage or {}),rid))
            self.execute("UPDATE receipts SET state='not_consumed',updated=? WHERE run_id=? AND state IN ('queued','sending')", (now(),rid))
            self.bump(row['scope'])
            self.execute("COMMIT")
        except BaseException:
            self.execute("ROLLBACK"); raise
    def request_begin(self, sid, key, op, params):
        digest = hashlib.sha256(dumps({'op':op,'params':params}).encode()).hexdigest()
        row = self.one("SELECT * FROM requests WHERE scope=? AND key=?", (sid,key))
        if row:
            if row['digest'] != digest: raise AgentError("idempotency_conflict", "request_id already used for different arguments")
            if row['state'] == 'done': return json.loads(row['response'])
            raise AgentError("request_uncertain", "Operation was started but no durable reply exists; inspect state before retrying", request_id=key)
        self.execute("INSERT INTO requests VALUES(?,?,?,?,?,?,?)", (sid,key,digest,op,'pending',None,now()))
        return None
    def request_end(self, sid, key, response):
        self.execute("UPDATE requests SET state='done',response=? WHERE scope=? AND key=?", (dumps(response),sid,key))
    def close(self): self.db.close()
