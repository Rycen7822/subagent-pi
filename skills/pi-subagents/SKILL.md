---
name: pi-subagents
description: Delegate bounded work to persistent subagent-pi; steer, inspect, interrupt, recover, and collect their results.
---

# Pi Subagents

Use the Pi MCP tools for normal control; use the CLI for diagnostics.
Open or resume `pi_context` with the actual absolute workspace cwd, then reuse its scope.
Never infer the workspace from the MCP server or daemon working directory.

Follow the user's delegation policy; do not add automatic review stages.
Give each task an objective, edit boundary, relevant paths, and expected result.
Avoid overlapping writers, including edits performed by the parent.
Keep mutation `request_id` stable across identical retries; inspect uncertain outcomes.

Prefer `pi_wait_agent` over progress polling. Wait cancellation never cancels Pi.
Use bounded incremental inspection only when needed; reuse returned cursors.
Queued steering is not confirmed consumption. Use explicit interruption or recovery.
Read and integrate results, then acknowledge the exact run and result hash.
When state is uncertain, query outstanding work in the same scope.
There are no automatic wakeups, hooks, or native Codex `/agents` integration.

Read only the relevant section of `../../docs/lifecycle.md`, `recovery.md`,
`configuration.md`, or `troubleshooting.md` when needed.
Human CLI and installation examples are in `../../docs/getting-started.md`.
Managed children load Pi's own configuration and then inherit Codex skills/MCP;
diagnostics: `subagent-pi doctor --inheritance`; details in `../../docs/inheritance.md`.
