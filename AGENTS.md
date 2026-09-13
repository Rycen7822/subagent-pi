# Maintenance

This package implements Codex-facing Pi agent control, not a new LLM harness.
Keep MCP and CLI on the same Runtime and schema source.
Do not add hooks, automatic prompt injection, workflow engines or implicit model fallbacks.
Keep SKILL.md <=35 lines; detailed guidance belongs in docs/.
Never equate queued input with observed consumption, or idle Pi with verified process cleanup.
Never replay an uncertain mutation automatically. Keep stable request and run identities.
Read results without acknowledging; acknowledgement binds an exact result hash.
New features need regression coverage at their real boundary (RPC, subprocess, IPC or packaging).
Use the offline fake Pi for routine tests. Live model calls require explicit operator consent.
Document limitations rather than silently weakening safety checks.

# Working conventions (.work/)

All temporary files, scratch documents, and intermediate artifacts go under `.work/` subdirectories (e.g. `.work/tmp/`, `.work/docs/`), never in the repo root.

Maintain exactly two files at the top of `.work/`:

- `.work/note.md` — key information for in-progress tasks: decisions, constraints, verification evidence, unresolved issues. Keep it current so nothing important is lost to context compaction.
- `.work/CODEX_STATE.md` — one or two short sentences: current task progress, plus anchors (path:lines or path — reason) pointing to key files and information locations.

Update these files at meaningful checkpoints or handoffs, not after every single read.
