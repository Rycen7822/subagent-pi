#!/usr/bin/env python3
"""Deterministic stdio MCP server for bridge tests. No network, no models.

Stage-evidence events go to FAKE_MCP_EVENTS (one JSON line per stage) so tests
can prove WHICH stage was reached instead of guessing from error strings.
"""
from __future__ import annotations
import json
import os
import secrets
import sys
import threading
import time

MODE = os.environ.get('FAKE_MCP_MODE', 'normal')  # normal|no_answer|die_after_init|die_during_call|close_stdin_after_init|close_stdin_after_list|close_after_call|slow
CLOSE_AFTER_CALL = os.environ.get('FAKE_MCP_CLOSE_AFTER_CALL') == '1'
CALL_LOG = os.environ.get('FAKE_MCP_CALL_LOG')
EVENTS = os.environ.get('FAKE_MCP_EVENTS')
PAGED = os.environ.get('FAKE_MCP_PAGED') == '1'
MANY = os.environ.get('FAKE_MCP_MANY') == '1'
DYNAMIC = os.environ.get('FAKE_MCP_DYNAMIC') == '1'
MUTATE = os.environ.get('FAKE_MCP_MUTATE') == '1'
DYN_FILE = os.environ.get('FAKE_MCP_DYN_FILE')
TOOLS_HIDDEN = set(os.environ.get('FAKE_MCP_HIDE', '').split(','))

def event(kind, **kw):
    if EVENTS:
        with open(EVENTS, 'a') as f:
            f.write(json.dumps({'event': kind, 't': time.time(), **kw}) + '\n')

def log_call(name):
    if CALL_LOG:
        with open(CALL_LOG, 'a') as f:
            f.write(name + '\n')

def base_tools():
    return [
        {"name": "echo", "description": "Echo the arguments",
         "inputSchema": {"type": "object", "properties": {
             "text": {"type": "string", "description": "text to echo"},
             "count": {"type": "integer", "minimum": 1}},
             "required": ["text"], "additionalProperties": False},
         "annotations": {"readOnlyHint": True}},
        {"name": "delete_file", "description": "Destructive op (counter only; deletes nothing)",
         "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
         "annotations": {"readOnlyHint": False}},
        {"name": "status", "description": "Show status",
         "inputSchema": {"type": "object", "properties": {}},
         "annotations": {"readOnlyHint": True}},
        {"name": "unannounced", "description": "Tool without any annotations",
         "inputSchema": {"type": "object", "properties": {}}},  # no readOnlyHint: treated as write-capable
    ]

# Keep the dynamic name stable across server restarts so multi-host tests see
# one identity; tests still learn it only from the catalog, never from here.
DYN_NAME = ('dyn-' + secrets.token_hex(4))
if DYNAMIC and DYN_FILE:
    if os.path.exists(DYN_FILE):
        DYN_NAME = open(DYN_FILE).read().strip() or DYN_NAME
    else:
        with open(DYN_FILE, 'w') as f:
            f.write(DYN_NAME)
TOOLS = [t for t in base_tools() if t['name'] not in TOOLS_HIDDEN]
if DYNAMIC:
    TOOLS = TOOLS + [{
        "name": DYN_NAME, "description": "Dynamically generated tool",
        "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        "annotations": {"readOnlyHint": True}}]
if MANY:
    TOOLS = TOOLS + [{"name": f"bulk_{i}", "description": "bulk", "inputSchema": {"type": "object", "properties": {}},
                      "annotations": {"readOnlyHint": True}} for i in range(600)]
if MUTATE:
    # First catalog = base set; AFTER a tools/list_changed notification the
    # catalog differs (delete_file removed, transferred added).
    TOOLS_MUTATED = [t for t in TOOLS if t['name'] != 'delete_file']
    TOOLS_MUTATED.append({"name": "transferred", "description": "Added by mutation",
                          "inputSchema": {"type": "object", "properties": {}},
                          "annotations": {"readOnlyHint": True}})

PAGED_FULL = TOOLS
list_count = 0
sent_mutate = False

def reply(req, result):
    print(json.dumps({"jsonrpc": "2.0", "id": req.get('id'), "result": result}), flush=True)

def tools_page(cursor):
    if not PAGED:
        return {"tools": TOOLS}
    page_size = 2
    start = int(cursor) if cursor else 0
    page = PAGED_FULL[start:start + page_size]
    nxt = start + page_size
    out = {"tools": page}
    if nxt < len(PAGED_FULL):
        out["nextCursor"] = str(nxt)
    return out

def handle_call(req, name, args):
    log_call(f"{name}:{json.dumps(args, sort_keys=True)}")
    event('call-received', tool=name)
    if CLOSE_AFTER_CALL:
        # Deterministic EPIPE for the client's CANCEL notification: hold the
        # call, close our read end, then the client's abort-write hits it.
        time.sleep(0.8)
        event('closing-stdin')
        os.close(0)
        for _ in range(300):
            if os.getppid() == 1: break
            time.sleep(0.1)
        sys.exit(0)
    if MODE == 'no_answer':
        return  # never respond -> client deadline
    if MODE == 'slow':
        time.sleep(5)
    if name == 'echo' or name == DYN_NAME:
        text = str(args.get('text', ''))
        count = int(args.get('count', 1))
        if 'text' not in args or not isinstance(args.get('text'), str):
            reply(req, {"content": [{"type": "text", "text": "invalid arguments: text is required"}], "isError": True})
            return
        reply(req, {"content": [{"type": "text", "text": text * count}],
                    "structuredContent": {"echoed": text, "count": count}})
    elif name == 'delete_file':
        reply(req, {"content": [{"type": "text", "text": f"counted delete attempt for {args.get('path')}"}]})
    else:
        reply(req, {"content": [{"type": "text", "text": "ok"}], "isError": False})

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
        event('initialize-received')
        reply(req, {"protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {"listChanged": True}},
                    "serverInfo": {"name": "fake-stdio", "version": "1.0"}})
        if MODE == 'die_after_init':
            sys.exit(3)
        if MODE == 'close_stdin_after_init':
            # The reply is flushed; the client's initialized-notification and
            # tools/list writes happen AFTER our close(0) (the network round
            # trip is slower than this synchronous close), so they hit EPIPE.
            event('closing-stdin')
            os.close(0)  # close OUR read end
            for _ in range(300):  # keep stdout open; exit when the host goes away
                if os.getppid() == 1: break
                time.sleep(0.1)
            sys.exit(0)
    elif method == 'notifications/initialized':
        event('initialized-received')
    elif method == 'tools/list':
        event('tools-list-received', count=list_count + 1)
        page = tools_page(req.get('params', {}).get('cursor'))
        if MODE == 'close_stdin_after_list':
            # Deterministic EPIPE: the catalog answer goes out first, so the
            # client's NEXT write (tools/call) is what hits the closed pipe.
            reply(req, page)
            event('closing-stdin')
            os.close(0)  # close OUR read end
            for _ in range(300):  # keep stdout open; exit when the host goes away
                if os.getppid() == 1: break
                time.sleep(0.1)
            sys.exit(0)
        if MUTATE:
            list_count += 1
            if list_count == 1:
                page = {'tools': TOOLS}  # original catalog
            else:
                page = {'tools': TOOLS_MUTATED}  # after list_changed
        reply(req, page)
        if MUTATE and list_count == 1:
            print(json.dumps({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}), flush=True)
            event('list-changed-sent')
    elif method == 'tools/call':
        params = req.get('params') or {}
        if MODE == 'die_during_call':
            event('call-received-die')
            sys.exit(7)
        handle_call(req, params.get('name'), params.get('arguments') or {})
    elif method == 'notifications/cancelled':
        event('cancelled-notification-received')
    elif rid is not None:
        print(json.dumps({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "not implemented"}}), flush=True)
