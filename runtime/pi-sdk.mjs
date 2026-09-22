#!/usr/bin/env node
/** Plugin-owned headless transport over the unmodified Pi SDK.
 * The resource loader's public ExtensionRuntime actions are bound to our queue.
 * No AgentSession methods, prototypes, source or installed files are patched. */
import { pathToFileURL } from 'node:url';
import { createInterface } from 'node:readline';
import { randomUUID } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { TaskQueue } from './task-queue.mjs';

const [sdkPath, ...argv] = process.argv.slice(2);
const sdk = await import(pathToFileURL(sdkPath).href);
const args = sdk.parseArgs(argv);
const supported = new Set(['mode','provider','model','thinking','apiKey','systemPrompt','appendSystemPrompt','session','sessionDir',
  'extensions','skills','noExtensions','noSkills','tools','excludeTools','noTools','noBuiltinTools','promptTemplates',
  'noPromptTemplates','themes','noThemes','noContextFiles','offline','messages','fileArgs','unknownFlags','diagnostics']);
for (const key of Object.keys(args)) {
  if (!supported.has(key)) throw new Error(`Unsupported managed Pi option: ${key}`);
}
if (args.messages.length || args.fileArgs.length || args.diagnostics.some(d => d.type === 'error')) {
  throw new Error('Managed child accepts tasks only through its protocol; invalid Pi command arguments');
}
if (args.offline) process.env.PI_OFFLINE = '1';
const output = (value) => process.stdout.write(JSON.stringify(value) + '\n');
const report = (error) => output({ type: 'extension_error', originRunId: queue.context.getStore()?.id, error: String(error?.error ?? error) });
const unsupported = async () => { throw new Error('Session replacement/reload is not supported in a managed child; close and respawn explicitly'); };
let session, resources;
const queue = new TaskQueue(async (input) => {
  if (input.kind === 'custom') {
    await session.sendCustomMessage(input.message, { triggerTurn: true });
  } else {
    const content = input.content;
    const message = typeof content === 'string' ? content : content.filter(p => p.type === 'text').map(p => p.text).join('\n');
    const images = typeof content === 'string' ? undefined : content.filter(p => p.type === 'image');
    await session.prompt(message, { source: input.source, images,
      expandPromptTemplates: input.expandPromptTemplates,
      streamingBehavior: input.deliverAs });
  }
}, output);

function bindManagedActions() {
  // These are the public SDK resource runtime's action implementations, not
  // methods on the host session. Rebind at session_start before other extensions.
  resources.runtime.sendUserMessage = (content, options = {}) => {
    try { queue.enqueue({ content, source: 'extension', ...options }); } catch (error) { report(error); }
  };
  resources.runtime.sendMessage = (message, options = {}) => {
    const owner = queue.context.getStore();
    if (owner && owner !== queue.active) { report('Custom message rejected: its managed task ended'); return; }
    if (options.deliverAs === 'nextTurn' || options.triggerTurn === false ||
        (!queue.active && !options.triggerTurn)) {
      void session.sendCustomMessage(message, { ...options, triggerTurn: false }).catch(report);
    } else {
      try { queue.enqueue({ kind: 'custom', message }); } catch (error) { report(error); }
    }
  };
}

const cwd = process.cwd();
const agentDir = sdk.getAgentDir();
// Read ambient settings at boot, but keep mutations local to this child. The
// separate global/project scopes preserve package path resolution and precedence.
const settings = {};
for (const [scope, path] of Object.entries({ global: join(agentDir, 'settings.json'), project: join(cwd, '.pi/settings.json') })) {
  try { settings[scope] = readFileSync(path, 'utf8'); } catch (error) { if (error.code !== 'ENOENT') throw error; }
}
const settingsManager = sdk.SettingsManager.fromStorage({ withLock(scope, edit) {
  const updated = edit(settings[scope]);
  if (updated !== undefined) settings[scope] = updated;
} });
const services = await sdk.createAgentSessionServices({
  cwd, agentDir, settingsManager, extensionFlagValues: args.unknownFlags,
  resourceLoaderOptions: {
    additionalExtensionPaths: args.extensions,
    additionalSkillPaths: args.skills,
    additionalPromptTemplatePaths: args.promptTemplates,
    noExtensions: args.noExtensions, noSkills: args.noSkills,
    noPromptTemplates: args.noPromptTemplates, noContextFiles: args.noContextFiles,
    additionalThemePaths: args.themes, noThemes: args.noThemes,
    systemPrompt: args.systemPrompt, appendSystemPrompt: args.appendSystemPrompt,
    extensionFactories: [{ name: 'subagent-pi', hidden: true, factory(pi) {
      pi.on('session_start', bindManagedActions);
    } }],
    extensionsOverride(base) {
      resources = base;
      const managed = base.extensions.find(e => e.path === '<inline:subagent-pi>');
      if (!managed) throw new Error('Managed SDK extension did not load');
      return { ...base, extensions: [managed, ...base.extensions.filter(e => e !== managed)] };
    },
  },
});
if (resources.errors.length) throw new Error(resources.errors.map(e => `${e.path}: ${e.error}`).join('\n'));
const errors = services.diagnostics.filter(d => d.type === 'error');
if (errors.length) throw new Error(errors.map(d => d.message).join('\n'));
const sessionManager = args.session ? sdk.SessionManager.open(args.session) : sdk.SessionManager.create(cwd, args.sessionDir);
const savedModel = sessionManager.buildSessionContext().model;
const modelName = args.model ?? savedModel?.modelId ?? settingsManager.getDefaultModel();
const provider = args.provider ?? (args.model ? undefined : savedModel?.provider ?? settingsManager.getDefaultProvider());
const resolved = sdk.resolveCliModel({ cliProvider: provider, cliModel: modelName,
  cliThinking: args.thinking, modelRuntime: services.modelRuntime });
if (resolved.error || (modelName && !resolved.model)) throw new Error(resolved.error || 'Requested model not found');
if (args.provider && !modelName) throw new Error('Set an explicit model when selecting a provider for a managed child');
if (args.apiKey && resolved.model) await services.modelRuntime.setRuntimeApiKey(resolved.model.provider, args.apiKey);
({ session } = await sdk.createAgentSessionFromServices({ services, sessionManager,
  model: resolved.model, thinkingLevel: args.thinking ?? resolved.thinkingLevel,
  tools: args.tools, excludeTools: args.excludeTools, noTools: args.noTools ? 'all' : args.noBuiltinTools ? 'builtin' : undefined,
}));

session.subscribe(event => {
  queue.observe(event);
  // A host per-run boundary has no authority over the daemon task.
  if (event.type !== 'agent_settled') output(event);
});

sdk.initTheme(settingsManager.getTheme(), false);
const dialogs = new Map();
function dialog(method, fields, options) {
  const id = randomUUID();
  return new Promise(resolve => {
    let timer;
    const finish = value => { clearTimeout(timer); dialogs.delete(id); resolve(value); };
    dialogs.set(id, finish);
    if (options?.timeout) timer = setTimeout(() => finish(undefined), options.timeout);
    output({ type: 'extension_ui_request', id, method, ...fields });
  });
}
const ui = { ...session.extensionRunner.getUIContext(),
  select: (title, options, opts) => dialog('select', { title, options }, opts),
  confirm: async (title, message, opts) => Boolean(await dialog('confirm', { title, message }, opts)),
  input: (title, placeholder, opts) => dialog('input', { title, placeholder }, opts),
  editor: (title, prefill) => dialog('editor', { title, prefill }),
  notify: (message, type) => output({ type: 'extension_ui_request', method: 'notify', message, notifyType: type }),
};
await session.bindExtensions({ mode: 'rpc', uiContext: ui, onError: report,
  abortHandler: () => process.exit(130), shutdownHandler: () => process.exit(0),
  commandContextActions: { waitForIdle: () => session.waitForIdle(), newSession: unsupported,
    fork: unsupported, navigateTree: unsupported, switchSession: unsupported, reload: unsupported },
});

async function command(request) {
  const { id, type } = request;
  const reply = data => output({ type: 'response', id, command: type, success: true, data });
  try {
    switch (type) {
      case 'get_state':
        reply({ subagentProtocol: 1, sessionFile: session.sessionFile, sessionId: session.sessionId,
          model: session.model, isStreaming: Boolean(queue.active), pendingMessageCount: queue.active?.inputs.length ?? 0 });
        break;
      case 'get_commands': reply({ commands: resources.runtime.getCommands() }); break;
      case 'prompt':
        if (typeof request.runId !== 'string' || !request.runId) throw new Error('Missing managed run identity');
        queue.start(request.runId, { content: request.message, source: 'rpc' });
        reply({});
        break;
      case 'steer':
        queue.enqueue({ content: request.message, source: 'rpc', deliverAs: 'steer' }, queue.active);
        reply({});
        break;
      case 'extension_ui_response': {
        const value = request.cancelled ? undefined : request.value ?? request.confirmed;
        dialogs.get(id)?.(value);
        break;
      }
      default: throw new Error(`Unsupported managed command: ${type}`);
    }
  } catch (error) {
    output({ type: 'response', id, command: type, success: false, error: String(error) });
  }
}
const lines = createInterface({ input: process.stdin, crlfDelay: Infinity });
for await (const line of lines) {
  try { await command(JSON.parse(line)); } catch (error) { report(error); }
}
process.exit(0);
