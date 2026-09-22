# CLI reference

所有正常命令 stdout 为单个 JSON 值，诊断写 stderr。错误退出码 2；用户中断 130。stdio MCP 模式 stdout 仅 JSON-RPC。所有 CLI 命令和 MCP 使用同一账本。

## 通用

```text
subagent-pi [--home DIRECTORY] COMMAND
subagent-pi --version
subagent-pi doctor
subagent-pi schemas
subagent-pi call OP --json JSON_OR_DASH
```

--home 放在子命令之前；也可设置 PI_AGENTS_HOME。脚本建议用 `call --json -` 从 stdin 传 JSON。`schemas` 输出实际 MCP 参数定义，不需要记忆文档中的近似结构。

## Scope

```text
subagent-pi scope open --cwd ABSOLUTE_DIR [--scope EXISTING_SCOPE] [--label TEXT]
subagent-pi scope list
subagent-pi codex [-- CODEX_ARGS...]
```

常规命令带 `--scope ID`，或设置 PI_AGENTS_SCOPE。只有 CLI spawn 允许省略 scope 自动创建；MCP spawn 必须使用 pi_context 的 scope。

## Spawn / input

```text
subagent-pi spawn [--scope ID] [--cwd DIR] [--name NAME]
  [--profile PROFILE] [--model PROVIDER/MODEL] [--access read|write]
  [--idle-timeout-seconds N] [--request-id KEY]
  (--task TEXT | --task-file FILE_OR_DASH)

subagent-pi send AGENT_ID --scope ID (--message TEXT | --message-file FILE_OR_DASH)
  [--interrupt] [--request-id KEY]
subagent-pi steer AGENT_ID --scope ID (--message TEXT | --message-file FILE_OR_DASH)
  [--request-id KEY]
subagent-pi follow-up AGENT_ID --scope ID (--message TEXT | --message-file FILE_OR_DASH)
  [--request-id KEY]
```

task 应包含目标、必要上下文、授权范围和验收要求；Pi 不复制父 Codex 对话。steer 等当前 SDK 调用结束后在同一 run 续跑，回执 execution=after_current_sdk_call 不代表消费；follow-up 创建独立 run；send 用于空闲 agent。要立即停止工作用 close，替换工作用 send --interrupt。具体委派示例见 [lifecycle.md](lifecycle.md#spawn)。

## Observe / results

```text
subagent-pi list --scope ID [--limit N]
subagent-pi inspect AGENT_ID --scope ID [--after SEQ] [--limit N]
  [--detail tools|full] [--max-bytes N]
subagent-pi wait [RUN_IDS...] --scope ID [--mode any|all] [--timeout-seconds N]
subagent-pi result RUN_ID --scope ID [--offset N] [--max-bytes N]
subagent-pi ack RUN_ID --scope ID --sha256 HASH [--request-id KEY]
```

inspect 默认不返回 assistant message，detail=full 加入规范化文本，不等于输出全部原始 session。事件 cursor 和 result byte offset 是两种不同游标，不可互用。

## Lifecycle

```text
subagent-pi interrupt AGENT_ID --scope ID [--request-id KEY]
subagent-pi close AGENT_ID --scope ID [--request-id KEY]
subagent-pi respawn AGENT_ID --scope ID [--message TEXT] [--request-id KEY]
subagent-pi resume AGENT_ID --scope ID [--message TEXT] [--request-id KEY]
subagent-pi answer AGENT_ID UI_REQUEST_ID --scope ID --answer TEXT_OR_TRUE_FALSE
  [--request-id KEY]
```

interrupt 停止并核验子进程，保留 session；再次使用需 respawn。interrupt+message 显式替换为新进程。resume 为 respawn 别名；live idle agent 使用 send。close 保留 session；daemon stop --force 会影响所有 scope 的 resident agents，而非当前 scope。

## Runtime / docs

```text
subagent-pi daemon start
subagent-pi daemon status
subagent-pi daemon stop [--force]
subagent-pi daemon run
subagent-pi guide [TOPIC] [--section SECTION] [--offset N] [--max-bytes N]
```

daemon run 为前台诊断/服务管理入口；普通 MCP/CLI 会按需启动 daemon，不需要额外终端。guide 读取随安装发布的版本化 docs，默认最多 4 KiB，最多 16 KiB。
