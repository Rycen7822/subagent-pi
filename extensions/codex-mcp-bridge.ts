/**
 * Codex MCP bridge for managed subagent-pi children.
 *
 * Loaded ONLY via explicit `--extension` on children booted by the subagent-pi
 * daemon. Receives its whole configuration over an anonymous pipe (fd number in
 * PI_AGENTS_BOOTSTRAP_FD); it never reads user Pi config, global config files,
 * or any other source. Nothing is written to disk: metadata lives in process
 * memory, stderr carries non-secret diagnostics, and stdout stays reserved for
 * the parent RPC protocol. A structured readiness receipt (JSON, no secrets) is
 * written to the fd in PI_AGENTS_BRIDGE_RECEIPT_FD once tools are registered and
 * required servers have initialized — or immediately on failure.
 *
 * No third-party runtime dependencies: Node built-ins plus `typebox`, which Pi
 * itself provides to extensions. The supported MCP lifecycle subset is:
 * initialize (protocol 2025-06-18 handshake), tools/list (pagination),
 * tools/call, notifications/initialized, notifications/cancelled and
 * notifications/tools/list_changed, over newline-delimited stdio JSON-RPC or
 * the streamable-HTTP transport (JSON or SSE responses, mcp-session-id).
 * Anything outside this subset is rejected explicitly instead of half-working.
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { readSync, closeSync, writeSync } from "node:fs";
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
  approval_default: "auto" | "confirm";
  tool_approval: Record<string, "auto" | "confirm">;
  confirm_all?: boolean;
}
interface ToolMeta {
  name: string;
  description?: string;
  readOnly: boolean;
  inputSchema?: unknown;
}
interface Bootstrap {
  v: number;
  agent: { id: string; access: string; generation: number };
  source: { codex_home: string; mode: string };
  mcp: { servers: ServerCfg[] };
}

const MAX_PAYLOAD = 8 * 1024 * 1024;
const MAX_RESULT_TEXT = 256 * 1024;
const MAX_LINE = 1024 * 1024;
const MAX_SCHEMA_BYTES = 64 * 1024;
const MAX_PAGES = 20;
const MAX_TOOLS = 512;
const BASE_ENV = ["PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR"];
const SELF_BINARY = "subagent-pi";
const SELF_MODULE = "subagent_pi";

class CancelledError extends Error {
  constructor(public outcomeUnknown: boolean) {
    super(outcomeUnknown
      ? "call cancelled in flight; server outcome is unknown and the call was not retried"
      : "call cancelled before it was sent");
    this.name = "CancelledError";
  }
}

function redactUrl(url: string): string {
  const cut = url.search(/[?#]/);
  return cut >= 0 ? url.slice(0, cut) : url;
}

interface ReceiptServer { name: string; status: "ready" | "failed" | "lazy"; required: boolean; error?: string }

function writeReceipt(fdEnv: string | undefined, payload: Record<string, unknown>): void {
  if (!fdEnv || !/^\d+$/.test(fdEnv)) return;
  const fd = parseInt(fdEnv, 10);
  try {
    writeSync(fd, JSON.stringify(payload) + "\n");
  } catch { /* parent went away; nothing sensible to do */ }
  try { closeSync(fd); } catch { /* already closed */ }
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
      // Message is capped and contains no payload fragments.
      result = { error: `bootstrap failed: ${((err as Error).message || "unknown error").slice(0, 200)}` };
    }
  }
  g.__subagentPiBridgeBootstrap = result; // reload-safe: never block on a consumed pipe twice
  delete process.env.PI_AGENTS_BOOTSTRAP_FD; // downstream children must not see the channel name
  return result;
}

interface JsonRpcResponse { id?: number | string | null; result?: unknown; error?: { code: number; message: string } }

/** Validates and keeps the FULL tool metadata in process memory (no disk cache). */
function toToolMeta(raw: unknown): ToolMeta | null {
  if (typeof raw !== "object" || raw === null) return null;
  const t = raw as { name?: unknown; description?: unknown; annotations?: { readOnlyHint?: unknown }; inputSchema?: unknown };
  if (typeof t.name !== "string" || !t.name) return null;
  let schema: unknown;
  if (typeof t.inputSchema === "object" && t.inputSchema !== null) {
    const encoded = JSON.stringify(t.inputSchema);
    if (encoded.length <= MAX_SCHEMA_BYTES) schema = t.inputSchema;
  }
  return {
    name: t.name,
    description: typeof t.description === "string" ? t.description.slice(0, 200) : undefined,
    readOnly: t.annotations?.readOnlyHint === true,
    inputSchema: schema,
  };
}

abstract class McpConnection {
  toolsCache: ToolMeta[] | null = null;
  abstract request(method: string, params: unknown, timeoutSec: number, opts?: { notification?: boolean; signal?: AbortSignal }): Promise<unknown>;
  abstract initialize(): Promise<void>;
  abstract callTool(name: string, args: unknown, timeoutSec: number, signal?: AbortSignal): Promise<unknown>;
  abstract close(): void;
  async ensureTools(cfg: ServerCfg, signal?: AbortSignal): Promise<ToolMeta[]> {
    if (this.toolsCache) return this.toolsCache;
    const collected: ToolMeta[] = [];
    let cursor: string | undefined;
    let pages = 0;
    const seenCursors = new Set<string>();
    do {
      const result = await this.request("tools/list", cursor ? { cursor } : {}, cfg.startup_timeout_sec, { signal }) as
        { tools?: unknown[]; nextCursor?: unknown };
      if (typeof result?.nextCursor === "string" && result.nextCursor) {
        if (seenCursors.has(result.nextCursor)) break; // server cursor loop guard
        seenCursors.add(result.nextCursor);
      }
      for (const tool of result?.tools ?? []) {
        if (collected.length >= MAX_TOOLS) break;
        const meta = toToolMeta(tool);
        if (meta) collected.push(meta);
      }
      cursor = typeof result?.nextCursor === "string" && result.nextCursor ? result.nextCursor : undefined;
      pages += 1;
    } while (cursor && pages < MAX_TOOLS && pages < MAX_PAGES && collected.length < MAX_TOOLS);
    this.toolsCache = collected;
    return collected;
  }
}

class StdioConnection extends McpConnection {
  private proc: ChildProcess | null = null;
  private buffer = "";
  private nextId = 1;
  private pending = new Map<number, { resolve: (v: unknown) => void; reject: (e: Error) => void; timer: NodeJS.Timeout }>();
  private stderrTail = "";
  private generation = 0;
  exitError: string | null = null;

  constructor(private cfg: ServerCfg) { super(); }

  private failPending(message: string): void {
    const err = new Error(message);
    for (const [, entry] of this.pending) {
      clearTimeout(entry.timer);
      entry.reject(err);
    }
    this.pending.clear();
  }

  private ensureProcess(): ChildProcess {
    if (this.proc && this.exitError === null) return this.proc;
    if (this.proc) this.close();
    if (!this.cfg.command) throw new Error("stdio server missing command");
    // Recursion guard by execution definition: renaming the server in the
    // config does not bypass this. Covers the direct entrypoint, entry-script
    // args (the installer generates `python <...>/bin/subagent-pi mcp`) and
    // `-m subagent_pi` module forms.
    const args = this.cfg.args ?? [];
    const base = (p: string) => p.split("/").pop();
    const selfReferencing =
      base(this.cfg.command) === SELF_BINARY ||
      args.some((a) => a === "-m" && args[args.indexOf(a) + 1] === SELF_MODULE) ||
      args.some((a) => base(a) === SELF_BINARY) ||
      (base(this.cfg.command) === "python" && args.some((a) => a === SELF_MODULE || a === SELF_BINARY));
    if (selfReferencing) throw new Error("refusing to start the subagent-pi management server inside a managed child");
    const env: Record<string, string> = {};
    for (const key of BASE_ENV) if (process.env[key]) env[key] = process.env[key] as string;
    Object.assign(env, this.cfg.env ?? {});
    this.exitError = null;
    this.generation += 1;
    const myGeneration = this.generation;
    const child = spawn(this.cfg.command, args, {
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
    // Spawn failures and early exits must reject waiters, never crash Pi with
    // an unhandled 'error' event.
    child.on("error", (err: Error) => {
      if (myGeneration !== this.generation) return;
      this.exitError = `server failed to start: ${err.message.slice(0, 200)}`;
      this.failPending(this.exitError);
    });
    child.on("exit", () => {
      if (myGeneration !== this.generation) return; // stale process of an older generation
      this.exitError = "server process exited";
      this.failPending("stdio server exited before responding");
    });
    return child;
  }

  private onData(chunk: string): void {
    this.buffer += chunk;
    if (this.buffer.length > MAX_LINE) {
      this.buffer = "";
      this.failPending("stdio server emitted an oversized unframed line");
      try { this.proc?.kill("SIGKILL"); } catch { /* ignore */ }
      return;
    }
    let idx: number;
    while ((idx = this.buffer.indexOf("\n")) >= 0) {
      const line = this.buffer.slice(0, idx).trim();
      this.buffer = this.buffer.slice(idx + 1);
      if (!line) continue;
      try {
        const msg = JSON.parse(line) as JsonRpcResponse;
        const id = typeof msg.id === "number" ? msg.id : undefined;
        if (id !== undefined && this.pending.has(id)) {
          const entry = this.pending.get(id) as { resolve: (v: unknown) => void; reject: (e: Error) => void; timer: NodeJS.Timeout };
          this.pending.delete(id);
          clearTimeout(entry.timer);
          if (msg.error) entry.reject(new Error(`server error ${msg.error.code}: ${msg.error.message.slice(0, 300)}`));
          else entry.resolve(msg.result);
        } else if (msg.id === undefined) {
          const method = (msg as unknown as { method?: string }).method;
          if (method === "notifications/tools/list_changed") this.toolsCache = null; // no model wakeup
        }
      } catch { /* non-JSON stdout line: ignore, never forward */ }
    }
  }

  async request(method: string, params: unknown, timeoutSec: number,
                opts: { notification?: boolean; signal?: AbortSignal } = {}): Promise<unknown> {
    const child = this.ensureProcess();
    if (opts.signal?.aborted) throw new CancelledError(false);
    if (opts.notification) {
      child.stdin?.write(JSON.stringify({ jsonrpc: "2.0", method, params }) + "\n");
      return null;
    }
    const id = this.nextId++;
    const promise = new Promise<unknown>((resolve, reject) => {
      const timer = setTimeout(() => {
        // Reject through the pending entry so cleanup and double-settlement
        // guards live in exactly one place.
        if (this.pending.has(id)) {
          this.pending.delete(id);
          reject(new Error(`${method} timed out after ${timeoutSec}s`));
        }
      }, timeoutSec * 1000);
      this.pending.set(id, { resolve, reject, timer });
    });
    try {
      child.stdin?.write(JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n");
    } catch (err) {
      const entry = this.pending.get(id);
      if (entry) { this.pending.delete(id); clearTimeout(entry.timer); }
      throw new Error(`failed to send ${method}: ${(err as Error).message.slice(0, 200)}`);
    }
    const onAbort = () => {
      const entry = this.pending.get(id);
      if (entry) {
        this.pending.delete(id);
        clearTimeout(entry.timer);
        entry.reject(new CancelledError(true));
        // Cancellation notice is best-effort; side effects are NOT rolled back.
        try { child.stdin?.write(JSON.stringify({ jsonrpc: "2.0", method: "notifications/cancelled", params: { requestId: id } }) + "\n"); } catch { /* ignore */ }
      }
    };
    if (opts.signal) {
      if (opts.signal.aborted) onAbort();
      else opts.signal.addEventListener("abort", onAbort, { once: true });
    }
    try {
      return await promise;
    } finally {
      const entry = this.pending.get(id);
      if (entry) { this.pending.delete(id); clearTimeout(entry.timer); }
      opts.signal?.removeEventListener("abort", onAbort);
    }
  }

  async initialize(): Promise<void> {
    await this.request("initialize", {
      protocolVersion: "2025-06-18",
      capabilities: {},
      clientInfo: { name: "subagent-pi-bridge", version: "0.2.1" },
    }, this.cfg.startup_timeout_sec);
    await this.request("notifications/initialized", {}, this.cfg.startup_timeout_sec, { notification: true });
  }

  async callTool(name: string, args: unknown, timeoutSec: number, signal?: AbortSignal): Promise<unknown> {
    return await this.request("tools/call", { name, arguments: args ?? {} }, timeoutSec, { signal });
  }

  close(): void {
    const child = this.proc;
    this.proc = null;
    this.failPending("connection closed");
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

  /** Deadline covers the WHOLE exchange — headers, body and result parsing. */
  private async post(body: unknown, timeoutSec: number, signal?: AbortSignal): Promise<Response> {
    if (signal?.aborted) throw new CancelledError(false);
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(new CancelledError(true)), timeoutSec * 1000);
    const onOuterAbort = () => controller.abort(new CancelledError(true));
    if (signal) {
      if (signal.aborted) { clearTimeout(timer); throw new CancelledError(false); }
      signal.addEventListener("abort", onOuterAbort, { once: true });
    }
    try {
      return await fetch(this.cfg.url as string, {
        method: "POST",
        headers: this.headers(),
        body: JSON.stringify(body),
        signal: controller.signal,
      });
    } catch (err) {
      if ((err as Error).name === "AbortError" && signal?.aborted) throw new CancelledError(true);
      if ((err as Error).name === "AbortError") throw new Error(`${redactUrl(this.cfg.url ?? "")} timed out after ${timeoutSec}s`);
      throw new Error(`HTTP request failed: ${(err as Error).message.slice(0, 200)}`);
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", onOuterAbort);
    }
  }

  /** Reads a bounded JSON body with the deadline still armed. */
  private async readBoundedJson(response: Response, limit: number): Promise<unknown> {
    const reader = response.body?.getReader();
    if (!reader) throw new Error("empty response body");
    const chunks: Buffer[] = [];
    let total = 0;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > limit) {
        try { await reader.cancel(); } catch { /* ignore */ }
        throw new Error("response body exceeds limit");
      }
      chunks.push(Buffer.from(value));
    }
    return JSON.parse(Buffer.concat(chunks).toString("utf8"));
  }

  private async parseSse(response: Response, id: number): Promise<unknown> {
    const reader = response.body?.getReader();
    if (!reader) throw new Error("empty event-stream response");
    const decoder = new TextDecoder();
    let buffer = "";
    let total = 0;
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        total += value.byteLength;
        if (total > MAX_RESULT_TEXT) throw new Error("event-stream exceeded limit");
        buffer += decoder.decode(value, { stream: true });
        let idx: number;
        while ((idx = buffer.indexOf("\n")) >= 0) {
          const line = buffer.slice(0, idx).trim();
          buffer = buffer.slice(idx + 1);
          if (!line.startsWith("data:")) continue;
          const data = line.slice(5).trim();
          if (!data || data === "[DONE]") continue;
          let msg: JsonRpcResponse;
          try {
            msg = JSON.parse(data) as JsonRpcResponse;
          } catch { continue; } // keepalive/comment-ish line
          if (msg.id === id) {
            if (msg.error) throw new Error(`server error ${msg.error.code}: ${msg.error.message.slice(0, 300)}`);
            return msg.result;
          }
        }
      }
    } finally {
      try { await reader.cancel(); } catch { /* ignore */ }
    }
    throw new Error("event-stream closed before the JSON-RPC response arrived");
  }

  async request(method: string, params: unknown, timeoutSec: number,
                opts: { notification?: boolean; signal?: AbortSignal } = {}): Promise<unknown> {
    const id = opts.notification ? null : this.nextId++;
    const body = id === null ? { jsonrpc: "2.0", method, params } : { jsonrpc: "2.0", id, method, params };
    const response = await this.post(body, timeoutSec, opts.signal);
    const session = response.headers.get("mcp-session-id");
    if (session) this.sessionId = session;
    if (!response.ok) {
      try { await response.body?.cancel(); } catch { /* ignore */ }
      throw new Error(`HTTP ${response.status} from ${redactUrl(this.cfg.url ?? "")}`);
    }
    if (id === null) {
      try { await response.body?.cancel(); } catch { /* ignore */ }
      return null;
    }
    const contentType = response.headers.get("content-type") ?? "";
    try {
      if (contentType.includes("text/event-stream")) return await this.parseSse(response, id);
      const msg = await this.readBoundedJson(response, MAX_RESULT_TEXT) as JsonRpcResponse;
      if (msg.error) throw new Error(`server error ${msg.error.code}: ${msg.error.message.slice(0, 300)}`);
      return msg.result;
    } catch (err) {
      if ((err as Error).name === "AbortError" && opts.signal?.aborted) throw new CancelledError(true);
      throw err;
    }
  }

  async initialize(): Promise<void> {
    await this.request("initialize", {
      protocolVersion: "2025-06-18",
      capabilities: {},
      clientInfo: { name: "subagent-pi-bridge", version: "0.2.1" },
    }, this.cfg.startup_timeout_sec);
    await this.request("notifications/initialized", {}, this.cfg.startup_timeout_sec, { notification: true });
  }

  async callTool(name: string, args: unknown, timeoutSec: number, signal?: AbortSignal): Promise<unknown> {
    return await this.request("tools/call", { name, arguments: args ?? {} }, timeoutSec, { signal });
  }

  close(): void { /* stateless; active readers are cancelled by their deadlines */ }
}

export default async function (pi: ExtensionAPI) {
  const boot = readBootstrap();
  const connections = new Map<string, McpConnection>();
  const servers: ServerCfg[] = boot.payload?.mcp.servers ?? [];
  const access = boot.payload?.agent.access ?? "write";
  // Defense in depth: a read child without an explicit allowlist ALWAYS
  // confirms and only sees readOnly tools — the bridge derives this itself
  // instead of trusting the payload to have precomputed confirm_all.
  for (const cfg of servers) {
    if (access === "read" && cfg.allowed_tools === null) cfg.confirm_all = true;
  }

  function toolAllowed(cfg: ServerCfg, name: string): boolean {
    if (cfg.disabled_tools.includes(name)) return false;
    if (cfg.allowed_tools !== null && !cfg.allowed_tools.includes(name)) return false;
    return true;
  }

  function toolVisible(cfg: ServerCfg, meta: ToolMeta): boolean {
    if (!toolAllowed(cfg, meta.name)) return false;
    // Read child without an explicit allowlist: only readOnly-advertised tools
    // are visible. readOnlyHint is a server self-report that only affects
    // visibility — it never removes the mandatory confirmation.
    if (cfg.confirm_all && !meta.readOnly) return false;
    return true;
  }

  /** Child confirm_all wins over any parent-side auto (F06). */
  function needsConfirmation(cfg: ServerCfg, tool: ToolMeta): boolean {
    if (cfg.confirm_all) return true;
    return (cfg.tool_approval[tool.name] ?? cfg.approval_default) !== "auto";
  }

  async function ensureConnection(cfg: ServerCfg, signal?: AbortSignal): Promise<McpConnection> {
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
        throw new Error(`server ${cfg.name} failed to initialize: ${(err as Error).message.slice(0, 300)}`);
      }
    }
    return conn;
  }

  /** Release every connection, reader and pending request on shutdown. */
  function closeAll(): void {
    for (const [, conn] of connections) conn.close();
    connections.clear();
  }
  pi.on("session_shutdown", () => closeAll());

  function describeResult(result: unknown): string {
    const r = result as { content?: { type: string; text?: string }[]; structuredContent?: unknown };
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
    return text;
  }

  const parameters = Type.Object({
    action: Type.Union([Type.Literal("list"), Type.Literal("describe"), Type.Literal("call")]),
    server: Type.Optional(Type.String({ description: "Server name (describe/call)" })),
    tool: Type.Optional(Type.String({ description: "Tool name (describe/call)" })),
    args: Type.Optional(Type.Record(Type.String(), Type.Unknown(), { description: "Tool arguments object (call)" })),
  });

  async function execute(_id: string, params: { action: "list" | "describe" | "call"; server?: string; tool?: string; args?: Record<string, unknown> },
                         signal: AbortSignal, _onUpdate: unknown, ctx: { ui: { confirm: (title: string, message: string) => Promise<boolean> } }) {
    if (boot.error) {
      throw new Error(`Inherited MCP is unavailable in this child: ${boot.error}`);
    }
    if (params.action === "list") {
      // Names and configured policy only — cold start does not connect every
      // server. Use describe for a specific server's tools.
      const report = servers.map((cfg) => ({
        server: cfg.name,
        transport: cfg.transport,
        required: cfg.required,
        policy: {
          allowed_tools: cfg.allowed_tools,
          disabled_tools: cfg.disabled_tools,
          approval_default: cfg.approval_default,
          confirm_all: cfg.confirm_all === true,
        },
      }));
      return { content: [{ type: "text", text: JSON.stringify({ servers: report }, null, 1).slice(0, MAX_RESULT_TEXT) }], details: {} };
    }
    const serverName = params.server;
    const toolName = params.tool;
    if (!serverName || !toolName) {
      throw new Error("action=describe|call requires server and tool");
    }
    const cfg = servers.find((s) => s.name === serverName);
    if (!cfg) throw new Error(`Unknown server ${serverName}; use action=list`);
    if (!toolAllowed(cfg, toolName)) {
      throw new Error(`Tool ${serverName}.${toolName} is excluded by the inherited server policy`);
    }
    let conn: McpConnection;
    try {
      conn = await ensureConnection(cfg, signal);
    } catch (err) {
      throw new Error((err as Error).message);
    }
    let tools = await conn.ensureTools(cfg, signal);
    let toolMeta = tools.find((t) => t.name === toolName);
    if (!toolMeta) {
      tools = await conn.ensureTools(cfg, signal); // cache may be stale after list_changed
      toolMeta = tools.find((t) => t.name === toolName);
      if (!toolMeta) throw new Error(`Tool ${toolName} is not offered by ${serverName}`);
    }
    if (params.action === "describe") {
      if (!toolVisible(cfg, toolMeta)) {
        throw new Error(`Tool ${serverName}.${toolName} is not exposed to this child by the inherited policy`);
      }
      if (!toolMeta.inputSchema) {
        throw new Error(`Tool ${serverName}.${toolName} has no usable inputSchema (missing or larger than ${MAX_SCHEMA_BYTES} bytes)`);
      }
      return {
        content: [{ type: "text", text: JSON.stringify({ server: serverName, tool: toolMeta.name, description: toolMeta.description, inputSchema: toolMeta.inputSchema }).slice(0, MAX_RESULT_TEXT) }],
        details: { server: serverName, tool: toolMeta.name, inputSchema: toolMeta.inputSchema },
      };
    }
    // action === "call"
    if (!toolVisible(cfg, toolMeta)) {
      throw new Error(`Tool ${serverName}.${toolName} is not exposed in this read-only managed child (no explicit allowlist)`);
    }
    if (needsConfirmation(cfg, toolMeta)) {
      const argsPreview = JSON.stringify(params.args ?? {}).slice(0, 500);
      const ok = await ctx.ui.confirm(
        "MCP tool call",
        `Allow inherited MCP call ${serverName}.${toolName}? args: ${argsPreview}`,
      );
      if (!ok) {
        // Denial is not an error: nothing was called.
        return { content: [{ type: "text", text: "Approval denied; the MCP tool was not called" }], details: {} };
      }
    }
    if (signal?.aborted) throw new CancelledError(false);
    try {
      const result = await conn.callTool(toolName, params.args ?? {}, cfg.tool_timeout_sec, signal) as { isError?: boolean } | undefined;
      const text = describeResult(result);
      // MCP isError must surface as a real tool error (Pi sets isError on throw).
      if (result?.isError) throw new Error(`MCP tool reported failure: ${text.slice(0, 1000)}`);
      return { content: [{ type: "text", text }], details: { server: serverName, tool: toolName } };
    } catch (err) {
      if ((err as Error).name === "AbortError" || (err as Error).name === "CancelledError") throw new CancelledError(true);
      // Failures are never retried automatically: a tools/call may have had side effects.
      throw new Error(`MCP call ${serverName}.${toolName} failed: ${(err as Error).message.slice(0, 500)}`);
    }
  }

  pi.registerTool({
    name: "codex_mcp",
    label: "Codex MCP bridge",
    description:
      "Call tools on Codex MCP servers inherited into this managed child. " +
      "action=list shows configured servers and policy (no connections). " +
      "action=describe returns a tool's full inputSchema. action=call invokes " +
      "server+tool with an args object. Connections live in memory for this " +
      "worker's lifetime; nothing is cached on disk.",
    parameters,
    execute,
  });

  // Readiness receipt: structured, non-secret, exact. Required servers are
  // initialized eagerly so the daemon knows dependencies work BEFORE any task
  // is sent; optional servers stay lazy.
  const receiptFd = process.env.PI_AGENTS_BRIDGE_RECEIPT_FD;
  delete process.env.PI_AGENTS_BRIDGE_RECEIPT_FD;
  const agent = boot.payload?.agent;
  const receiptServers: ReceiptServer[] = [];
  let failedRequired = false;
  for (const cfg of servers) {
    if (!cfg.required) {
      receiptServers.push({ name: cfg.name, status: "lazy", required: false });
      continue;
    }
    try {
      const conn = await ensureConnection(cfg);
      await conn.ensureTools(cfg);
      receiptServers.push({ name: cfg.name, status: "ready", required: true });
    } catch (err) {
      receiptServers.push({ name: cfg.name, status: "failed", required: true, error: (err as Error).message.slice(0, 200) });
      failedRequired = true;
    }
  }
  if (failedRequired) closeAll();
  writeReceipt(receiptFd, {
    kind: "subagent-pi-bridge-receipt",
    v: 1,
    agent: agent?.id ?? null,
    generation: agent?.generation ?? null,
    state: failedRequired ? "failed" : "ready",
    servers: receiptServers,
  });
  const readyCount = receiptServers.filter((s) => s.status !== "failed").length;
  if (boot.error) {
    process.stderr.write(`subagent-pi-bridge ready servers=0 error=${boot.error.slice(0, 200)}\n`);
  } else {
    process.stderr.write(`subagent-pi-bridge ready servers=${readyCount}\n`);
  }
}
