#!/usr/bin/env python3
"""Local streamable-HTTP MCP server for bridge host tests. Binds 127.0.0.1 only."""
from __future__ import annotations
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODE = os.environ.get('FAKE_MCP_HTTP_MODE', 'normal')  # normal|headers_then_hang|slow_json|bad_status
CALL_LOG = os.environ.get('FAKE_MCP_HTTP_CALL_LOG')

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
            body = json.dumps(payload).encode()
            data = (b'data: ' + body + b'\n\n') if sse else body
            self.send_response(200)
            self.send_header('content-type', 'text/event-stream' if sse else 'application/json')
            self.send_header('mcp-session-id', 'sess-123')
            self.send_header('content-length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            self.wfile.flush()
        if method == 'initialize':
            reply({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": "2025-06-18",
                   "capabilities": {"tools": {}}, "serverInfo": {"name": "fake-http", "version": "1.0"}}})
        elif method == 'tools/list':
            reply({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}, sse=True)
        elif method == 'tools/call':
            name = (req.get('params') or {}).get('name')
            args = (req.get('params') or {}).get('arguments') or {}
            log_call(f"{name}:{json.dumps(args, sort_keys=True)}")
            if MODE == 'headers_then_hang':
                # Headers already sent for the initialize case is impossible here;
                # for calls we simply never reply (deadline test).
                return
            if MODE == 'slow_json':
                time.sleep(5)
            result = {"content": [{"type": "text", "text": f"published {args.get('body', '')}"}]}
            reply({"jsonrpc": "2.0", "id": rid, "result": result})
        elif rid is not None:
            reply({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "not implemented"}})
        else:
            self.send_response(202); self.send_header('content-length', '0'); self.end_headers()

if __name__ == '__main__':
    server = ThreadingHTTPServer(('127.0.0.1', int(sys.argv[1])), Handler)
    print('ready', flush=True)
    server.serve_forever()
