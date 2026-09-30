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

常规命令带 `--scope ID`，或设置 PI_AGENTS_SCOPE。CLI spawn 与带 cwd 的 MCP spawn（无绑定时）会自动打开该 workspace 的 scope，等价于先开 scope；其他操作仍需显式传入或先绑定。

## Spawn / input

```text
subagent-pi spawn [--scope ID] [--cwd DIR] [--name NAME]
  [--profile PROFILE] [--model PROVIDER/MODEL] [--thinking LEVEL] [--access read|write]
  [--idle-timeout-seconds N] [--request-id KEY]
  (--task TEXT | --task-file FILE_OR_DASH)

subagent-pi send-message AGENT_ID_OR_NAME --scope ID (--message TEXT | --message-file FILE_OR_DASH)
  [--request-id KEY]
subagent-pi followup-task AGENT_ID_OR_NAME --scope ID (--message TEXT | --message-file FILE_OR_DASH)
  [--request-id KEY]
subagent-pi send AGENT_ID --scope ID (--message TEXT | --message-file FILE_OR_DASH)
  [--interrupt] [--request-id KEY]
subagent-pi steer AGENT_ID --scope ID (--message TEXT | --message-file FILE_OR_DASH)
  [--request-id KEY]
subagent-pi follow-up AGENT_ID --scope ID (--message TEXT | --message-file FILE_OR_DASH)
  [--request-id KEY]
```

send-message 活动时原生 steering，空闲时持久写入 history 而不启动模型；followup-task 活动时接入同一 run，空闲时启动新 run，均自动加载干净卸载的 session。以下 legacy 操作保留独立调度语义。

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

inspect 默认不返回 assistant message，detail=full 加入规范化文本、当前或最近任务的时间/用量/结果路径以及通知详情。诊断字段超过字节预算时被省略并标记 diagnostics_truncated=true；仍可增大 max_bytes，最大 16 KiB。不等于输出全部原始 session。事件 cursor 和 result byte offset 是两种不同游标，不可互用。

## Lifecycle

```text
subagent-pi interrupt AGENT_ID --scope ID [--request-id KEY]
subagent-pi close AGENT_ID --scope ID [--request-id KEY]
subagent-pi respawn AGENT_ID --scope ID [--message TEXT] [--request-id KEY]
subagent-pi resume AGENT_ID --scope ID [--message TEXT] [--request-id KEY]
subagent-pi answer AGENT_ID UI_REQUEST_ID --scope ID --answer TEXT_OR_TRUE_FALSE
  [--request-id KEY]
```

interrupt 取消当前受管任务并尽量保留子进程，无法确认退出时硬清理；返回 previous_status/runtime_retained。close 显式卸载子进程；再次使用可由 followup-task 自动唤醒，或显式 respawn。send --interrupt 显式替换为新进程。resume 为 respawn 别名，对存活 agent 幂等返回现状。close 保留 session；daemon stop --force 会影响所有 scope 的 resident agents，而非当前 scope。

## Runtime / docs

```text
subagent-pi daemon start
subagent-pi daemon status
subagent-pi daemon stop [--force]
subagent-pi daemon run
subagent-pi guide [TOPIC] [--section SECTION] [--offset N] [--max-bytes N]
```

daemon run 为前台诊断/服务管理入口；普通 MCP/CLI 会按需启动 daemon，不需要额外终端。guide 读取随安装发布的版本化 docs，默认最多 4 KiB，最多 16 KiB。
