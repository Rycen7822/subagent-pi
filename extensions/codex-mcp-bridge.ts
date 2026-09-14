/**
 * Codex MCP bridge for managed subagent-pi children.
 *
 * Loaded ONLY via explicit `--extension` on children booted by the subagent-pi
 * daemon. Receives its whole configuration over an anonymous pipe (fd number in
 * PI_AGENTS_BOOTSTRAP_FD); it reads user Pi config, global config files, or any
 * other source. Nothing is written to disk: metadata lives in process memory,
 * stderr carries a single non-secret readiness marker, and stdout stays reserved
 * for the parent RPC protocol.
 *
 * No third-party dependencies: Node built-ins plus `typebox`, which Pi itself
 * provides to extensions.
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { readSync, closeSync } from "node:fs";
import { spawn, type ChildProcess } from "node:child_process";

interface ServerCfg {
  name: string;
  transport: "stdio" | "http";
  command?: string;
  args?: string[];
  cwd?: string | null;
  env?: Record<string, string>;
  url?: string;
  headers?: Record<string, string>;
  bearer_token?: string | null;
  startup_timeout_sec: number;
  tool_timeout_sec: number;
  required: boolean;
  allowed_tools: string[] | null;
  disabled_tools: string[];
  approval_mode: string;
  auto_approval_tools: string[];
  confirm_all?: boolean;
}
interface Bootstrap {
  v: number;
  agent: { id: string; access: string; generation: number };
  source: { codex_home: string; mode: string };
  mcp: { servers: ServerCfg[] };
}

const MAX_PAYLOAD = 8 * 1024 * 1024;
const MAX_RESULT_TEXT = 256 * 1024;
const BASE_ENV = ["PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR"];

function redactUrl(url: string): string {
  const cut = url.search(/[?#]/);
  return cut >= 0 ? url.slice(0, cut) : url;
}

function readBootstrap(): { payload?: Bootstrap; error?: string } {
  const g = globalThis as Record<string, unknown>;
  if (g.__subagentPiBridgeBootstrap) return g.__subagentPiBridgeBootstrap as { payload?: Bootstrap; error?: string };
  let result: { payload?: Bootstrap; error?: string };
  const raw = process.env.PI_AGENTS_BOOTSTRAP_FD;
  if (!raw || !/^\d+$/.test(raw)) {
    result = { error: "bootstrap channel missing" };
  } else {
    const fd = parseInt(raw, 10);
    try {
      const chunks: Buffer[] = [];
      let total = 0;
      const buf = Buffer.alloc(65536);
      // Read to EOF; the parent writes asynchronously, so this blocks safely.
      for (;;) {
        const n = readSync(fd, buf, 0, buf.length, null);
        if (n === 0) break;
        total += n;
        if (total > MAX_PAYLOAD) throw new Error("bootstrap payload exceeds limit");
        chunks.push(Buffer.from(buf.subarray(0, n)));
      }
      closeSync(fd);
      const parsed = JSON.parse(Buffer.concat(chunks).toString("utf8")) as Bootstrap;
      if (parsed?.v !== 1 || !Array.isArray(parsed?.mcp?.servers)) throw new Error("bootstrap payload malformed");
      result = { payload: parsed };
    } catch (err) {
      try { closeSync(fd); } catch { /* already closed */ }
      result = { error: `bootstrap failed: ${(err as Error).message}` };
    }
  }
  g.__subagentPiBridgeBootstrap = result; // reload-safe: never block on a consumed pipe twice
  return result;
}

interface JsonRpcResponse { id?: number | string | null; result?: unknown; error?: { code: number; message: string } }

abstract class McpConnection {
  toolsCache: { name: string; description?: string; readOnly?: boolean }[] | null = null;
  abstract request(method: string, params: unknown, timeoutSec: number, notification?: boolean): Promise<unknown>;
  abstract close(): void;
  async ensureTools(cfg: ServerCfg): Promise<{ name: string; description?: string; readOnly?: boolean }[]> {
    if (this.toolsCache) return this.toolsCache;
    const collected: { name: string; description?: string; readOnly?: boolean }[] = [];
    let cursor: string | undefined;
    let pages = 0;
    do {
      const result = await this.request("tools/list", cursor ? { cursor } : {}, cfg.startup_timeout_sec) as
        { tools?: { name: string; description?: string; annotations?: { readOnlyHint?: boolean } }[]; nextCursor?: string };
      for (const tool of result?.tools ?? []) {
        collected.push({
          name: tool.name,
          description: tool.description?.slice(0, 200),
          readOnly: tool.annotations?.readOnlyHint === true,
        });
      }
      cursor = result?.nextCursor;
      pages += 1;
    } while (cursor && pages < 20);
    this.toolsCache = collected;
    return collected;
  }
}

class StdioConnection extends McpConnection {
  private proc: ChildProcess | null = null;
  private buffer = "";
  private nextId = 1;
  private pending = new Map<number, { resolve: (v: unknown) => void; reject: (e: Error) => void }>();
  private stderrTail = "";
  exitError: string | null = null;

  constructor(private cfg: ServerCfg) { super(); }

  private ensureProcess(): ChildProcess {
    if (this.proc && this.exitError === null) return this.proc;
    if (this.proc) { this.close(); }
    if (!this.cfg.command) throw new Error("stdio server missing command");
    const env: Record<string, string> = {};
    for (const key of BASE_ENV) if (process.env[key]) env[key] = process.env[key] as string;
    Object.assign(env, this.cfg.env ?? {});
    // Recursion guard: never spawn the management runtime from a managed child.
    const base = this.cfg.command.split("/").pop();
    if (base === "subagent-pi") throw new Error("refusing to start the subagent-pi management server inside a managed child");
    this.exitError = null;
    const child = spawn(this.cfg.command, this.cfg.args ?? [], {
      cwd: this.cfg.cwd || undefined,
      env,
      stdio: ["pipe", "pipe", "pipe"],
    });
    this.proc = child;
    child.stdout?.setEncoding("utf8");
    child.stdout?.on("data", (chunk: string) => this.onData(chunk));
    child.stderr?.setEncoding("utf8");
    child.stderr?.on("data", (chunk: string) => {
      this.stderrTail = (this.stderrTail + chunk).slice(-4096);
    });
    child.on("exit", () => {
      this.exitError = "server process exited";
      const err = new Error("stdio server exited");
      for (const [, entry] of this.pending) entry.reject(err);
      this.pending.clear();
    });
    return child;
  }

  private onData(chunk: string): void {
    this.buffer += chunk;
    let idx: number;
    while ((idx = this.buffer.indexOf("\n")) >= 0) {
      const line = this.buffer.slice(0, idx).trim();
      this.buffer = this.buffer.slice(idx + 1);
      if (!line) continue;
      try {
        const msg = JSON.parse(line) as JsonRpcResponse;
        const id = typeof msg.id === "number" ? msg.id : undefined;
        if (id !== undefined && this.pending.has(id)) {
          const entry = this.pending.get(id) as { resolve: (v: unknown) => void; reject: (e: Error) => void };
          this.pending.delete(id);
          if (msg.error) entry.reject(new Error(`server error ${msg.error.code}: ${msg.error.message.slice(0, 300)}`));
          else entry.resolve(msg.result);
        } else if (msg.id === undefined) {
          const method = (msg as unknown as { method?: string }).method;
          if (method === "notifications/tools/list_changed") this.toolsCache = null; // no model wakeup
        }
      } catch { /* non-JSON stdout line: ignore, never forward */ }
    }
  }

  async request(method: string, params: unknown, timeoutSec: number, notification = false): Promise<unknown> {
    const child = this.ensureProcess();
    if (notification) {
      child.stdin?.write(JSON.stringify({ jsonrpc: "2.0", method, params }) + "\n");
      return null;
    }
    const id = this.nextId++;
    const promise = new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
    });
    child.stdin?.write(JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n");
    const timer = setTimeout(() => {
      if (this.pending.has(id)) {
        this.pending.delete(id);
        reject(new Error(`${method} timed out after ${timeoutSec}s`));
      }
    }, timeoutSec * 1000);
    try {
      return await promise;
    } finally {
      clearTimeout(timer);
    }
  }

  async initialize(): Promise<void> {
    const result = await this.request("initialize", {
      protocolVersion: "2025-06-18",
      capabilities: {},
      clientInfo: { name: "subagent-pi-bridge", version: "0.2.0" },
    }, this.cfg.startup_timeout_sec) as { protocolVersion?: string };
    await this.request("notifications/initialized", {}, this.cfg.startup_timeout_sec, true);
    return;
  }

  async callTool(name: string, args: unknown, timeoutSec: number): Promise<unknown> {
    return await this.request("tools/call", { name, arguments: args ?? {} }, timeoutSec);
  }

  close(): void {
    const child = this.proc;
    this.proc = null;
    if (child) {
      try { child.stdin?.end(); } catch { /* ignore */ }
      try { child.kill("SIGTERM"); } catch { /* ignore */ }
    }
  }
}

class HttpConnection extends McpConnection {
  private sessionId: string | null = null;
  private nextId = 1;

  constructor(private cfg: ServerCfg) { super(); }

  private headers(): Record<string, string> {
    const headers: Record<string, string> = {
      "content-type": "application/json",
      accept: "application/json, text/event-stream",
      ...(this.cfg.headers ?? {}),
    };
    if (this.cfg.bearer_token) headers.authorization = `Bearer ${this.cfg.bearer_token}`;
    if (this.sessionId) headers["mcp-session-id"] = this.sessionId;
    return headers;
  }

  private async post(body: unknown, timeoutSec: number, signal?: AbortSignal): Promise<Response> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutSec * 1000);
    if (signal) signal.addEventListener("abort", () => controller.abort(), { once: true });
    try {
      return await fetch(this.cfg.url as string, {
        method: "POST",
        headers: this.headers(),
        body: JSON.stringify(body),
        signal: controller.signal,
      });
    } finally {
      clearTimeout(timer);
    }
  }

  private async parseSse(response: Response, id: number): Promise<unknown> {
    const reader = response.body?.getReader();
    if (!reader) throw new Error("empty event-stream response");
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx: number;
      while ((idx = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, idx).trim();
        buffer = buffer.slice(idx + 1);
        if (!line.startsWith("data:")) continue;
        const data = line.slice(5).trim();
        if (!data || data === "[DONE]") continue;
        try {
          const msg = JSON.parse(data) as JsonRpcResponse;
          if (msg.id === id) {
            if (msg.error) throw new Error(`server error ${msg.error.code}: ${msg.error.message.slice(0, 300)}`);
            return msg.result;
          }
        } catch (err) {
          if ((err as Error).message.startsWith("server error")) throw err;
        }
      }
    }
    throw new Error("event-stream closed before the JSON-RPC response arrived");
  }

  async request(method: string, params: unknown, timeoutSec: number, notification = false): Promise<unknown> {
    const id = notification ? null : this.nextId++;
    const body = id === null ? { jsonrpc: "2.0", method, params } : { jsonrpc: "2.0", id, method, params };
    const response = await this.post(body, timeoutSec);
    const session = response.headers.get("mcp-session-id");
    if (session) this.sessionId = session;
    if (!response.ok) throw new Error(`HTTP ${response.status} from ${redactUrl(this.cfg.url ?? "")}`);
    if (id === null) return null;
    const contentType = response.headers.get("content-type") ?? "";
    if (contentType.includes("text/event-stream")) return await this.parseSse(response, id);
    const msg = await response.json() as JsonRpcResponse;
    if (msg.error) throw new Error(`server error ${msg.error.code}: ${msg.error.message.slice(0, 300)}`);
    return msg.result;
  }

  async initialize(): Promise<void> {
    await this.request("initialize", {
      protocolVersion: "2025-06-18",
      capabilities: {},
      clientInfo: { name: "subagent-pi-bridge", version: "0.2.0" },
    }, this.cfg.startup_timeout_sec);
    await this.request("notifications/initialized", {}, this.cfg.startup_timeout_sec, true);
  }

  async callTool(name: string, args: unknown, timeoutSec: number): Promise<unknown> {
    return await this.request("tools/call", { name, arguments: args ?? {} }, timeoutSec);
  }

  close(): void { /* stateless; nothing to terminate */ }
}

export default function (pi: ExtensionAPI) {
  const boot = readBootstrap();
  const connections = new Map<string, McpConnection>();
  const servers: ServerCfg[] = boot.payload?.mcp.servers ?? [];

  function toolAllowed(cfg: ServerCfg, name: string): boolean {
    if (cfg.disabled_tools.includes(name)) return false;
    if (cfg.allowed_tools !== null && !cfg.allowed_tools.includes(name)) return false;
    return true;
  }

  function needsConfirmation(cfg: ServerCfg, tool: { readOnly?: boolean } | undefined, name: string): boolean {
    if (cfg.auto_approval_tools.includes(name)) return false;
    if (cfg.confirm_all) return true; // read child without explicit allowlist
    return cfg.approval_mode !== "auto";
  }

  async function ensureConnection(cfg: ServerCfg): Promise<McpConnection> {
    let conn = connections.get(cfg.name);
    if (conn instanceof StdioConnection && conn.exitError) {
      conn.close();
      connections.delete(cfg.name);
      conn = undefined;
    }
    if (!conn) {
      conn = cfg.transport === "http" ? new HttpConnection(cfg) : new StdioConnection(cfg);
      connections.set(cfg.name, conn);
      try {
        await conn.initialize();
      } catch (err) {
        connections.delete(cfg.name);
        conn.close();
        throw new Error(`server ${cfg.name} failed to initialize: ${(err as Error).message}`);
      }
    }
    return conn;
  }

  function describeResult(result: unknown): string {
    const r = result as { content?: { type: string; text?: string }[]; structuredContent?: unknown; isError?: boolean };
    const parts: string[] = [];
    let unsupported = 0;
    for (const item of r?.content ?? []) {
      if (item.type === "text" && typeof item.text === "string") parts.push(item.text);
      else unsupported += 1;
    }
    let text = parts.join("\n");
    if (r?.structuredContent !== undefined) {
      text += (text ? "\n" : "") + "structuredContent: " + JSON.stringify(r.structuredContent).slice(0, MAX_RESULT_TEXT);
    }
    if (unsupported > 0) text += `\n[${unsupported} content item(s) of unsupported non-text type omitted; nothing was written to disk]`;
    if (text.length > MAX_RESULT_TEXT) text = text.slice(0, MAX_RESULT_TEXT) + "\n[output truncated]";
    if (r?.isError) text = "MCP tool reported isError: true\n" + text;
    return text;
  }

  pi.registerTool({
    name: "codex_mcp",
    label: "Codex MCP bridge",
    description:
      "Call tools on Codex MCP servers inherited into this managed child. " +
      "action=list shows configured servers and their tools (metadata only). " +
      "action=call invokes server+tool with an args object. Connections are established " +
      "on demand and kept in memory; nothing is cached on disk.",
    parameters: Type.Object({
      action: Type.Union([Type.Literal("list"), Type.Literal("call")]),
      server: Type.Optional(Type.String({ description: "Server name for action=call" })),
      tool: Type.Optional(Type.String({ description: "Tool name for action=call" })),
      args: Type.Optional(Type.Record(Type.String(), Type.Unknown(), { description: "Tool arguments object" })),
    }),
    async execute(_id, params, signal, _onUpdate, ctx) {
      if (boot.error) {
        return { content: [{ type: "text", text: `Inherited MCP is unavailable in this child: ${boot.error}` }], details: {} };
      }
      const access = boot.payload?.agent.access ?? "write";
      if (params.action === "list") {
        const report: Record<string, unknown>[] = [];
        const queue = [...servers];
        const runOne = async (cfg: ServerCfg) => {
          try {
            const conn = await ensureConnection(cfg);
            const tools = await conn.ensureTools(cfg);
            const visible = tools.filter((t) => toolAllowed(cfg, t.name) && (!cfg.confirm_all || t.readOnly === true));
            report.push({ server: cfg.name, transport: cfg.transport, tools: visible.map((t) => ({ name: t.name, description: t.description })) });
          } catch (err) {
            report.push({ server: cfg.name, transport: cfg.transport, error: (err as Error).message });
          }
        };
        // Bounded concurrency for cold-start discovery.
        const worker = async (): Promise<void> => {
          for (;;) {
            const cfg = queue.shift();
            if (!cfg) return;
            await runOne(cfg);
          }
        };
        await Promise.all(Array.from({ length: Math.min(4, servers.length) }, worker));
        return { content: [{ type: "text", text: JSON.stringify(report, null, 1).slice(0, MAX_RESULT_TEXT) }], details: {} };
      }
      // action === "call"
      const serverName = params.server;
      const toolName = params.tool;
      if (!serverName || !toolName) {
        return { content: [{ type: "text", text: "action=call requires server and tool" }], details: {} };
      }
      const cfg = servers.find((s) => s.name === serverName);
      if (!cfg) return { content: [{ type: "text", text: `Unknown server ${serverName}; use action=list` }], details: {} };
      if (!toolAllowed(cfg, toolName)) {
        return { content: [{ type: "text", text: `Tool ${serverName}.${toolName} is excluded by the inherited server policy` }], details: {} };
      }
      let conn: McpConnection;
      try {
        conn = await ensureConnection(cfg);
      } catch (err) {
        return { content: [{ type: "text", text: (err as Error).message }], details: {} };
      }
      let tools = await conn.ensureTools(cfg);
      const toolMeta = tools.find((t) => t.name === toolName);
      if (!toolMeta) {
        tools = await conn.ensureTools(cfg); // cache may be stale after list_changed
        const again = tools.find((t) => t.name === toolName);
        if (!again) return { content: [{ type: "text", text: `Tool ${toolName} is not offered by ${serverName}` }], details: {} };
      }
      const fallbackMeta = toolMeta ?? { readOnly: false };
      if (cfg.confirm_all && fallbackMeta.readOnly !== true) {
        return { content: [{ type: "text", text: `Tool ${serverName}.${toolName} is not exposed in a read-only managed child (no explicit allowlist)` }], details: {} };
      }
      if (needsConfirmation(cfg, fallbackMeta, toolName)) {
        const argsPreview = JSON.stringify(params.args ?? {}).slice(0, 500);
        const ok = await ctx.ui.confirm(
          "MCP tool call",
          `Allow inherited MCP call ${serverName}.${toolName}? args: ${argsPreview}`,
        );
        if (!ok) return { content: [{ type: "text", text: "Approval denied; the MCP tool was not called" }], details: {} };
      }
      try {
        const result = await conn.callTool(toolName, params.args ?? {}, cfg.tool_timeout_sec);
        return { content: [{ type: "text", text: describeResult(result) }], details: { server: serverName, tool: toolName } };
      } catch (err) {
        const message = (err as Error).message ?? "call failed";
        if (signal?.aborted) {
          return { content: [{ type: "text", text: `MCP call ${serverName}.${toolName} was cancelled` }], details: {} };
        }
        // Failures are never retried automatically: a tools/call may have side effects.
        return { content: [{ type: "text", text: `MCP call ${serverName}.${toolName} failed: ${message}` }], details: {} };
      }
    },
  });

  if (boot.error) {
    process.stderr.write(`subagent-pi-bridge ready servers=0 error=${boot.error.slice(0, 200)}\n`);
  } else {
    process.stderr.write(`subagent-pi-bridge ready servers=${servers.length}\n`);
  }
}
