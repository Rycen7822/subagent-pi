# Sources and implementation notes

Implementation reference date: 2026-09-14. These are protocol/design references, not runtime network dependencies.

- OpenAI plugin packaging: https://developers.openai.com/plugins/build/plugins
- OpenAI Codex plugins: https://developers.openai.com/codex/plugins
- OpenAI Codex MCP configuration: https://developers.openai.com/codex/mcp
- MCP stdio framing: https://modelcontextprotocol.io/specification/2025-06-18/basic/transports
- Pi RPC documentation: https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/rpc.md
- Pi CLI session resolution: https://github.com/earendil-works/pi/blob/main/packages/coding-agent/src/main.ts
- Design inspiration: https://github.com/nicobailon/pi-subagents/tree/1a7101e69c3fb140cdc8a96551c50c40089a85f7

The prior source review of pi-subagents informed the distinction between queued/consumed steering, session ownership, process-group verification, completion receipts and bounded inspection. This package is independently implemented; it does not vendor that project's code or SDK. Pi and Codex remain separately installed external programs and retain their own licenses.

Portable root manifests and a Codex compatibility manifest are both included. The installer generates actual absolute MCP command paths and a local marketplace; it does not depend on unverified plugin-root interpolation. No hooks are included, required or suggested by the runtime.
