#!/usr/bin/env python3
"""Deterministic stdio MCP server for bridge host tests. No network, no models."""
from __future__ import annotations
import json
import os
import sys

MODE = os.environ.get('FAKE_MCP_MODE', 'normal')  # normal|no_answer|die_after_init|slow
CALL_LOG = os.environ.get('FAKE_MCP_CALL_LOG')
TOOLS = [
    {"name": "echo", "description": "Echo the arguments",
     "inputSchema": {"type": "object", "properties": {
         "text": {"type": "string", "description": "text to echo"},
         "count": {"type": "integer", "minimum": 1}},
         "required": ["text"], "additionalProperties": False},
     "annotations": {"readOnlyHint": True}},
    {"name": "delete_file", "description": "Destructive op",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
     "annotations": {"readOnlyHint": False}},
    {"name": "status", "description": "Show status",
     "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"readOnlyHint": True}},
]
TOOLS = [t for t in TOOLS if t['name'] not in os.environ.get('FAKE_MCP_HIDE', '').split(',')]

def log_call(name):
    if CALL_LOG:
        with open(CALL_LOG, 'a') as f:
            f.write(name + '\n')

for raw in sys.stdin:
    line = raw.strip()
    if not line:
        continue
    try:
        req = json.loads(line)
    except ValueError:
        continue
    rid, method = req.get('id'), req.get('method')
    if method == 'initialize':
        print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {"listChanged": True}},
            "serverInfo": {"name": "fake-stdio", "version": "1.0"}}}), flush=True)
        if MODE == 'die_after_init':
            sys.exit(3)
    elif method == 'notifications/initialized':
        pass
    elif method == 'tools/list':
        print(json.dumps({"jsonrpc": "2.0", "id": rid,
                          "result": {"tools": TOOLS}}), flush=True)
    elif method == 'tools/call':
        name = (req.get('params') or {}).get('name')
        args = (req.get('params') or {}).get('arguments') or {}
        log_call(f"{name}:{json.dumps(args, sort_keys=True)}")
        if MODE == 'no_answer':
            continue  # never respond -> client deadline
        if MODE == 'slow':
            import time; time.sleep(5)
        if name == 'echo':
            text = str(args.get('text', ''))
            count = int(args.get('count', 1))
            if not isinstance(args.get('text'), str) or 'text' not in args:
                print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": {
                    "content": [{"type": "text", "text": "invalid arguments: text is required"}],
                    "isError": True}}), flush=True)
                continue
            print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": text * count}],
                "structuredContent": {"echoed": text, "count": count}}}), flush=True)
        elif name == 'delete_file':
            print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": f"deleted {args.get('path')}"}]}}), flush=True)
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": "ok"}], "isError": False}}), flush=True)
    elif method == 'notifications/cancelled':
        # no response to the original call is the realistic outcome
        pass
    elif rid is not None:
        print(json.dumps({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "not implemented"}}), flush=True)
