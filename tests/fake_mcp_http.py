#!/usr/bin/env python3
"""Local streamable-HTTP MCP server for bridge tests. Binds 127.0.0.1 only.

Stage evidence goes to FAKE_MCP_HTTP_EVENTS (JSON lines), so tests can prove a
tools/call was RECEIVED and headers were FLUSHED before asserting deadline,
cancel or close behavior on the body phase. `hang_body_json` and
`hang_body_sse` deliberately keep the connection open after the headers, which
is exactly the phase the bridge's exchange lifecycle must terminate.
"""
from __future__ import annotations
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODE = os.environ.get('FAKE_MCP_HTTP_MODE', 'normal')
# normal|headers_then_hang|slow_json|bad_status|hang_body_json|hang_body_sse
EVENTS = os.environ.get('FAKE_MCP_HTTP_EVENTS')
CALL_LOG = os.environ.get('FAKE_MCP_HTTP_CALL_LOG')

def event(kind, **kw):
    if EVENTS:
        with open(EVENTS, 'a') as f:
            f.write(json.dumps({'event': kind, 't': time.time(), **kw}) + '\n')

def log_call(name):
    if CALL_LOG:
        with open(CALL_LOG, 'a') as f:
            f.write(name + '\n')

TOOLS = [
    {"name": "search", "description": "Search things",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"],
                     "additionalProperties": False},
     "annotations": {"readOnlyHint": True}},
    {"name": "publish", "description": "Publish content",
     "inputSchema": {"type": "object", "properties": {"body": {"type": "string"}}, "required": ["body"]},
     "annotations": {"readOnlyHint": False}},
]

class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def log_message(self, *a): pass

    def _flush_headers(self, ctype, length=None, session=True):
        self.send_response(200)
        self.send_header('content-type', ctype)
        if session:
            self.send_header('mcp-session-id', 'sess-123')
        if length is not None:
            self.send_header('content-length', str(length))
        self.end_headers()
        try:
            self.wfile.flush()
        except OSError:
            pass

    def do_POST(self):
        length = int(self.headers.get('content-length', 0))
        body = self.rfile.read(length)
        try:
            req = json.loads(body)
        except ValueError:
            self.send_response(400); self.send_header('content-length', '0'); self.end_headers(); return
        rid, method = req.get('id'), req.get('method')
        if MODE == 'bad_status':
            self.send_response(503); self.send_header('content-length', '0'); self.end_headers(); return

        def reply(payload, sse=False):
            encoded = json.dumps(payload).encode()
            data = (b'data: ' + encoded + b'\n\n') if sse else encoded
            self._flush_headers('text/event-stream' if sse else 'application/json', len(data))
            self.wfile.write(data)
            try:
                self.wfile.flush()
            except OSError:
                pass

        if method == 'initialize':
            event('initialize-received')
            reply({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": "2025-06-18",
                   "capabilities": {"tools": {}}, "serverInfo": {"name": "fake-http", "version": "1.0"}}})
        elif method == 'tools/list':
            reply({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}, sse=True)
        elif method == 'tools/call':
            name = (req.get('params') or {}).get('name')
            args = (req.get('params') or {}).get('arguments') or {}
            log_call(f"{name}:{json.dumps(args, sort_keys=True)}")
            event('call-received', tool=name)
            if MODE == 'headers_then_hang':
                # Legacy mode kept for older tests: headers go out, no body ever.
                self._flush_headers('application/json', 999999)
                event('headers-sent')
                time.sleep(30)
                return
            if MODE == 'slow_json':
                time.sleep(5)
                reply({"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": "late"}]}})
                return
            result = {"jsonrpc": "2.0", "id": rid,
                      "result": {"content": [{"type": "text", "text": f"handled {name} {args.get('body') or args.get('query') or ''}"}]}}
            if MODE == 'hang_body_json':
                encoded = json.dumps(result).encode()
                self._flush_headers('application/json', len(encoded))
                event('headers-sent')
                self.wfile.write(encoded[:len(encoded) // 2])  # one fragment, then hold the rest forever
                try:
                    self.wfile.flush()
                except OSError:
                    pass
                event('body-fragment-sent')
                time.sleep(30)
                return
            if MODE == 'hang_body_sse':
                self._flush_headers('text/event-stream')  # no content-length: stream stays open
                event('headers-sent')
                self.wfile.write(b': keepalive\n\n')  # non-terminal event
                try:
                    self.wfile.flush()
                except OSError:
                    pass
                event('body-fragment-sent')
                time.sleep(30)
                return
            reply(result)
        elif rid is not None:
            reply({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "not implemented"}})
        else:
            self.send_response(202); self.send_header('content-length', '0'); self.end_headers()

if __name__ == '__main__':
    server = ThreadingHTTPServer(('127.0.0.1', int(sys.argv[1])), Handler)
    print('ready', flush=True)
    server.serve_forever()
