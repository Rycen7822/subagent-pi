# Lifecycle

## 身份与状态

scope 是父任务集合；agent_id 是长期身份；run_id 是一项委托任务；generation 是一次受管子进程；request_id 是控制操作的幂等身份。MCP 与 CLI 使用同一份账本。

agent 状态包括 starting、running、needs_input、idle、stopping、dormant、closed、orphaned、crashed。run 状态包括 queued、starting、running、needs_input、completed、failed、interrupted、crashed、cancelled、timed_out。completed 表示执行正常结束，不证明答案正确或可以发布。

## 完成边界

插件的 SDK 子进程拥有任务内输入队列。一个 run 可以包含多次 SDK prompt，以及 Pi 自己在 prompt 内完成的工具调用、重试、自动压缩和 before-settle 续跑。插件逐项 await SDK 调用，队列排空才发出携带 run_id 的 `managed_task_end`；Pi 的 `agent_end` / `agent_settled` 只描述宿主活动，不能结算任务。

扩展的 `pi.sendUserMessage` 及会触发执行的 `pi.sendMessage` 接入该队列；SDK 的公开 ExtensionRuntime action bindings 仅在受管子进程内绑定，未修改 AgentSession、原型或安装文件。输入按接受顺序串行执行，包含 input hook、认证及 before_agent_start，所以不会出现并发预处理抢占底层 run。每次用户消息续跑走完整 SDK prompt 路径，重新应用当前扩展的提示与工具规则；custom message 续跑沿用 SDK 的 sendCustomMessage 语义。

每条任务链用 AsyncLocalStorage 保留归属。任务结束后的异步输入会被拒绝并记录诊断，不能混入后继任务；没有活动任务时扩展不能自动启动模型任务。启动阶段显式启用命令展开的、已注册的本地 slash command 会在接收任务前串行执行完毕，仅调用命令 handler，不回退到模型 prompt。扩展自己创建其他 SDK session 或独立进程不受此队列管理，扩展仍是受信任的代码，插件不是沙箱。

受管子进程在加载 SDK 和扩展前保留专用 JSON 输出函数，将普通 `process.stdout.write`（含 console 输出和终端通知）转到 stderr 诊断日志，避免与协议帧粘连。此绑定仅作用于插件自己的子进程；普通 Pi 不受影响。扩展直接写底层文件描述符等绕过 Node stream 的行为不属于该隔离保证。

主输入被 handled 且整条任务没有 assistant 结果时记为 failed，并继续排队的后继任务。SDK 异常同样产生明确终态。消息被接受不等于已经执行；正常完成不依赖 sleep、空闲轮询或 deadline。

daemon 结算时校验 generation、run_id 和 stopping，终态幂等。迟到/重复完成不能结束另一个 run。结果包括该任务链的最后 assistant 文本和累计 usage，读取不确认结果，ack 必须绑定精确 SHA-256。

## Spawn

`pi_context` 建立 workspace 根；同一 MCP 连接后续可省略 scope，spawn 的 cwd 默认作用域根；显式 cwd 必须是其自身或子目录的绝对路径。新的 MCP 连接不会按 cwd 猜测旧 scope，需显式恢复。guard 启动插件自己的 Node SDK 入口，它加载所选 Pi 安装旁的 SDK。SessionManager 创建独立持久会话，后续恢复沿用同一路径。

默认最多 4 个 resident agent，每个 scope 最多 16 个历史 agent。writer 独占同一或嵌套 cwd，跨 scope 检查；parent Codex 不受此锁约束。

## Steering 与 follow-up

`mode="steer"` 仅用于活动任务：输入加入插件队列，等当前 SDK 调用结束后作为同一 run 的续跑执行。它不再插入正在进行的 Pi 工具循环。不同扩展输入和外部 steer 统一按接受顺序消费；不模拟 Pi 交互式 steer 的抢先优先级。普通 Pi 的行为不变。

queued 仅表示接受。consumed 来自 user message_end 的文本 FIFO 匹配，不证明模型理解或服从。未消费输入在任务终态标为 not_consumed；响应不确定可为 unknown，禁止自动重发。idle steer 返回 agent_idle，不隐式启动或恢复。

`mode="follow_up"` 由 daemon 持久排队，每项有独立 run_id，前一个任务及其续跑全部结束后才开始。`mode="send"` 只用于 idle agent。

## Interrupt

interrupt 取消 daemon 排队任务，并终止、核验插件自己拥有的进程组。这也停止输入 hook、认证预处理和扩展计时器；不会靠 Pi 的瞬时 idle 声称停止成功。返回 `process_retained=false`、state=dormant，cleanup 如实报告 verified/unknown。不会向普通 Pi 会话发信号。

session 和结果保留；单独 interrupt 后需显式 respawn。`pi_send_input(interrupt=true, message=...)` 明确要求替换工作：只有旧进程清理 verified 后才启动新 generation 执行该新消息，不重放旧任务。清理无法确认则拒绝替换。

## Close 与 respawn

MCP 只列出 pi_close_agent 作为停止工具；旧 pi_interrupt_agent 与 CLI interrupt 仍兼容。close 先 TERM 再 KILL 并核验受管进程组，保留会话和结果。无法确认归属/清理时阻止恢复，不对猜测 PID 发信号。

respawn 要求旧 writer 已消失且会话存在；agent_id 不变、generation 增加。可附加新消息，不附加则恢复为 idle。CLI resume 是其别名。已存活的 idle agent 使用 send。

受管会话不允许扩展切换、fork、reload 或导航到其他 session；关闭/恢复是唯一替换入口。Pi 配置、技能、provider、工具和普通生命周期扩展仍加载，TUI 专用界面按 headless 模式处理。

## 父代理关注与回答

`pi_wait_agent` 用 daemon 的事件条件等待，不轮询进度。默认最多等 10 分钟，单次最多 1 小时；timeout_ms 是上限，不是固定延迟，0 只检查当前状态。默认 any 在任一任务完成时立即返回；all 等所有正常完成，但失败、崩溃、中断、取消、运行超时或需要输入仍立即返回。取消等待只断开这次读取，不停止子代理。显式设置更小的 max_wait_seconds 会限制默认值和允许的上限；等待超时与任务自己的 deadline 无关。

`questions` 带 agent_id、run_id、问题 id、正文和选择项，供 `pi_answer_agent` 使用；模型可调用仅在受管子进程注册的 `ask_parent`，暂停工具执行直到显式回答。回答不会自动批准其他请求。标准安装使用 Codex 原生清单，为本插件设置 3630 秒 MCP 超时，覆盖一小时等待及传输余量；其他 MCP 客户端或手动使用通用清单时，仍需保证外层工具超时足够长。

wait 的每个终态结果最多预读 2 KiB，整页文本共用 8 KiB 预算；`has_more` 时从 next_offset 调 result。读结果不 ack，仍需精确 hash 确认。时间戳等轨迹细节留在 inspect 和 result，默认 run 摘要不重复输出。

父会话身份来自 Codex 每次 MCP 调用的 `_meta.threadId`（CLI 则读自身 CODEX_THREAD_ID），不接受模型参数指定父代理。scope 默认值按父会话隔离；绑定后不能静默改指另一个父会话。其他会话仍可用显式 scope 读取旧结果，创建新任务需使用自己的 scope。parent_notifications 在 context/list 返回绑定及最近投递状态。

完成、失败、崩溃、取消、中断、超时或问题产生持久通知；插件调用 `codex queue --thread … --message …`，由原 Codex 进程读取队列。父会话已完成回合但仍加载时会自动开启后续回合；忙碌时等到空闲，跨进程检查通常约 10 秒。不会强制打断正在工作的父代理，也不会复活已经关闭或明确中断的父会话。通知只带任务定位和事件类型，明确标为自动子代理事件，不构成用户授权。

投递状态区分 pending/sending/queued/failed/unknown/superseded：queued 只表示收到入队回执，不保证父代理已处理；提交超时、异常退出或发送中的 daemon 重启记为 unknown，绝不盲重发（CLI 每次调用都会生成新消息 ID）。尚未投递的问题若已回答、结果若已 ack，则取消该待发提醒；已经入队的提醒可能与主动 wait 重叠，父代理应按 run ID/hash 检查最新状态。daemon 停止时未发出的通知会保留到下次启动。无可信父身份时不猜测目的会话，仍以 wait/list 收取结果。

此机制要求 Codex 支持 queue（实测 0.155.1），使用绑定时的本地 CODEX_HOME；远程会话不在本机消息存储中时不能据此承诺唤醒。未修改 Pi/Codex 宿主，也不模拟键盘或重启用户会话。
