#!/usr/bin/env node
/**
 * Minimal Pi-host fixture that loads the REAL codex-mcp-bridge.ts through jiti
 * (same loader Pi uses) and drives its registered tool directly. No Pi process,
 * no model call, no disk cache. Used by tests/test_bridge_host.py for layer-4
 * evidence: real stdio/HTTP transports against local fake MCP servers.
 *
 * Usage: node bridge_host.mjs <payload.json> <actions.json> <result.json>
 * payload.json = bootstrap payload (agent/source/mcp)
 * actions.json = [{action,server,tool,args,confirm,abortAfterMs}]
 */
import { openSync, readFileSync, writeSync, mkdtempSync } from 'node:fs';
import { createRequire } from 'node:module';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const PI_DIR = process.env.PI_CODING_AGENT_DIR;
if (!PI_DIR) throw new Error('PI_CODING_AGENT_DIR not set');
const require_ = createRequire(import.meta.url);
const { createJiti } = require_(join(PI_DIR, 'node_modules', 'jiti', 'lib', 'jiti.mjs'));

const [payloadPath, actionsPath, resultPath] = process.argv.slice(2);
const payload = JSON.parse(readFileSync(payloadPath, 'utf8'));
const actions = JSON.parse(readFileSync(actionsPath, 'utf8'));

// The bridge reads PI_AGENTS_BOOTSTRAP_FD to EOF, so a regular read-only fd of
// the payload file exercises the identical read/parse path. Receipt fd is a
// temp file the bridge writes and closes.
const dir = mkdtempSync(join(tmpdir(), 'bridge-host-'));
const payloadFile = join(dir, 'payload.json');
writeSync(openSync(payloadFile, 'w'), JSON.stringify(payload));
const bootstrapFd = openSync(payloadFile, 'r');
const receiptPath = join(dir, 'receipt.json');
const receiptFd = openSync(receiptPath, 'wx', 0o600);
process.env.PI_AGENTS_BOOTSTRAP_FD = String(bootstrapFd);
process.env.PI_AGENTS_BRIDGE_RECEIPT_FD = String(receiptFd);

const jiti = createJiti(import.meta.url, { alias: { typebox: join(PI_DIR, 'node_modules', 'typebox', 'build', 'index.mjs') } });
const bridgePath = join(process.env.BRIDGE_TS, 'codex-mcp-bridge.ts');
const mod = await jiti.import(bridgePath);
const factory = mod.default ?? mod;
const handlers = {};
let registered = null;
const piStub = {
  registerTool: (def) => { registered = def; },
  on: (event, cb) => { (handlers[event] ||= []).push(cb); },
};
await factory(piStub); // Pi awaits the extension factory before continuing
// The bridge closed the bootstrap fd itself after reading it to EOF.

// The bridge factory awaits required-server initialization before writing the
// receipt, so by the time the factory resolved, the receipt file is written.
let receipt = null;
try { receipt = JSON.parse(readFileSync(receiptPath, 'utf8')); } catch { /* missing receipt is a failure signal */ }

const results = [];
for (const step of actions) {
  const signal = new AbortController();
  let timer = null;
  if (step.abortAfterMs) timer = setTimeout(() => signal.abort(new Error('host abort')), step.abortAfterMs);
  try {
    const ctx = { ui: { confirm: async () => step.confirm === true } };
    const out = await registered.execute('call-1', {
      action: step.action, server: step.server, tool: step.tool, args: step.args ?? {},
    }, signal.signal, null, ctx);
    results.push({ kind: 'result', step: step.name ?? step.action, text: out?.content?.[0]?.text ?? null, details: out?.details ?? {} });
  } catch (err) {
    results.push({ kind: 'error', step: step.name ?? step.action, name: err.name, message: String(err.message).slice(0, 500) });
  } finally {
    if (timer) clearTimeout(timer);
  }
}

writeSync(openSync(resultPath, 'w'), JSON.stringify({
  registered: registered ? { name: registered.name, parameters: registered.parameters } : null,
  receipt,
  results,
}));
process.exit(0);
