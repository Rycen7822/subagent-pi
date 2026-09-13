# Lifecycle

## 身份与状态

scope 是父任务集合；agent_id 是长期逻辑身份；run_id 是一项委托任务；generation 是一次 Pi 进程实例；request_id 是控制操作的幂等身份。

agent 状态包括 starting、running、needs_input、idle、stopping、dormant、closed、orphaned、crashed。run 状态包括 queued、starting、running、needs_input、completed、failed、interrupted、crashed、cancelled、timed_out。completed 仅表示这轮执行正常结束，不证明任务答案正确、测试通过或可以发布。

## Spawn

`pi_context` 先建立明确的 workspace 根；`pi_spawn_agent` 的 cwd 必须是该根或其子目录，并且是绝对路径。新 agent 使用 Pi 的 `--session-dir` 建立独立会话；运行时读取 `get_state.sessionFile` 作为后续恢复路径。成功返回后任务在 daemon 中继续，不需要保持 MCP/CLI 调用打开。

默认最多 4 个 resident agent，每个 scope 最多 16 个历史逻辑 agent。满额时明确报错，不静默抢占；关闭 idle agent 释放进程槽位，新的任务集合可以新建 scope。writer 在驻留期间独占同一或嵌套 cwd，跨 scope 也检查；parent Codex 不受此锁约束。

## Steering

`pi_send_input(mode="steer")` 仅用于活动 agent。返回 queued 只代表 Pi 接受，不代表已消费。通过后续 user message_end 事件的文本 FIFO 匹配观察 consumed。匹配证据不是 Pi 提供的端到端唯一消息 ID，不是“模型理解了或服从了”的证明。

若任务先结束，未消费消息标为 not_consumed；断线或响应不确定可能标为 unknown。相关状态可从 inspect 的 receipts 读取。相同文本的重复消息依接收先后匹配；不能依据文本匹配建立安全授权保证。

idle agent 的 steer 返回 agent_idle，不隐式开始新 turn，更不会隐式 respawn。steer 不打断已经执行中的工具，也不撤销文件修改。

## Send 与 follow-up

`mode="send"` 在 idle agent 上创建一个新 run，沿用同一个 Pi session。

`mode="follow_up"` 在 daemon 中创建 durable queued run。当前 run 完成后顺序发送下一项 prompt。刻意不用 Pi 原生 follow_up 来承载不同 run，以免把多项结果混入一个难以区分的 agent_end 边界。

该队列在 broker 重启后不盲目重放：未执行条目标为 cancelled，运行中条目标为 crashed，并保留结果/恢复线索。用户显式决定下一步。steer/队列上限为 20，超出明确拒绝。

## Interrupt

先取消 daemon follow-up 队列，再清 Pi steering/follow-up 队列，取消 pending UI 请求，发送 abort，并确认 Pi 已空闲。

如果 clear_queue 不支持、abort 超时或仍有活动状态，退回终止受管理进程组。返回 process_retained 和 cleanup。`cleanup=not_checked` 表示 RPC 已空闲，但不声称所有子进程都已消失。

`pi_send_input(interrupt=true, message=...)` 是 interrupt 后开始一项新 run；若中断降级为关闭 Pi 进程，则返回 worker_unavailable，需要显式 respawn，不隐藏重启行为。

## Close 与 respawn

close 终止受管理进程组，先 TERM 再 KILL 并检查；保留 session 和所有结果。若无法确认清理，状态保持 orphaned/unknown，不自动恢复。

respawn 仅在旧 writer 不再活动、session 存在时工作；agent_id 不变、generation 增加。可附加新的 message；不附加时只恢复为 idle。不会重放旧 shell 命令、自动扩大权限或自动换模型。

CLI 的 resume 是 respawn 的别名，适用于 dormant/closed/crashed 的 agent。仍然存活的 idle agent 应用 send，而不是 resume。

## Wait

wait 可指定精确 run_ids，也可省略以捕获当时的未确认任务集合。默认 any；all 等待所有选中 run 成为终态，遇到 needs_input 则立即返回。

默认 25 秒，支持显式更长等待，但须小于宿主 MCP 工具超时。取消 wait 仅取消订阅等待，不发送 abort；stdout 活动、读文件、工具进度不会让 wait 返回。

未确认的已完成任务会令 wait 立即返回，这是防遗漏设计。处理并确认它们后，再等新任务。不要反复等待同一个已完成但未确认的 run。

## Result acknowledgement

result 按 UTF-8 字节分页；next_offset 是下一页合法起点，hash 对应完整保存文件。阅读不改变 ack。完成处理或显式放弃结果后，通过 run_id + result_sha256 确认。ack 不删除文件。

确认错误 hash 会被拒绝，active run 不能确认。所有终态包括失败和取消也要纳入未处理任务集合。原始 Pi session 保留完整上下文；给模型的轨迹按上限展示，不包含 raw thinking。

## Needs input

select/input/editor 需要文本回答，confirm 需要布尔值。pi_answer_agent 仅对当前待处理的 UI request ID 生效，不自动批准。只支持上述 Pi RPC 对话框交互，不模拟 TUI 组件或任意终端交互。
