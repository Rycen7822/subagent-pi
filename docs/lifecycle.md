# Lifecycle

## 身份与状态

scope 是父任务集合；agent_id 是长期身份；run_id 是一项委托任务；generation 是一次受管子进程；request_id 是控制操作的幂等身份。MCP 与 CLI 使用同一份账本。

agent 状态包括 starting、running、needs_input、idle、stopping、dormant、closed、orphaned、crashed。run 状态包括 queued、starting、running、needs_input、completed、failed、interrupted、crashed、cancelled、timed_out。completed 表示执行正常结束，不证明答案正确或可以发布。

## 完成边界

插件的 SDK 子进程拥有任务内输入队列。一个 run 可以包含多次 SDK prompt，以及 Pi 自己在 prompt 内完成的工具调用、重试、自动压缩和 before-settle 续跑。插件逐项 await SDK 调用，队列排空才发出携带 run_id 的 `managed_task_end`；Pi 的 `agent_end` / `agent_settled` 只描述宿主活动，不能结算任务。

扩展的 `pi.sendUserMessage` 及会触发执行的 `pi.sendMessage` 接入该队列；SDK 的公开 ExtensionRuntime action bindings 仅在受管子进程内绑定，未修改 AgentSession、原型或安装文件。输入按接受顺序串行执行，包含 input hook、认证及 before_agent_start，所以不会出现并发预处理抢占底层 run。每次用户消息续跑走完整 SDK prompt 路径，重新应用当前扩展的提示与工具规则；custom message 续跑沿用 SDK 的 sendCustomMessage 语义。

每条任务链用 AsyncLocalStorage 保留归属。任务结束后的异步输入会被拒绝并记录诊断，不能混入后继任务；没有活动任务时扩展不能自动启动模型任务。扩展自己创建其他 SDK session 或独立进程不受此队列管理，扩展仍是受信任的代码，插件不是沙箱。

主输入被 handled 且整条任务没有 assistant 结果时记为 failed，并继续排队的后继任务。SDK 异常同样产生明确终态。消息被接受不等于已经执行；正常完成不依赖 sleep、空闲轮询或 deadline。

daemon 结算时校验 generation、run_id 和 stopping，终态幂等。迟到/重复完成不能结束另一个 run。结果包括该任务链的最后 assistant 文本和累计 usage，读取不确认结果，ack 必须绑定精确 SHA-256。

## Spawn

`pi_context` 建立 workspace 根；spawn 的 cwd 必须是其自身或子目录的绝对路径。guard 启动插件自己的 Node SDK 入口，它加载所选 Pi 安装旁的 SDK。SessionManager 创建独立持久会话，后续恢复沿用同一路径。

默认最多 4 个 resident agent，每个 scope 最多 16 个历史 agent。writer 独占同一或嵌套 cwd，跨 scope 检查；parent Codex 不受此锁约束。

## Steering 与 follow-up

`mode="steer"` 仅用于活动任务：输入加入插件队列，等当前 SDK 调用结束后作为同一 run 的续跑执行。它不再插入正在进行的 Pi 工具循环。不同扩展输入和外部 steer 统一按接受顺序消费；不模拟 Pi 交互式 steer 的抢先优先级。普通 Pi 的行为不变。

queued 仅表示接受。consumed 来自 user message_end 的文本 FIFO 匹配，不证明模型理解或服从。未消费输入在任务终态标为 not_consumed；响应不确定可为 unknown，禁止自动重发。idle steer 返回 agent_idle，不隐式启动或恢复。

`mode="follow_up"` 由 daemon 持久排队，每项有独立 run_id，前一个任务及其续跑全部结束后才开始。`mode="send"` 只用于 idle agent。

## Interrupt

interrupt 取消 daemon 排队任务，并终止、核验插件自己拥有的进程组。这也停止输入 hook、认证预处理和扩展计时器；不会靠 Pi 的瞬时 idle 声称停止成功。返回 `process_retained=false`、state=dormant，cleanup 如实报告 verified/unknown。不会向普通 Pi 会话发信号。

session 和结果保留；单独 interrupt 后需显式 respawn。`pi_send_input(interrupt=true, message=...)` 明确要求替换工作：只有旧进程清理 verified 后才启动新 generation 执行该新消息，不重放旧任务。清理无法确认则拒绝替换。

## Close 与 respawn

close 先 TERM 再 KILL 并核验受管进程组，保留会话和结果。无法确认归属/清理时阻止恢复，不对猜测 PID 发信号。

respawn 要求旧 writer 已消失且会话存在；agent_id 不变、generation 增加。可附加新消息，不附加则恢复为 idle。CLI resume 是其别名。已存活的 idle agent 使用 send。

受管会话不允许扩展切换、fork、reload 或导航到其他 session；关闭/恢复是唯一替换入口。Pi 配置、技能、provider、工具和普通生命周期扩展仍加载，TUI 专用界面按 headless 模式处理。
