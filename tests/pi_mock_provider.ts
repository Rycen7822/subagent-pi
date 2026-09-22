/** Offline provider for real SDK/MCP tests. All network requests fail. */
import { existsSync } from "node:fs";
import { createAssistantMessageEventStream } from "@earendil-works/pi-ai";

const env = process.env;
const sleep = (ms: number) => new Promise(resolve => setTimeout(resolve, ms));
const textOf = (content) => typeof content === "string" ? content
  : (content || []).filter(part => part.type === "text").map(part => part.text).join(" ");
const mark = (text: string) => process.stderr.write(text + "\n");

export default function (pi) {
  globalThis.fetch = async () => { throw new Error("NETWORK_FORBIDDEN"); };
  if (env.PI_MOCK_FORCE_PROMPT) pi.on("before_agent_start", () => ({ systemPrompt: env.PI_MOCK_FORCE_PROMPT }));
  let calls = 0;
  pi.registerProvider("pi-mock-offline", {
    baseUrl: "http://127.0.0.1:1", apiKey: "offline-test-only", api: "openai-responses",
    models: [{ id: "mock", name: "Offline Mock", reasoning: false, input: ["text"],
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }, contextWindow: 128000, maxTokens: 1024 }],
    streamSimple(model, context) {
      const stream = createAssistantMessageEventStream();
      const call = ++calls;
      mark(`PI_MOCK_REPLY ${call}`);
      const texts = context.messages.map(entry => textOf(entry.content)).filter(Boolean);
      if (env.PI_MOCK_WIRE === "1") {
        mark(`PI_MOCK_WIRE ${call} ${JSON.stringify({ texts,
          forced: context.systemPrompt || textOf(context.messages.find(entry => entry.role === "system")?.content) })}`);
      }
      if (env.PI_MOCK_CONTEXT === "1") mark(`PI_MOCK_CONTEXT ${call} ${texts.join(" | ")}`);
      const message = { role: "assistant", content: [{ type: "text", text: `MOCK_REPLY_${call}` }],
        api: model.api, provider: model.provider, model: model.id, stopReason: "stop", timestamp: Date.now(),
        usage: { input: 1, output: 1, cacheRead: 0, cacheWrite: 0, totalTokens: 2,
          cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } } };
      setTimeout(() => { stream.push({ type: "done", reason: "stop", message }); stream.end(); }, Number(env.PI_MOCK_STREAM_MS || 30));
      return stream;
    },
  });

  let continued = false;
  pi.on("agent_before_settle", async event => {
    mark("PI_MOCK_BEFORE_SETTLE");
    if (env.PI_MOCK_SETTLE_MS) await sleep(Number(env.PI_MOCK_SETTLE_MS));
    if (env.PI_MOCK_CONTINUE === "1" && !continued) {
      continued = true;
      return { entries: [...event.entries, { type: "custom_message", customType: "mock-continuation",
        content: "Continue once for the lifecycle test.", display: false }], continue: true };
    }
  });

  const [first, sibling, nested] = [env.PI_MOCK_SETTLED_SEND, env.PI_MOCK_SETTLED_SIBLING, env.PI_MOCK_SETTLED_NESTED];
  const sent = new Set<string>();
  const entered = new Set<string>();
  if (first) {
    pi.on("agent_settled", () => {
      for (const value of [first, sibling, entered.has(first) ? nested : undefined]) {
        if (value && !sent.has(value)) { sent.add(value); pi.sendUserMessage(value); }
      }
    });
    pi.on("input", async event => {
      if (![first, sibling, nested].includes(event.text)) return;
      entered.add(event.text);
      mark(`PI_MOCK_SETTLED_INPUT_TEXT ${event.text}`);
      if (env.PI_MOCK_SETTLED_INPUT_MS) await sleep(Number(env.PI_MOCK_SETTLED_INPUT_MS));
      mark(`PI_MOCK_SETTLED_INPUT_MODE ${env.PI_MOCK_SETTLED_INPUT || "continue"}`);
      return { action: env.PI_MOCK_SETTLED_INPUT === "handled" ? "handled" : "continue" };
    });
  }

  let blockIndex = 0;
  const tokens = (env.PI_MOCK_BLOCK_INPUT || "").split(",").filter(Boolean);
  pi.on("input", async (event, ctx) => {
    if (env.PI_MOCK_CONFIRM && event.text.includes("ASK")) {
      mark("PI_MOCK_CONFIRM_WAIT");
      const answer = await ctx.ui.confirm("Permission", "Proceed with mock work?");
      mark(`PI_MOCK_CONFIRM_ANSWER ${answer}`);
      if (!answer) return { action: "handled" };
    }
    if (!tokens.some(token => event.text.includes(token))) return;
    const index = ++blockIndex;
    mark(`PI_MOCK_INPUT_BLOCKED_START ${index} ${event.text}`);
    const deadline = Date.now() + Number(env.PI_MOCK_BLOCK_MS || 0);
    while (Date.now() < deadline) {
      if (env.PI_MOCK_RELEASE_FILE && existsSync(env.PI_MOCK_RELEASE_FILE)) break;
      if (env.PI_MOCK_BLOCK_RELEASE_DIR && existsSync(`${env.PI_MOCK_BLOCK_RELEASE_DIR}/${index}`)) break;
      await sleep(20);
    }
    mark(`PI_MOCK_INPUT_BLOCKED_END ${index}`);
    return { action: env.PI_MOCK_BLOCK_RESULT === "handled" ? "handled" : "continue" };
  });

  let started = false;
  pi.on("agent_start", () => {
    if (started) return;
    started = true;
    for (const text of (env.PI_MOCK_EXT_FOLLOWUP || "").split(",").filter(Boolean)) {
      pi.sendUserMessage(text, { deliverAs: "followUp" });
    }
    if (env.PI_MOCK_LATE_RELEASE) void (async () => {
      while (!existsSync(env.PI_MOCK_LATE_RELEASE)) await sleep(20);
      pi.sendUserMessage("STALE_TIMER_INPUT");
      mark("PI_MOCK_LATE_ATTEMPTED");
    })();
  });
}
