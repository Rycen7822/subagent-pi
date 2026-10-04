# CLI reference

业务命令 stdout 为单个 JSON 值；`--version` 和帮助输出例外。诊断写 stderr，错误退出码 2，用户中断 130。MCP 模式 stdout 为 JSON-RPC。CLI 与 MCP 共用 Runtime、schema 和账本。

## 通用

```text
subagent-pi [--home DIRECTORY] COMMAND
subagent-pi --version
subagent-pi doctor [--inheritance]
subagent-pi schemas
subagent-pi call OP --json JSON_OR_DASH
```

`--home` 放在子命令之前；`PI_AGENTS_HOME` 可指定同一状态目录。`schemas` 输出当前 CLI 对应的 10 个日常 MCP 工具的输入/输出定义，不能证明运行中的 Codex 连接已采用该版本。`call --json -` 从 stdin 读原始操作参数，适合脚本；其操作名以实际 schema/Runtime 为准。

带 `--request-id` 的 CLI 任务控制操作省略该参数时会生成并回显 ID。可靠重试应从首次调用就显式指定同一个 key，并保持参数不变；结果不确定先查账本。带 request_id 字段的 MCP 工具要求显式提供，成功回执不重复回显它；管理 pi_context 没有该字段，不使用这套请求账本。规则见 [recovery.md](recovery.md)。

## Scope

```text
subagent-pi scope open --cwd ABSOLUTE_DIR [--scope EXISTING_SCOPE] [--label TEXT]
subagent-pi scope list
subagent-pi codex [-- CODEX_ARGS...]
```

CLI spawn 未指定 scope 时按 cwd 自动打开 scope；cwd 默认当前终端目录。其他业务命令传 `--scope ID` 或设置 `PI_AGENTS_SCOPE`。`scope open` 创建/恢复 CLI 账本作用域，不会绑定现有 MCP 连接；将返回的 scope 显式传给 MCP，或通过管理 pi_context 绑定。并发 worktree 示例见 [lifecycle.md](lifecycle.md#scope-与-worktree)。

启动器解析 `-C DIR`、`--cd DIR`、`--cd=DIR`、`-CDIR` 并绑定实际目标目录，参数原样传给 Codex。wrapper 的首个 `--` 会移除，之后的内部 `--` 终止目录扫描；详见 [getting-started.md](getting-started.md#首次使用与-scope)。

## Spawn 与日常消息

```text
subagent-pi spawn [--scope ID] [--cwd DIR] [--name NAME]
  [--profile PROFILE] [--model PROVIDER/MODEL] [--thinking LEVEL] [--access read|write]
  [--idle-timeout-seconds N] [--request-id KEY]
  (--task TEXT | --task-file FILE_OR_DASH)

subagent-pi send-message AGENT_ID_OR_NAME --scope ID
  (--message TEXT | --message-file FILE_OR_DASH) [--request-id KEY]
subagent-pi followup-task AGENT_ID_OR_NAME --scope ID
  (--message TEXT | --message-file FILE_OR_DASH) [--request-id KEY]
subagent-pi answer AGENT_ID_OR_NAME UI_REQUEST_ID --scope ID
  --answer TEXT_OR_TRUE_FALSE [--request-id KEY]
```

send-message 对活动任务使用原生 steering；空闲时只保存消息，不启动模型。followup-task 对活动任务加入同一 run，空闲时创建新 run；两者可加载已核验卸载的 session。followup-task 与下文兼容命令 follow-up 的调度语义不同。

Pi 不复制父对话；task 写明目标、相关事实/路径、授权边界及验收要求。名称可用于后续寻址；歧义名称会拒绝。正文与标识符限制见 [lifecycle.md](lifecycle.md#身份与状态)，模型/推理选择见 [configuration.md](configuration.md#profiles)。ask_parent 和 Pi UI 返回的问题使用 answer 显式回复。CLI `--answer true` / `false` 转为布尔值，供确认问题使用；文本问题需要字面文本 `true` / `false` 时，使用 MCP 的字符串 answer，或 `call answer --json -` 保留 JSON 字符串类型。选择问题必须原样回复提供的选项。

## 观察与结果

```text
subagent-pi list --scope ID [--query TEXT] [--sort updated|created] [--offset N] [--limit N]
subagent-pi inspect AGENT_ID_OR_NAME --scope ID [--after SEQ] [--limit N]
  [--detail tools|full] [--max-bytes N]
subagent-pi wait [RUN_IDS...] --scope ID [--mode any|all] [--timeout-seconds N]
subagent-pi wait --agents NAME_OR_ID... --scope ID [--mode any|all]
subagent-pi result [RUN_ID | --agent NAME_OR_ID] --scope ID [--offset N] [--max-bytes N]
```

wait 默认 any、最多等 600 秒；并行协调时对同一 scope 的剩余 run 保持一次 wait，任一完成/失败/停止/问题就处理返回的就绪结果，再等待剩余任务。显式 `--mode all` 等选中任务全部进入终态，即使已有任务失败或停止也继续等其他任务；问题和超时仍返回。all 在响应准备好前不抑制部分完成通知，但通知入队不保证打断阻塞中的调用。MCP 的 pi_wait_agent 使用相同的 mode 和语义。timeout_seconds 最大 3600，0 只检查，取消等待不停止任务。“全部”是本次 run 快照，不包含之后新建的任务或其他 scope；问题需明确回答。wait / result 成功输出后自动消费终态通知，不需 ACK；list / inspect 保留关注。省略选择时等待活动或未交付 run；--agents 固定当前/最近 run，多任务歧义需明确 run ID。

inspect 默认有界事件；detail=full 加入规范化文本、当前/最近任务的时间、用量、结果路径和通知详情。诊断超出预算会被省略并标记 diagnostics_truncated；max_bytes 最大 16 KiB。事件 cursor 与结果 byte offset 是两种游标。

wait 整页结果正文共享 8 KiB 预算，先提供完整小结果；正文完整时直接审核，不需再调 result。has_more 时用返回的 run.id 与 next_offset 调 result。--agent 只可取第一页，后续按 run ID 翻页；大正文的 result_sha256 对应整个结果文件。存在未确定消费或失败的输入时返回有界 input_issues。list 搜索不改变 scope-wide outstanding；按 next_offset 翻页并保持 query/sort 相同，updated 排序会随任务状态变化。0.6.0 移除 ack 命令和 acknowledged 字段；结果交付后仍可重读。

## 中断、卸载与恢复

```text
subagent-pi interrupt AGENT_ID_OR_NAME --scope ID [--request-id KEY]
subagent-pi close AGENT_ID_OR_NAME --scope ID [--request-id KEY]
subagent-pi respawn AGENT_ID_OR_NAME --scope ID [--message TEXT] [--request-id KEY]
subagent-pi resume AGENT_ID_OR_NAME --scope ID [--message TEXT] [--request-id KEY]
```

interrupt 取消当前受管任务及排队工作，并尽量保留进程；返回 previous_status/runtime_retained，必要时硬清理。close 明确卸载并核验进程组，保留 session/结果。已核验 dormant/closed agent 通常可直接用 followup-task 继续；respawn/resume 用于确保加载，存活 worker 不替换，存活时附带 message 会被拒绝。清理 unknown 的 agent 需先处理旧实例，见 [recovery.md](recovery.md)。

已结算进程默认空闲 30 分钟后自动卸载；读取和遥测不续期，空闲消息会续期。interrupt、close 和恢复均不撤销已有文件或外部副作用。

## 兼容输入与显式管理

以下命令及对应 pi_send_input 保留旧调度方式；不在日常 MCP 工具目录中。pi_context、pi_close_agent、pi_respawn_agent 也作为显式管理调用保留。

```text
subagent-pi send AGENT_ID_OR_NAME --scope ID
  (--message TEXT | --message-file FILE_OR_DASH) [--interrupt] [--request-id KEY]
subagent-pi steer AGENT_ID_OR_NAME --scope ID
  (--message TEXT | --message-file FILE_OR_DASH) [--request-id KEY]
subagent-pi follow-up AGENT_ID_OR_NAME --scope ID
  (--message TEXT | --message-file FILE_OR_DASH) [--request-id KEY]
```

steer 在当前 SDK 调用之后接入同一 run，不影响正在进行的工具循环；follow-up 持久排队一个独立 run；send 仅用于空闲任务。send/follow-up 可唤醒已核验卸载的 agent，steer 不会隐式唤醒。send --interrupt 明确终止并核验旧进程，再以原 session 启动新 generation 和新任务。

原始 IPC `interrupt` 是兼容硬停止；CLI `interrupt` 和日常 pi_interrupt_agent 映射到 soft_interrupt。脚本使用 call 原始操作时需区分这些操作名。

## Runtime 与文档

```text
subagent-pi daemon start
subagent-pi daemon status
subagent-pi daemon stop [--force]
subagent-pi daemon run
subagent-pi guide [TOPIC] [--section SECTION] [--offset N] [--max-bytes N]
```

daemon run 用于前台诊断/服务管理；普通 MCP/CLI 按需启动 daemon。stop --force 会中断同一状态目录内所有 scope 的 resident agent。guide 读取对应安装内的版本化 docs，默认最多 4 KiB、上限 16 KiB；next_offset/has_more 用于分页，--section 按标题选段。升级要同时刷新源码安装、Codex 缓存和新会话，见 [getting-started.md](getting-started.md#升级与移除)。
