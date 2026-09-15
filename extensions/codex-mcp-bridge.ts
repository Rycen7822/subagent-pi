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
  protocol_mode?: "auto" | "legacy_2025_06_18" | "modern_2026_07_28";
  tool_output_limits?: Record<string, number>;
  allowed_tools: string[] | null;
  disabled_tools: string[];
  approval_default: "auto" | "confirm";
  tool_approval: Record<string, "auto" | "confirm">;
  // Read children confirm every call; the parent may only ever tighten this.
  confirm_all?: boolean;
}
interface ToolMeta {
  name: string;
  description?: string;
  readOnly: boolean;
  inputSchema?: unknown;
  headerPlan: HeaderPlan;
}
// x-mcp-header (MCP 2026-07-28): a tool may declare that a plain
// string/integer/boolean argument is mirrored into an HTTP header. The plan is
// computed once from the inputSchema at discovery and kept in memory only.
type HeaderPlanEntry = { path: string[]; header: string; type: "string" | "integer" | "boolean" };
type HeaderPlan = { ok: true; entries: HeaderPlanEntry[] } | { ok: false; reason: string };
const HEADER_TOKEN = /^[!#$%&'*+.^_`|~0-9A-Za-z-]+$/;
const HEADER_DYNAMIC_KEYS = ["items", "oneOf", "anyOf", "allOf", "not", "if", "then", "else", "$ref", "dependentSchemas", "patternProperties"];
const HEADER_MAX_DEPTH = 8;
const HEADER_MAX_NODES = 256;
function parseHeaderPlan(schema: unknown): HeaderPlan {
  const entries: HeaderPlanEntry[] = [];
  const seen = new Set<string>();  // header names, lower-cased for case-insensitive uniqueness
  let nodes = 0;
  // Reachable annotations are found by walking ONLY through properties chains.
  const legalWalk = (node: unknown, path: string[], depth: number): string | null => {
    if (++nodes > HEADER_MAX_NODES) return `inputSchema exceeds the x-mcp-header walker limit (${HEADER_MAX_NODES} nodes)`;
    if (depth > HEADER_MAX_DEPTH) return `property '${path.join(".")}' exceeds the maximum nesting depth (${HEADER_MAX_DEPTH})`;
    if (typeof node !== "object" || node === null) return null;
    const properties = (node as { properties?: unknown }).properties;
    if (properties === undefined) return null;
    if (typeof properties !== "object" || properties === null) return `property '${path.join(".")}': properties is not an object`;
    for (const [param, def] of Object.entries(properties as Record<string, unknown>)) {
      if (typeof def !== "object" || def === null) continue;
      const here = [...path, param];
      if ("x-mcp-header" in def) {
        const why = (reason: string): string => `property '${here.join(".")}': ${reason}`;
        const annotation = (def as Record<string, unknown>)["x-mcp-header"];
        if (typeof annotation !== "string" || !annotation) return why("x-mcp-header annotation must be a non-empty string");
        if (!HEADER_TOKEN.test(annotation)) return why("x-mcp-header annotation is not a valid HTTP token");
        if (seen.has(annotation.toLowerCase())) return why(`x-mcp-header '${annotation}' duplicates an earlier header (case-insensitive)`);
        const type = (def as Record<string, unknown>).type;
        if (type !== "string" && type !== "integer" && type !== "boolean") return why("x-mcp-header requires type string, integer or boolean");
        seen.add(annotation.toLowerCase());
        entries.push({ path: here, header: annotation, type });
      }
      // a properties path must not pass THROUGH a dynamic ancestor ($ref,
      // items, oneOf, ...); annotations beneath one are caught by the count check
      if (HEADER_DYNAMIC_KEYS.some((k) => k in (def as Record<string, unknown>))) continue;
      const nested = legalWalk(def, here, depth + 1);
      if (nested) return nested;
    }
    return null;
  };
  // The full scan counts EVERY annotation in the schema. More annotations than
  // the properties-chain walk collected means one sits under a dynamic position
  // (items/oneOf/anyOf/allOf/not/if/then/else/$ref/dependentSchemas/patternProperties).
  const countAnnotations = (root: unknown): number => {
    let count = 0;
    const stack = [root];
    while (stack.length > 0) {
      if (++nodes > HEADER_MAX_NODES * 2) return Number.POSITIVE_INFINITY;  // budget shared with the legal walk
      const cur = stack.pop();
      if (Array.isArray(cur)) { stack.push(...cur); continue; }
      if (typeof cur !== "object" || cur === null) continue;
      for (const [k, v] of Object.entries(cur as Record<string, unknown>)) {
        if (k === "x-mcp-header") count += 1;
        else stack.push(v);
      }
    }
    return count;
  };
  if (typeof schema !== "object" || schema === null) return { ok: true, entries };
  const legalError = legalWalk(schema, [], 0);
  if (legalError) return { ok: false, reason: legalError };
  if (countAnnotations(schema) > entries.length) {
    return { ok: false, reason: "x-mcp-header annotation under an unsupported dynamic path (only plain properties chains are supported)" };
  }
  return { ok: true, entries };
}
// MCP 2026-07-28 header value encoding: plain visible ASCII (plus interior
// SP/HTAB, no leading/trailing whitespace) is sent as-is; anything else —
// non-ASCII, control characters, edge whitespace, or a value shaped like the
// Base64 sentinel — travels as `=?base64?<Base64 UTF-8>?=`.
// This is also the CRLF-injection defense: unsafe bytes never hit the wire raw.
function encodeMcpHeaderValue(value: string): string {
  let plain = value.length > 0;
  for (let i = 0; i < value.length; i++) {
    const c = value.charCodeAt(i);
    if ((c < 0x20 && c !== 0x09) || c > 0x7e || c === 0x7f ||
        ((c === 0x20 || c === 0x09) && (i === 0 || i === value.length - 1))) { plain = false; break; }
  }
  // ANY string shaped like the sentinel is re-encoded, so a literal value can
  // never be mistaken for an encoded one on the receiving side.
  if (plain && !(value.startsWith("=?base64?") && value.endsWith("?="))) return value;
  return `=?base64?${Buffer.from(value, "utf8").toString("base64")}?=`;
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
const MAX_ERROR_BODY = 64 * 1024;  // non-2xx JSON-RPC error bodies, era classification only
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

interface JsonRpcResponse { id?: number | string | null; result?: unknown; error?: { code: number; message: string; data?: unknown } }

function toToolMeta(raw: unknown, mirrorHeaders: boolean): ToolMeta | null {
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
    // stdio never parses or enforces x-mcp-header: an HTTP-only annotation must
    // not take a stdio tool out of the catalog.
    headerPlan: mirrorHeaders ? parseHeaderPlan(schema) : { ok: true, entries: [] },
  };
}

abstract class McpConnection {
  toolsCache: ToolMeta[] | null = null;
  catalogTruncated = false;
  protected mirrorHeaders: boolean = false;  // HTTP-only: stdio tools ignore x-mcp-header entirely
  private catalogEpoch = 0; // bump on list_changed so an in-flight crawl never repopulates an invalidated cache
  protected invalidateCatalog(): void {
    this.toolsCache = null;
    this.catalogEpoch += 1;
  }
  /** MCP 2026-07-28: every request self-describes via _meta (no handshake). */
  protected withMeta(params: unknown): unknown {
    const base = (typeof params === "object" && params !== null ? params : {}) as Record<string, unknown>;
    return { ...base, _meta: { ...((base._meta ?? {}) as Record<string, unknown>), ...MODERN_META } };
  }
  abstract get dead(): boolean;
  /** False when the next explicit operation must build a fresh connection (closed, or legacy session expired). */
  get reusable(): boolean { return !this.dead; }
  abstract request(method: string, params: unknown, timeoutSec: number, opts?: { notification?: boolean; signal?: AbortSignal }): Promise<unknown>;
  abstract initialize(): Promise<void>;
  abstract callTool(name: string, args: unknown, timeoutSec: number, signal?: AbortSignal,
                     headerPlan?: HeaderPlanEntry[]): Promise<unknown>;
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
        const meta = toToolMeta(tool, this.mirrorHeaders);
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
  private era: "legacy" | "modern";
  exitError: string | null = null;

  constructor(private cfg: ServerCfg) {
    super();
    this.mirrorHeaders = false;  // header mirroring is an HTTP-transport feature
    this.era = this.cfg.protocol_mode === "modern_2026_07_28" ? "modern" : "legacy";
  }

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
    if (this.proc) {
      if (this.exitError === null) return this.proc;
      // The process died after the handshake: this connection is FINISHED. A
      // replacement must be built by ensureConnection (which re-runs the full
      // initialize handshake); respawning in place would hand a fresh process
      // tools/call as its very first frame.
      throw new Error(`${this.exitError}; this connection is finished, the next explicit operation re-initializes a new process`);
    }
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
    this.generation += 1;  // one connection = one handshake generation, ever
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
          if (msg.error) entry.reject(new RpcError(msg.error.code, msg.error.message, msg.error.data));
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
    const wireParams = this.era === "modern" ? this.withMeta(params) : params;
    if (opts.notification) {
      this.sendFrame({ jsonrpc: "2.0", method, params: wireParams });  // no pending entry; async EPIPE is the stdin listener's job
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
      this.sendFrame({ jsonrpc: "2.0", id, method, params: wireParams });
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
    if (this.era === "legacy") {
      await this.legacyHandshake();
      return;
    }
    // Auto lifecycle (current Codex V20260728): the first RPC of a
    // modern-enabled process is a side-effect-free server/discover carrying
    // full modern _meta. A valid DiscoverResult or a recognized modern error
    // keeps the modern era; any OTHER legacy-style error or a discovery
    // timeout falls back to the full legacy handshake. Discovery is
    // side-effect-free, never retried, and a tools/call is never replayed.
    let result: unknown;
    try {
      result = await this.request("server/discover", {}, this.cfg.startup_timeout_sec);
    } catch (err) {
      if (classifyProtocolError(err) === "modern") return;  // recognized modern error: no fallback
      this.era = "legacy";
      await this.legacyHandshake();
      return;
    }
    assertDiscoverResult(result);  // malformed success: protocol error, no fallback
  }

  private async legacyHandshake(): Promise<void> {
    await this.request("initialize", {
      protocolVersion: "2025-06-18",
      capabilities: {},
      clientInfo: CLIENT_INFO,
    }, this.cfg.startup_timeout_sec);
    await this.request("notifications/initialized", {}, this.cfg.startup_timeout_sec, { notification: true });
  }

  // stdio ignores x-mcp-header: header mirroring is an HTTP-transport feature
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

// MCP protocol eras. Legacy (2025-06-18): initialize handshake + Mcp-Session-Id.
// Modern (2026-07-28): stateless — no handshake, every request self-describes via
// _meta and the MCP-Protocol-Version / Mcp-Method / Mcp-Name headers.
const MODERN_VERSION = "2026-07-28";
// MCP 2026 recognized modern protocol errors: HeaderMismatch,
// MissingRequiredClientCapability, UnsupportedProtocolVersion.
const MODERN_ERROR_CODES = new Set([-32020, -32021, -32022]);
const MAX_REDIRECTS = 3;

/** Single modern-error classifier shared by the HTTP probe and the stdio Auto
 * lifecycle: a recognized modern code keeps the modern era (no initialize);
 * anything else is a legacy-style error. -32022 additionally requires a common
 * supported version, reported as a hard incompatibility (never a fallback). */
function classifyProtocolError(err: unknown): "modern" | "legacy" {
  const code = err instanceof RpcError ? err.code : err instanceof HttpRpcError ? err.jsonRpcCode : null;
  if (code === null || !MODERN_ERROR_CODES.has(code)) return "legacy";
  if (code !== -32022) return "modern";
  const data = err instanceof RpcError ? err.data : err instanceof HttpRpcError ? err.jsonRpcData : null;
  const supported = Array.isArray((data as { supported?: unknown } | null)?.supported)
    ? (data as { supported: unknown[] }).supported.map(String) : [];
  if (!supported.includes(MODERN_VERSION)) {
    throw new Error(`server only supports modern protocol versions [${supported.join(", ") || "none listed"}]; ` +
      `this bridge speaks ${MODERN_VERSION} — no common modern version, refusing to guess`);
  }
  return "modern";
}

/** A successful server/discover must be a real 2026 DiscoverResult naming a
 * common modern version; anything else is a protocol error, never a fallback. */
function assertDiscoverResult(result: unknown): void {
  if (typeof result !== "object" || result === null) throw new Error("malformed DiscoverResult: result is not an object");
  const r = result as { resultType?: unknown; supportedVersions?: unknown };
  if (typeof r.resultType !== "string" || !r.resultType) throw new Error("malformed DiscoverResult: missing resultType");
  if (!Array.isArray(r.supportedVersions)) throw new Error("malformed DiscoverResult: supportedVersions is not an array");
  if (!r.supportedVersions.map(String).includes(MODERN_VERSION)) {
    throw new Error(`DiscoverResult supportedVersions [${r.supportedVersions.map(String).join(", ") || "none listed"}] ` +
      `has no common version with this bridge (${MODERN_VERSION})`);
  }
}
const CLIENT_INFO = { name: "subagent-pi-bridge", version: "0.2.9" };
const MODERN_META = {
  "io.modelcontextprotocol/protocolVersion": MODERN_VERSION,
  "io.modelcontextprotocol/clientInfo": CLIENT_INFO,
  "io.modelcontextprotocol/clientCapabilities": {},
};
class StaleSessionError extends Error { constructor(m: string) { super(m); this.name = "StaleSessionError"; } }
/** In-band JSON-RPC error (HTTP 200 body or SSE frame). */
class RpcError extends Error {
  constructor(public code: number, message: string, public data?: unknown) {
    super(`server error ${code}: ${message.slice(0, 300)}`);
    this.name = "RpcError";
  }
}
/** HTTP-level failure carrying the parsed JSON-RPC error body, when one existed. */
class HttpRpcError extends Error {
  constructor(public status: number, public jsonRpcCode: number | null,
              public jsonRpcData: unknown, public bodyParsed: boolean, url: string) {
    super(`HTTP ${status} from ${url}`);
    this.name = "HttpRpcError";
  }
}
const ABORT_USER = Symbol("user");
const ABORT_CLOSED = Symbol("closed");

class HttpConnection extends McpConnection {
  private sessionId: string | null = null;
  private nextId = 1;
  private closed = false;
  private active = new Set<AbortController>();  // every in-flight exchange, so close() can end them all
  private mode: "undecided" | "legacy" | "modern" = "undecided";
  private stale = false;  // legacy session expired (HTTP 404); re-initialize on the next explicit operation
  private negotiatedVersion: string | null = null;

  constructor(private cfg: ServerCfg) { super(); this.mirrorHeaders = true; }

  get dead(): boolean { return this.closed; }
  get reusable(): boolean { return !this.closed && !this.stale; }

  private headers(method?: string, toolName?: string, httpMethod = "POST",
                  paramHeaders?: Record<string, string>): Record<string, string> {
    const headers: Record<string, string> = {
      "content-type": "application/json",
      accept: "application/json, text/event-stream",
      ...(this.cfg.headers ?? {}),
    };
    if (this.cfg.bearer_token) headers.authorization = `Bearer ${this.cfg.bearer_token}`;
    // NOTE: no early return for DELETE — the legacy session id must ride along.
    if (this.mode !== "legacy") {
      // 2026-07-28 (and the auto probe): every request is self-describing; the
      // headers let gateways route and authorize without parsing JSON bodies.
      headers["mcp-protocol-version"] = MODERN_VERSION;
      if (method) headers["mcp-method"] = method;
      if (method === "tools/call" && toolName) headers["mcp-name"] = encodeMcpHeaderValue(toolName);
    } else {
      if (this.negotiatedVersion) headers["mcp-protocol-version"] = this.negotiatedVersion;  // negotiated at initialize
      if (this.sessionId) headers["mcp-session-id"] = this.sessionId;
    }
    if (paramHeaders) Object.assign(headers, paramHeaders);  // Mcp-Param-* from the tool's x-mcp-header plan
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
            if (msg.error) throw new RpcError(msg.error.code, msg.error.message);
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
                opts: { notification?: boolean; signal?: AbortSignal; toolName?: string; paramHeaders?: Record<string, string> } = {}): Promise<unknown> {
    // One exchange, one AbortController: the deadline, caller cancellation and
    // connection close all cover send/headers/body/parse until settlement, so a
    // hung body still hits the deadline. No exchange is ever retried here.
    if (this.closed) throw new Error(`connection ${this.cfg.name} is closed`);
    if (opts.signal?.aborted) throw new CancelledError(false);
    const id = opts.notification ? null : this.nextId++;
    const sent = id !== null;
    const wireParams = this.mode === "legacy" ? params : this.withMeta(params);
    const controller = new AbortController();
    this.active.add(controller);
    const timer = setTimeout(() => controller.abort(ABORT_DEADLINE), timeoutSec * 1000);
    const onOuterAbort = () => controller.abort(ABORT_USER);
    if (opts.signal) {
      if (opts.signal.aborted) { clearTimeout(timer); this.active.delete(controller); throw new CancelledError(false); }
      opts.signal.addEventListener("abort", onOuterAbort, { once: true });
    }
    try {
      const body = id === null ? { jsonrpc: "2.0", method, params: wireParams } : { jsonrpc: "2.0", id, method, params: wireParams };
      const response = await this.send(this.cfg.url as string, {
        method: "POST",
        headers: this.headers(method, opts.toolName, "POST", opts.paramHeaders),
        body: JSON.stringify(body),
        signal: controller.signal,
      });
      if (!response.ok) {
        // 2025-06-18 session lifecycle: a 404 on a session-scoped request means
        // the session expired; the client must re-initialize. The sent request
        // is never replayed automatically — its outcome stays unknown.
        if (response.status === 404 && this.mode === "legacy" && this.sessionId && method !== "initialize") {
          this.stale = true;
          throw new StaleSessionError(`MCP session expired (HTTP 404)${sent ? "; the sent request's outcome is unknown" : ""}; the next explicit operation re-initializes`);
        }
        throw await this.classifyHttpError(response);
      }
      if (this.mode === "legacy") {
        const session = response.headers.get("mcp-session-id");
        if (session) this.sessionId = session;
      }
      if (id === null) return null; // notification accepted; nothing to wait for
      const contentType = response.headers.get("content-type") ?? "";
      if (contentType.includes("text/event-stream")) return await this.parseSse(response, id);
      const msg = await this.readBoundedJson(response, MAX_RESULT_TEXT) as JsonRpcResponse;
      if (msg.error) throw new RpcError(msg.error.code, msg.error.message);
      return msg.result;
    } catch (err) {
      // classify by WHO aborted; sent requests keep an outcome-unknown wording
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
    const requested = this.cfg.protocol_mode ?? "auto";
    if (this.cfg.transport === "stdio" || requested === "legacy_2025_06_18") {
      this.mode = "legacy";
    } else if (requested === "modern_2026_07_28") {
      this.mode = "modern";
    } else {
      // auto: one modern discovery probe; fall back to legacy only on PROOF the
      // endpoint is legacy-only (HTTP 404/405 or JSON-RPC "method not found" on
      // server/discover — both side-effect free). A generic 4xx/5xx is an error,
      // never a downgrade trigger, and a sent tools/call is never replayed.
      this.mode = (await this.probeModern()) ? "modern" : "legacy";
    }
    if (this.mode === "modern") {
      await this.request("tools/list", {}, this.cfg.startup_timeout_sec);  // readiness probe warms the catalog
    } else {
      await this.legacyInitialize();
    }
  }

  private async legacyInitialize(): Promise<void> {
    const result = await this.request("initialize", {
      protocolVersion: "2025-06-18",
      capabilities: {},
      clientInfo: CLIENT_INFO,
    }, this.cfg.startup_timeout_sec) as { protocolVersion?: unknown } | undefined;
    // Negotiate: honor the server's returned version for all later requests.
    this.negotiatedVersion = typeof result?.protocolVersion === "string" && result.protocolVersion ? result.protocolVersion : "2025-06-18";
    await this.request("notifications/initialized", {}, this.cfg.startup_timeout_sec, { notification: true });
  }

  private async probeModern(): Promise<boolean> {
    // Era detection happens ONLY on the side-effect-free server/discover,
    // classified by the bounded JSON-RPC error BODY (never message strings):
    // a recognized modern error (-32020/-32021/-32022) keeps modern — no
    // initialize is ever sent; an unrecognized or legacy-style 400, 404 or 405
    // proves legacy-only. Auth/rate-limit/5xx failures are errors, never
    // downgrade triggers, and a sent tools/call is never replayed either way.
    try {
      assertDiscoverResult(await this.request("server/discover", {}, this.cfg.startup_timeout_sec));
      return true;
    } catch (err) {
      if (err instanceof HttpRpcError) {
        if (err.status !== 400 && err.status !== 404 && err.status !== 405) throw err;
        return classifyProtocolError(err) === "modern";
      }
      if (err instanceof RpcError) {
        if (classifyProtocolError(err) === "modern") return true;  // in-band modern error on HTTP 200
        if (err.code === -32601) return false;  // legacy-style method-not-found
        throw err;
      }
      throw err;  // timeouts and user aborts are errors, never a downgrade trigger
    }
  }

  /** Manual redirect handling (SameOriginRedirect policy): no header or body
   * is ever transmitted to a redirect target before its origin is verified
   * against the ORIGINAL MCP origin. 301/302/303 become body-less GETs;
   * 307/308 preserve method, headers and body. Every hop shares the exchange's
   * single deadline (one AbortController) and the visited set blocks cycles. */
  private async send(url: string, init: RequestInit): Promise<Response> {
    const originalOrigin = new URL(this.cfg.url as string).origin;
    let current = url;
    let hopInit = init;
    const visited = new Set<string>([current]);
    for (let hop = 0; ; hop++) {
      const response = await fetch(current, { ...hopInit, redirect: "manual" });
      if (![301, 302, 303, 307, 308].includes(response.status)) return response;
      const location = response.headers.get("location");
      if (!location) throw new Error(`HTTP ${response.status} redirect without Location from ${redactUrl(current)}`);
      const next = new URL(location, current);
      if (next.origin !== originalOrigin) {
        throw new Error(`refusing cross-origin redirect to ${redactUrl(next.toString())}; no headers or body were sent`);
      }
      if (visited.has(next.toString())) throw new Error(`redirect loop at ${redactUrl(next.toString())}`);
      visited.add(next.toString());
      if (hop >= MAX_REDIRECTS) throw new Error(`too many redirects (>${MAX_REDIRECTS})`);
      if (response.status !== 307 && response.status !== 308) {
        const headers = { ...(hopInit.headers as Record<string, string>) };
        delete headers["content-type"];
        delete headers["content-length"];
        hopInit = { ...hopInit, method: "GET", body: undefined, headers };
      }
      current = next.toString();
    }
  }

  private async classifyHttpError(response: Response): Promise<Error> {
    // The error body is read with a strict byte cap and used ONLY to classify
    // the protocol era and produce a precise diagnostic; it is never logged.
    const url = redactUrl(this.cfg.url ?? "");
    let body: unknown;
    let parsed = false;
    try {
      body = await this.readBoundedJson(response, MAX_ERROR_BODY);
      parsed = true;
    } catch { /* not JSON, or over the cap */ }
    const rpc = (body && typeof body === "object" ? (body as { error?: unknown }).error : undefined) as
      { code?: unknown; message?: unknown; data?: unknown } | undefined;
    if (rpc && typeof rpc.code === "number") {
      return new HttpRpcError(response.status, rpc.code, rpc.data, true, url);
    }
    return new HttpRpcError(response.status, null, null, parsed, url);
  }

  async callTool(name: string, args: unknown, timeoutSec: number, signal?: AbortSignal,
                 headerPlan?: HeaderPlanEntry[]): Promise<unknown> {
    let paramHeaders: Record<string, string> | undefined;
    if (this.mode === "modern" && headerPlan && headerPlan.length > 0) {
      // Mirror only declared, plain-typed arguments, read by schema path; the
      // body keeps every argument unchanged and an absent argument produces no header.
      paramHeaders = {};
      for (const entry of headerPlan) {
        let node: unknown = args;
        let reachable = true;
        for (const seg of entry.path) {
          if (typeof node !== "object" || node === null) { reachable = false; break; }
          node = (node as Record<string, unknown>)[seg];
        }
        if (!reachable || node === undefined) continue;
        const bad = (why: string): Error =>
          new Error(`argument ${entry.path.join(".")} ${why}; refusing to mirror it as a header`);
        if (entry.type === "boolean" && typeof node !== "boolean") throw bad("is not a boolean");
        if (entry.type === "integer" && !(typeof node === "number" && Number.isSafeInteger(node))) throw bad("is not a safe integer");
        if (entry.type === "string" && typeof node !== "string") throw bad("is not a string");
        paramHeaders[`Mcp-Param-${entry.header}`] = encodeMcpHeaderValue(String(node));
      }
      if (Object.keys(paramHeaders).length === 0) paramHeaders = undefined;
    }
    return await this.request("tools/call", { name, arguments: args ?? {} }, timeoutSec, { signal, toolName: name, paramHeaders });
  }

  close(): void {
    if (this.closed) return;
    this.closed = true;
    for (const controller of this.active) controller.abort(ABORT_CLOSED);
    this.active.clear();
    // Best-effort session termination per the 2025-06-18 spec (DELETE the session).
    // Fire-and-forget: the worker may be exiting, so an unconfirmed DELETE is a
    // documented boundary, never a retry path.
    if (this.mode === "legacy" && this.sessionId) {
      try {
        void fetch(this.cfg.url as string, {
          method: "DELETE", headers: this.headers(undefined, undefined, "DELETE"),
          redirect: "manual",  // session termination must never leak to another origin
          signal: AbortSignal.timeout(2000),
        }).then(r => { void r.body?.cancel(); }).catch(() => { });
      } catch { /* ignore */ }
    }
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
    // The child rule always wins over parent-side auto: a read child confirms
    // everything, and an explicit confirm_all (a server with no parent-side
    // allowlist) confirms everything even in a write child.
    if (access === "read") return true;
    if (cfg.confirm_all === true) return true;
    return (cfg.tool_approval[meta.name] ?? cfg.approval_default) !== "auto";
  }

  async function ensureConnection(cfg: ServerCfg, signal?: AbortSignal): Promise<McpConnection> {
    const existing = connections.get(cfg.name);
    if (existing && existing.reusable) return existing;
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
      // An invalid x-mcp-header annotation excludes just that tool; the server and
      // its other tools stay usable.
      const visible = tools.filter((t) => toolVisible(cfg, t) && t.headerPlan.ok);
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
      if (!toolMeta.headerPlan.ok) {
        throw new Error(`Tool ${serverName}.${toolName} declares an invalid x-mcp-header annotation (${toolMeta.headerPlan.reason}) and is not callable`);
      }
      const report = { server: serverName, tool: toolMeta.name, description: toolMeta.description, inputSchema: toolMeta.inputSchema };
      return { content: [{ type: "text", text: JSON.stringify(report) }], details: report };
    }
    // action === "call": same effective policy, checked against current metadata right before execution
    if (!toolVisible(cfg, toolMeta)) {
      throw new Error(`Tool ${serverName}.${toolName} is not available to this managed child (access=${access}; only explicitly read-only tools are exposed)`);
    }
    if (!toolMeta.headerPlan.ok) {
      throw new Error(`Tool ${serverName}.${toolName} declares an invalid x-mcp-header annotation (${toolMeta.headerPlan.reason}) and is not callable`);
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
      const result = await conn.callTool(toolName, params.args ?? {}, cfg.tool_timeout_sec, signal, toolMeta.headerPlan.ok ? toolMeta.headerPlan.entries : undefined) as { isError?: boolean } | undefined;
      let text = describeResult(result);
      // output_token_limit: enforced here at the serialization boundary with a
      // conservative 4 bytes/token budget; it can only TIGHTEN the default cap.
      const budget = cfg.tool_output_limits?.[toolName];
      if (budget && budget > 0) {
        const bytes = Buffer.from(text, "utf8");
        if (bytes.length > budget) text = bytes.subarray(0, budget).toString("utf8") + "\n[output truncated to the configured output_token_limit budget]";
      }
      if (result?.isError) throw new Error(`MCP tool reported failure: ${text.slice(0, 1000)}`);  // Pi sets isError on throw
      return { content: [{ type: "text", text }], details: { server: serverName, tool: toolName } };
    } catch (err) {
      if ((err as Error).name === "AbortError" || (err as Error).name === "CancelledError") throw new CancelledError(true);
      // no automatic retry: a tools/call may have had side effects
      throw new Error(`MCP call ${serverName}.${toolName} failed: ${(err as Error).message.slice(0, 500)}`);
    }
  }

  // P1-B: one proxy tool fronts every inherited server, so all calls through it
  // serialize conservatively. Enforced twice: here with an in-memory promise
  // chain (no persistent scheduler), and via Pi's executionMode below.
  let executeChain: Promise<unknown> = Promise.resolve();
  const serializedExecute = async (id: string, params: { action: "list" | "describe" | "call"; server?: string; tool?: string; args?: Record<string, unknown> },
                                   signal: AbortSignal, onUpdate: unknown, ctx: { ui: { confirm: (title: string, message: string) => Promise<boolean> } }) => {
    const run = executeChain.then(() => execute(id, params, signal, onUpdate, ctx));
    executeChain = run.then(() => undefined, () => undefined);  // a cancelled first call never poisons the chain
    return run;
  };

  pi.registerTool({
    name: "codex_mcp",
    executionMode: "sequential",
    label: "Codex MCP bridge",
    description:
      "Discover and call tools on Codex MCP servers inherited into this managed child. " +
      "Discovery: action=list (no server) shows configured servers and policy without " +
      "connecting; action=list with server=<name> connects that one server and lists its " +
      "visible tool names and short descriptions. action=describe with server+tool returns " +
      "a tool's full inputSchema; action=call invokes server+tool with an args object. " +
      "Connections live in memory for this worker's lifetime; nothing is cached on disk.",
    parameters,
    execute: serializedExecute,
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
