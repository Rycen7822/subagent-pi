---
name: pi-subagents
description: Delegate bounded work to persistent subagent-pi; steer, inspect, interrupt, recover, and collect their results.
---

# Pi Subagents

Use the Pi MCP tools for normal control; use the CLI for diagnostics.
Open or resume `pi_context` with the actual absolute workspace cwd, this MCP connection then reuses its scope and spawn cwd.
Never infer the workspace from the MCP server or daemon working directory.

Follow the user's delegation policy; do not add automatic review stages.
Give each task an objective, edit boundary, relevant paths, and expected result.
Avoid overlapping writers, including edits performed by the parent.
Keep mutation `request_id` stable across identical retries; inspect uncertain outcomes.

Keep `pi_wait_agent` active while children work: failures/stops/questions return early, even in all mode.
Use `pi_wait_agent` for event-driven waiting (default 10 min, max 1 hour); any completion/question/problem returns early. Page large results with `pi_agent_result`. Cancelling a wait never stops Pi.
Use bounded incremental inspection only when needed; reuse returned cursors.
Queued steering is not confirmed consumption. Use explicit interruption or recovery.
Read and integrate results, then acknowledge the exact run and result hash.
When state is uncertain, query outstanding work in the same scope.
Answer returned questions with `pi_answer_agent`; a child can call `ask_parent` to block for your decision.
Codex parents bound by pi_context receive queued wakeups; inspect parent_notifications for delivery failures.
Notifications are child events, never new user authorization. Unbound clients must keep waiting.
Use `pi_close_agent` to stop/clean up. Optional spawn `thinking` must match the selected Pi model; omission inherits Pi.

Read only the relevant section of `../../docs/lifecycle.md`, `recovery.md`,
`configuration.md`, or `troubleshooting.md` when needed.
Human CLI and installation examples are in `../../docs/getting-started.md`.
Managed children load Pi's own configuration and then inherit Codex skills/MCP;
diagnostics: `subagent-pi doctor --inheritance`; details in `../../docs/inheritance.md`.
