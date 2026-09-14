/**
 * Codex MCP bridge for managed subagent-pi children. Loaded ONLY via explicit
 * `--extension` on daemon-booted children. Configuration arrives over an
 * anonymous pipe (fd in PI_AGENTS_BOOTSTRAP_FD); no user/global config is read
 * and nothing is written to disk. A structured readiness receipt (JSON, no
 * secrets) goes to the fd in PI_AGENTS_BRIDGE_RECEIPT_FD once required servers
 * have initialized — or immediately on failure.
 *
 * No third-party runtime deps beyond `typebox`, which Pi provides to extensions.
 * Supported MCP subset: initialize (2025-06-18), tools/list (pagination),
 * tools/call, notifications/initialized, notifications/cancelled and
 * notifications/tools/list_changed, over newline stdio JSON-RPC or streamable
 * HTTP (JSON or SSE, mcp-session-id). Anything else is rejected explicitly.
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
      for (;;) {  // read to EOF; the parent writes asynchronously, so this blocks safely
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
      result = { error: `bootstrap failed: ${((err as Error).message || "unknown error").slice(0, 200)}` };
    }
  }
  g.__subagentPiBridgeBootstrap = result; // reload-safe: never block on a consumed pipe twice
  delete process.env.PI_AGENTS_BOOTSTRAP_FD; // downstream children must not see the channel name
  return result;
}

interface JsonRpcResponse { id?: number | string | null; result?: unknown; error?: { code: number; message: string } }

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
  catalogTruncated = false;
  private catalogEpoch = 0; // bump on list_changed so an in-flight crawl never repopulates an invalidated cache
  protected invalidateCatalog(): void {
    this.toolsCache = null;
    this.catalogEpoch += 1;
  }
  abstract get dead(): boolean;
  abstract request(method: string, params: unknown, timeoutSec: number, opts?: { notification?: boolean; signal?: AbortSignal }): Promise<unknown>;
  abstract initialize(): Promise<void>;
  abstract callTool(name: string, args: unknown, timeoutSec: number, signal?: AbortSignal): Promise<unknown>;
  abstract close(): void;
  async ensureTools(cfg: ServerCfg, signal?: AbortSignal): Promise<ToolMeta[]> {
    // Cached in memory; catalogTruncated records a bound-stopped crawl instead of
    // presenting a partial catalog as complete. Invalidated on list_changed.
    if (this.toolsCache) return this.toolsCache;
    const epochAtStart = this.catalogEpoch;
    const collected: ToolMeta[] = [];
    let cursor: string | undefined;
    let pages = 0;
    this.catalogTruncated = false;
    const seenCursors = new Set<string>();
    do {
      const result = await this.request("tools/list", cursor ? { cursor } : {}, cfg.startup_timeout_sec, { signal }) as
        { tools?: unknown[]; nextCursor?: unknown };
      if (typeof result?.nextCursor === "string" && result.nextCursor) {
        if (seenCursors.has(result.nextCursor)) { this.catalogTruncated = true; break; } // server cursor loop guard
        seenCursors.add(result.nextCursor);
      }
      for (const tool of result?.tools ?? []) {
        if (collected.length >= MAX_TOOLS) { this.catalogTruncated = true; break; }
        const meta = toToolMeta(tool);
        if (meta) collected.push(meta);
      }
      cursor = typeof result?.nextCursor === "string" && result.nextCursor ? result.nextCursor : undefined;
      pages += 1;
    } while (cursor && pages < MAX_PAGES && collected.length < MAX_TOOLS);
    if (cursor && pages >= MAX_PAGES) this.catalogTruncated = true;
    if (this.catalogEpoch === epochAtStart) this.toolsCache = collected;  // a list_changed that arrived mid-crawl must win
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
  private closed = false;
  private stdinBroken = false;
  exitError: string | null = null;

  constructor(private cfg: ServerCfg) { super(); }

  get dead(): boolean {
    return this.closed || this.proc === null || this.exitError !== null;
  }

  private failPending(message: string): void {
    const err = new Error(message);
    for (const [, entry] of this.pending) {
      clearTimeout(entry.timer);
      entry.reject(err);
    }
    this.pending.clear();
  }

  private ensureProcess(): ChildProcess {
    if (this.closed) throw new Error("stdio connection is closed");
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
    this.stdinBroken = false;
    this.generation += 1;
    const myGeneration = this.generation;
    const child = spawn(this.cfg.command, args, {
      cwd: this.cfg.cwd || undefined,
      env,
      stdio: ["pipe", "pipe", "pipe"],
    });
    this.proc = child;
    child.stdout?.setEncoding("utf8");
    child.stdout?.on("data", (chunk: string) => this.onData(chunk, myGeneration));
    child.stderr?.setEncoding("utf8");
    child.stderr?.on("data", (chunk: string) => {
      this.stderrTail = (this.stderrTail + chunk).slice(-4096);
    });
    // The stdin Socket can fail ASYNCHRONOUSLY (server closed its read end; the
    // next write raises EPIPE). ChildProcess 'error' does not cover this and
    // try/catch only sees synchronous failures — this handler settles pending
    // requests deterministically and marks the connection dead for reconnect.
    child.stdin?.on("error", (err: Error) => {
      if (myGeneration !== this.generation || this.closed) return;
      this.stdinBroken = true;
      const code = (err as NodeJS.ErrnoException).code ?? "error";
      this.exitError = this.exitError ?? `stdio transport broken: ${code}`;
      this.failPending(`stdio transport broken (${code}); the call may or may not have reached the server`);
    });
    // Spawn failures and early exits reject waiters; never an unhandled 'error' crash.
    child.on("error", (err: Error) => {
      if (myGeneration !== this.generation || this.closed) return;
      this.exitError = `server failed to start: ${err.message.slice(0, 200)}`;
      this.failPending(this.exitError);
    });
    child.on("exit", () => {
      if (myGeneration !== this.generation || this.closed) return; // stale process of an older generation
      this.exitError = this.exitError ?? "server process exited";
      this.failPending("stdio server exited before responding");
    });
    return child;
  }

  private sendFrame(frame: unknown): void {
    const child = this.ensureProcess();
    if (this.stdinBroken || !child.stdin?.writable) {
      throw new Error("stdio transport is broken; reconnect required");
    }
    child.stdin.write(JSON.stringify(frame) + "\n");
  }

  private onData(chunk: string, generation: number): void {
    if (generation !== this.generation) return; // stale bytes from a replaced process
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
          if (method === "notifications/tools/list_changed") this.invalidateCatalog(); // no model wakeup
        }
      } catch { /* non-JSON stdout line: ignore, never forward */ }
    }
  }

  async request(method: string, params: unknown, timeoutSec: number,
                opts: { notification?: boolean; signal?: AbortSignal } = {}): Promise<unknown> {
    if (opts.signal?.aborted) throw new CancelledError(false);
    this.ensureProcess();
    if (opts.notification) {
      this.sendFrame({ jsonrpc: "2.0", method, params });  // no pending entry; async EPIPE is the stdin listener's job
      return null;
    }
    const id = this.nextId++;
    const promise = new Promise<unknown>((resolve, reject) => {
      const timer = setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id);
          reject(new Error(`${method} timed out after ${timeoutSec}s`));
        }
      }, timeoutSec * 1000);
      this.pending.set(id, { resolve, reject, timer });
    });
    try {
      // settle the pending entry synchronously, or the rejection would hang until timeout
      this.sendFrame({ jsonrpc: "2.0", id, method, params });
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
        try { this.sendFrame({ jsonrpc: "2.0", method: "notifications/cancelled", params: { requestId: id } }); } catch { /* best-effort */ }
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
      clientInfo: { name: "subagent-pi-bridge", version: "0.2.3" },
    }, this.cfg.startup_timeout_sec);
    await this.request("notifications/initialized", {}, this.cfg.startup_timeout_sec, { notification: true });
  }

  async callTool(name: string, args: unknown, timeoutSec: number, signal?: AbortSignal): Promise<unknown> {
    return await this.request("tools/call", { name, arguments: args ?? {} }, timeoutSec, { signal });
  }

  close(): void {
    const child = this.proc;
    this.closed = true;
    this.proc = null;
    this.failPending("connection closed");
    if (child) {
      try { child.stdin?.end(); } catch { /* ignore */ }
      try { child.kill("SIGTERM"); } catch { /* ignore */ }
    }
  }
}

/** Distinguishes why an in-flight HTTP exchange was aborted. */
const ABORT_DEADLINE = Symbol("deadline");
const ABORT_USER = Symbol("user");
const ABORT_CLOSED = Symbol("closed");

class HttpConnection extends McpConnection {
  private sessionId: string | null = null;
  private nextId = 1;
  private closed = false;
  private active = new Set<AbortController>();  // every in-flight exchange, so close() can end them all

  constructor(private cfg: ServerCfg) { super(); }

  get dead(): boolean { return this.closed; }

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

  private async readBoundedJson(response: Response, limit: number): Promise<unknown> {
    const reader = response.body?.getReader();
    if (!reader) throw new Error("empty response body");
    const chunks: Buffer[] = [];
    let total = 0;
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        total += value.byteLength;
        if (total > limit) throw new Error("response body exceeds limit");
        chunks.push(Buffer.from(value));
      }
    } finally {
      try { await reader.cancel(); } catch { /* ignore */ }
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
    // One exchange, one AbortController: the deadline, caller cancellation and
    // connection close all cover send/headers/body/parse until settlement, so a
    // hung body still hits the deadline. No exchange is ever retried here.
    if (this.closed) throw new Error(`connection ${this.cfg.name} is closed`);
    if (opts.signal?.aborted) throw new CancelledError(false);
    const id = opts.notification ? null : this.nextId++;
    const controller = new AbortController();
    this.active.add(controller);
    const timer = setTimeout(() => controller.abort(ABORT_DEADLINE), timeoutSec * 1000);
    const onOuterAbort = () => controller.abort(ABORT_USER);
    if (opts.signal) {
      if (opts.signal.aborted) { clearTimeout(timer); this.active.delete(controller); throw new CancelledError(false); }
      opts.signal.addEventListener("abort", onOuterAbort, { once: true });
    }
    try {
      const body = id === null ? { jsonrpc: "2.0", method, params } : { jsonrpc: "2.0", id, method, params };
      const response = await fetch(this.cfg.url as string, {
        method: "POST",
        headers: this.headers(),
        body: JSON.stringify(body),
        signal: controller.signal,
      });
      const session = response.headers.get("mcp-session-id");
      if (session) this.sessionId = session;
      if (!response.ok) throw new Error(`HTTP ${response.status} from ${redactUrl(this.cfg.url ?? "")}`);
      if (id === null) return null; // notification accepted; nothing to wait for
      const contentType = response.headers.get("content-type") ?? "";
      if (contentType.includes("text/event-stream")) return await this.parseSse(response, id);
      const msg = await this.readBoundedJson(response, MAX_RESULT_TEXT) as JsonRpcResponse;
      if (msg.error) throw new Error(`server error ${msg.error.code}: ${msg.error.message.slice(0, 300)}`);
      return msg.result;
    } catch (err) {
      // classify by WHO aborted; sent requests keep an outcome-unknown wording
      const sent = id !== null;
      if (opts.signal?.aborted) {
        if (err instanceof CancelledError) throw err;
        throw new CancelledError(sent);
      }
      if (err === ABORT_DEADLINE) throw new Error(`${method} exchange timed out after ${timeoutSec}s${sent ? "; server outcome is unknown" : ""}`);
      if (err === ABORT_CLOSED) throw new Error(`${method} exchange ended because the connection was closed${sent ? "; server outcome is unknown" : ""}`);
      if (err === ABORT_USER) throw new CancelledError(sent);
      throw err;
    } finally {
      clearTimeout(timer);
      opts.signal?.removeEventListener("abort", onOuterAbort);
      this.active.delete(controller);
    }
  }

  async initialize(): Promise<void> {
    await this.request("initialize", {
      protocolVersion: "2025-06-18",
      capabilities: {},
      clientInfo: { name: "subagent-pi-bridge", version: "0.2.3" },
    }, this.cfg.startup_timeout_sec);
    await this.request("notifications/initialized", {}, this.cfg.startup_timeout_sec, { notification: true });
  }

  async callTool(name: string, args: unknown, timeoutSec: number, signal?: AbortSignal): Promise<unknown> {
    return await this.request("tools/call", { name, arguments: args ?? {} }, timeoutSec, { signal });
  }

  close(): void {
    if (this.closed) return;
    this.closed = true;
    for (const controller of this.active) controller.abort(ABORT_CLOSED);
    this.active.clear();
    this.sessionId = null;
  }
}

export default async function (pi: ExtensionAPI) {
  const boot = readBootstrap();
  const connections = new Map<string, McpConnection>();
  const connecting = new Map<string, Promise<McpConnection>>();
  const servers: ServerCfg[] = boot.payload?.mcp.servers ?? [];
  const access = boot.payload?.agent.access ?? "write";

  /** Deny (disabled_tools) wins first, for every access level. */
  function isDenied(cfg: ServerCfg, name: string): boolean {
    return cfg.disabled_tools.includes(name);
  }

  function toolVisible(cfg: ServerCfg, meta: ToolMeta): boolean {
    if (isDenied(cfg, meta.name)) return false;
    if (access === "read") {
      // P1-B: the parent's enabled_tools is NOT child authorization. A read child
      // sees only explicitly readOnly tools; an explicit allowlist can only SHRINK
      // the surface (empty = nothing). readOnlyHint is a self-report affecting the
      // managed tool surface only, not a sandbox claim.
      if (meta.readOnly !== true) return false;
      if (cfg.allowed_tools !== null && !cfg.allowed_tools.includes(meta.name)) return false;
      return true;
    }
    if (cfg.allowed_tools !== null && !cfg.allowed_tools.includes(meta.name)) return false;
    return true;
  }

  function needsConfirmation(cfg: ServerCfg, meta: ToolMeta): boolean {
    return access === "read" || (cfg.tool_approval[meta.name] ?? cfg.approval_default) !== "auto";  // the child rule wins over parent-side auto
  }

  async function ensureConnection(cfg: ServerCfg, signal?: AbortSignal): Promise<McpConnection> {
    const existing = connections.get(cfg.name);
    if (existing && !existing.dead) return existing;
    if (existing) {
      existing.close();
      connections.delete(cfg.name);
    }
    const inflight = connecting.get(cfg.name);
    if (inflight) return inflight;
    const create = (async () => {
      const conn = cfg.transport === "http" ? new HttpConnection(cfg) : new StdioConnection(cfg);
      connections.set(cfg.name, conn);
      try {
        await conn.initialize();
      } catch (err) {
        connections.delete(cfg.name);
        conn.close();
        throw new Error(`server ${cfg.name} failed to initialize: ${(err as Error).message.slice(0, 300)}`);
      }
      return conn;
    })();
    connecting.set(cfg.name, create);
    try {
      return await create;
    } finally {
      connecting.delete(cfg.name);
    }
  }

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
    server: Type.Optional(Type.String({ description: "Server name. list: optional (catalog one server); describe/call: required" })),
    tool: Type.Optional(Type.String({ description: "Tool name (describe/call)" })),
    args: Type.Optional(Type.Record(Type.String(), Type.Unknown(), { description: "Tool arguments object (call)" })),
  });

  async function execute(_id: string, params: { action: "list" | "describe" | "call"; server?: string; tool?: string; args?: Record<string, unknown> },
                         signal: AbortSignal, _onUpdate: unknown, ctx: { ui: { confirm: (title: string, message: string) => Promise<boolean> } }) {
    if (boot.error) {
      throw new Error(`Inherited MCP is unavailable in this child: ${boot.error}`);
    }
    if (params.action === "list" && !params.server) {
      // level 1: cold start connects nothing
      const report = servers.map((cfg) => ({
        server: cfg.name,
        transport: cfg.transport,
        required: cfg.required,
        policy: {
          allowed_tools: cfg.allowed_tools,
          disabled_tools: cfg.disabled_tools,
          approval_default: cfg.approval_default,
        },
      }));
      return { content: [{ type: "text", text: JSON.stringify({ servers: report }) }], details: { servers: report } };
    }
    const serverName = params.server;
    if (!serverName) throw new Error("action=describe|call requires server and tool");
    const cfg = servers.find((s) => s.name === serverName);
    if (!cfg) throw new Error(`Unknown server ${serverName}; use action=list`);
    if (params.action === "list") {
      // level 2: connect THIS server on demand; visibility rules apply here, not just at call time
      const conn = await ensureConnection(cfg, signal);
      const tools = await conn.ensureTools(cfg, signal);
      const visible = tools.filter((t) => toolVisible(cfg, t));
      const entries = visible.map((t) => ({ name: t.name, description: t.description ?? "", read_only: t.readOnly }));
      let truncated = conn.catalogTruncated || visible.length !== tools.length;
      while (JSON.stringify({ server: serverName, tools: entries, truncated }).length > MAX_RESULT_TEXT && entries.length > 0) {
        entries.pop();  // byte bound without breaking the JSON: drop whole entries
        truncated = true;
      }
      const report = { server: serverName, transport: cfg.transport, tools: entries, truncated };
      return { content: [{ type: "text", text: JSON.stringify(report) }], details: report };
    }
    const toolName = params.tool;
    if (!toolName) throw new Error("action=describe|call requires server and tool");
    if (isDenied(cfg, toolName)) {
      throw new Error(`Tool ${serverName}.${toolName} is excluded by the inherited server policy`);
    }
    const conn = await ensureConnection(cfg, signal);
    let tools = await conn.ensureTools(cfg, signal);
    let toolMeta = tools.find((t) => t.name === toolName);
    if (!toolMeta) {
      tools = await conn.ensureTools(cfg, signal); // cache may be stale after list_changed
      toolMeta = tools.find((t) => t.name === toolName);
      if (!toolMeta) throw new Error(`Tool ${toolName} is not offered by ${serverName}; use action=list with server=${serverName} to discover tools`);
    }
    if (params.action === "describe") {
      if (!toolVisible(cfg, toolMeta)) {
        throw new Error(`Tool ${serverName}.${toolName} is not exposed to this child by the inherited policy`);
      }
      if (!toolMeta.inputSchema) {
        throw new Error(`Tool ${serverName}.${toolName} has no usable inputSchema (missing or larger than ${MAX_SCHEMA_BYTES} bytes)`);
      }
      const report = { server: serverName, tool: toolMeta.name, description: toolMeta.description, inputSchema: toolMeta.inputSchema };
      return { content: [{ type: "text", text: JSON.stringify(report) }], details: report };
    }
    // action === "call": same effective policy, checked against current metadata right before execution
    if (!toolVisible(cfg, toolMeta)) {
      throw new Error(`Tool ${serverName}.${toolName} is not available to this managed child (access=${access}; only explicitly read-only tools are exposed)`);
    }
    if (needsConfirmation(cfg, toolMeta)) {
      const argsPreview = JSON.stringify(params.args ?? {}).slice(0, 500);
      const ok = await ctx.ui.confirm(
        "MCP tool call",
        `Allow inherited MCP call ${serverName}.${toolName}? args: ${argsPreview}`,
      );
      if (!ok) {
        return { content: [{ type: "text", text: "Approval denied; the MCP tool was not called" }], details: { confirmed: false } };  // denial is not an error
      }
    }
    if (signal?.aborted) throw new CancelledError(false);
    try {
      const result = await conn.callTool(toolName, params.args ?? {}, cfg.tool_timeout_sec, signal) as { isError?: boolean } | undefined;
      const text = describeResult(result);
      if (result?.isError) throw new Error(`MCP tool reported failure: ${text.slice(0, 1000)}`);  // Pi sets isError on throw
      return { content: [{ type: "text", text }], details: { server: serverName, tool: toolName } };
    } catch (err) {
      if ((err as Error).name === "AbortError" || (err as Error).name === "CancelledError") throw new CancelledError(true);
      // no automatic retry: a tools/call may have had side effects
      throw new Error(`MCP call ${serverName}.${toolName} failed: ${(err as Error).message.slice(0, 500)}`);
    }
  }

  pi.registerTool({
    name: "codex_mcp",
    label: "Codex MCP bridge",
    description:
      "Discover and call tools on Codex MCP servers inherited into this managed child. " +
      "Discovery: action=list (no server) shows configured servers and policy without " +
      "connecting; action=list with server=<name> connects that one server and lists its " +
      "visible tool names and short descriptions. action=describe with server+tool returns " +
      "a tool's full inputSchema; action=call invokes server+tool with an args object. " +
      "Connections live in memory for this worker's lifetime; nothing is cached on disk.",
    parameters,
    execute,
  });

  // Readiness receipt: required servers initialize eagerly so the daemon knows
  // dependencies work BEFORE any task is sent; optional servers stay lazy.
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
