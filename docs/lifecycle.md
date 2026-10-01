# Lifecycle

## 身份与状态

scope 是父任务集合；agent_id 是长期身份；run_id 是一项委托任务；generation 是一次受管子进程；request_id 是控制操作的幂等身份。MCP 与 CLI 使用同一份账本。 创建时用 name 标注角色或任务（如 git-stats-fix），同一 scope 内唯一；省略时使用 agent_id。创建、任务摘要、等待、问题、结果和通知均携带该名称；后续操作可使用 ID 或 scope 内唯一名称；名称与另一 ID 冲突时拒绝歧义目标。

名称和 agent 目标最多 128 个字符，允许 Unicode；scope、run、request 等技术 ID 最多 128 个 ASCII 字符，只允许字母、数字、`_ . : -`。任务与消息正文最多 65536 个 UTF-8 字节；schema 的 `x-maxBytes` 标注这一服务端约束，普通 JSON Schema 校验器不会自行执行该扩展。

agent 状态包括 starting、running、needs_input、idle、stopping、dormant、closed、orphaned、crashed。run 状态包括 queued、starting、running、needs_input、completed、failed、interrupted、crashed、cancelled、timed_out。completed 表示执行正常结束，不证明答案正确或可以发布。

## 完成边界

插件的 SDK 子进程拥有任务内输入队列。一个 run 可以包含多次 SDK prompt，以及 Pi 自己在 prompt 内完成的工具调用、重试、自动压缩和 before-settle 续跑。插件逐项 await SDK 调用，队列排空才发出携带 run_id 的 `managed_task_end`；Pi 的 `agent_end` / `agent_settled` 只描述宿主活动，不能结算任务。

扩展的 `pi.sendUserMessage` 及会触发执行的 `pi.sendMessage` 接入该队列；SDK 的公开 ExtensionRuntime action bindings 仅在受管子进程内绑定，未修改 AgentSession、原型或安装文件。传统输入按接受顺序串行执行，包含 input hook、认证及 before_agent_start；V2 外部活动消息使用 SDK 原生 steering，原生消息预处理也按接受顺序执行并计入任务所有权。队列最多积压 256 条、8 MiB；扩展超过上限时专属子进程退出，daemon 记录崩溃并取消后继任务。每次用户消息续跑走完整 SDK prompt 路径，重新应用当前扩展的提示与工具规则；custom message 续跑沿用 SDK 的 sendCustomMessage 语义。

每条任务链用 AsyncLocalStorage 保留归属。任务结束后的异步输入会被拒绝并记录诊断，不能混入后继任务；没有活动任务时扩展不能自动启动模型任务。启动阶段显式启用命令展开的、已注册的本地 slash command 会在接收任务前串行执行完毕，仅调用命令 handler，不回退到模型 prompt。扩展自己创建其他 SDK session 或独立进程不受此队列管理，扩展仍是受信任的代码，插件不是沙箱。

受管子进程在加载 SDK 和扩展前保留专用 JSON 输出函数，将普通 `process.stdout.write`（含 console 输出和终端通知）转到 stderr 诊断日志，避免协议帧粘连。SDK 协议待写字节（含 Node 缓冲）上限 8 MiB，额外排队最多 256 帧；背压期间可合并/丢弃临时进度，结果、问题、RPC 与任务边界保持顺序。关键帧无法容纳或等待 drain 超过 45 秒时，子进程以 74 退出，由 daemon 记录崩溃并取消队列，不假装送达。这是输出通道保护，不限制任务总时长或正常等待工具/回答的时长。普通 Pi 不受影响；扩展直接写底层文件描述符绕过 Node stream 的行为不属于此保证。

主输入被 handled 且整条任务没有 assistant 结果时记为 failed，并继续排队的后继任务。SDK 异常同样产生明确终态。消息被接受不等于已经执行；正常完成不依赖 sleep、空闲轮询或 deadline。

daemon 在 agent 锁内校验当前 Worker 身份、generation、run_id 和 stopping，终态幂等。迟到/重复完成不能结束另一个 run。结果包括该任务链的最后 assistant 文本和累计 usage，读取不确认结果，ack 必须绑定精确 SHA-256。

## Spawn

`pi_context` 绑定父 Codex 的 workspace；同一 MCP 连接后续可省略 scope，spawn 的 cwd 默认该目录，也可显式指定另一个现有目录的绝对路径。首次 `pi_spawn_agent` 带 cwd 且尚无绑定时，MCP adapter 会自动打开/恢复该目录的 scope（等价于先调 `pi_context`）；CLI 仍需显式 `--scope`。子代理指令始终读取 scope 目录的 `SUBAGENT-PI.md`，与子代理 cwd 无关；新建与唤醒/respawn 重新读取。新的 MCP 连接不会按 cwd 猜测旧 scope，需显式恢复。guard 启动插件自己的 Node SDK 入口，它加载所选 Pi 安装旁的 SDK。SessionManager 创建独立持久会话，后续恢复沿用同一路径。

默认最多 4 个 resident agent，每个 scope 最多 16 个历史 agent。resident 上限满时自动 park 最久未活动的已结算 idle agent：无活动 run、无待答问题、generation 一致才入选，走核验过的 interrupt 路径并保留会话；cleanup 非 verified 时记为 orphaned 并尝试下一个候选，无可 park 候选才返回 capacity_exceeded。writer 独占同一或嵌套 cwd，跨 scope 检查；新 writer 可核验并卸载已结算、无问题的冲突 writer，活动或未核验的 writer 仍阻止准入。parent Codex 不受此锁约束。空闲 resident 默认 1800 秒后卸载；专用 idle 时间戳在结算或空闲消息写入时更新，读取和后台遥测不会续期，未 ACK 的持久结果不阻止卸载。

Pi 不会获得父 Codex 的对话历史。把任务所需的上下文显式写入 task；继承 skills/MCP 不等于继承对话或授权。按任务需要简短说明以下内容即可，不要求固定格式，也不自动注入提示：

```text
目标：修复底栏 Git 增删统计。
已知事实：连续编辑会累计重复计数；入口 src/git-changes.ts。重命名处理是否有问题仍需核实。
范围与授权：可修改该模块、相关测试和必要文档；保留其他工作区改动，不提交、推送或安装。
验收：统计当前相对 HEAD 的差异，覆盖撤销和部分提交；先跑相关测试。
交付：说明根因、修改和实际验证结果；遇到超出授权范围的决定时向父代理提问。
```

复用 agent 时，它保留自己的 Pi 历史；后续 message 只需补充新任务、变化的事实和边界，不能假定它看到了父会话的新消息。补充当前任务用 pi_send_message；pi_followup_task 在活动任务内接入同一 run，空闲时创建新 run。需要独立排队的任务仍可通过 CLI follow-up。

## Steering 与 follow-up

`mode="steer"` 仅用于活动任务：输入加入插件队列，等当前 SDK 调用结束后作为同一 run 的续跑执行。它不再插入正在进行的 Pi 工具循环。不同扩展输入和外部 steer 统一按接受顺序消费；不模拟 Pi 交互式 steer 的抢先优先级。普通 Pi 的行为不变。

steer 回执的 execution=after_current_sdk_call 表示调度方式，不是已经执行，也不保证失败或中断后仍会消费。delivery=queued 仅表示接受。consumed 来自 user message_end 的文本 FIFO 匹配，不证明模型理解或服从。未消费输入在任务终态标为 not_consumed；响应不确定可为 unknown，禁止自动重发。idle steer 返回 agent_idle，不隐式启动或恢复。需要立即停止时应显式 close；需要替换任务时用 interrupt=true，中断不能撤销已有副作用。

`mode="follow_up"` 由 daemon 持久排队，每项有独立 run_id，前一个任务及其续跑全部结束后才开始。`mode="send"` 只用于 idle agent。

## V2 消息与任务

`pi_send_message` 在活动 SDK prompt 中使用公开 native steering，进入下一次模型调用；SDK 尚在主输入预处理时，先作为同一任务的有序续跑。空闲时调用公开 sendCustomMessage 写入 history，不启动模型、不创建 run。`pi_followup_task` 活动时与消息相同，空闲时创建新 run。两者都可自动加载已核验卸载的 session，并保留有效 model/thinking。接受不证明消费，诊断回执仍由 inspect 返回。

## Interrupt

`pi_interrupt_agent` 和 CLI interrupt 是软任务中断：取消 daemon 后继任务、插件输入、SDK queues 和待答 UI，调用公开 abort，并等待完整 managed task（包括原生输入和 hook 预处理）退出。合作式中断保留 PID/generation；空闲或已卸载 agent 返回原状态、不重新加载。返回 previous_status、runtime_retained，硬清理 fallback 另有 forced=true。原生 SDK 瞬时 idle 不能作为任务退出证明。

SDK 不在 streaming、原生预处理尚未退出，或任务曾排入无法用公开 API 清除的 nextTurn custom 输入时，直接要求硬清理（nextTurn 是否已消费无法可靠判断，采取保守处理）。其他 abort 无法在 rpc_timeout_seconds 内确认任务退出时，也改为终止并核验受管进程组；清理 unknown 时保留 orphaned 并拒绝自动恢复。中断不撤销已发生的文件或外部副作用，也不自动重放旧任务。软中断后的空闲时间从退出确认开始计时。

## Close 与 respawn

普通 MCP discovery 只广告 10 个日常工具（含软 interrupt）；context、close、respawn、legacy send 保留为显式管理调用和 CLI 操作。close、容量/闲置卸载、静默超时和 shutdown 仍使用先 TERM 后 KILL 的核验清理。底层 legacy interrupt RPC 保留硬停止语义；CLI interrupt 映射到 soft_interrupt。`pi_send_input(interrupt=true, message=...)` 保留显式硬替换语义，只有 cleanup verified 后才启动新 generation。

respawn 是幂等的确保加载：worker 仍存活时不改变任何状态并返回当前 state（already_running；带 message 会替换活 writer，因此明确拒绝并指向 pi_send_input），需要启动时才要求旧 writer 已消失、退出结算已完成且会话存在；agent_id 不变、generation 增加。自然退出尚在结算时明确返回 worker_alive，不在内部自动重试。进程退出后最多等待 2 秒读取末尾事件；即使后代仍持有输出管道，旧 run 和队列也会结算，残留进程组的 cleanup 仍如实为 unknown。所有停止路径在允许换代前将旧队列取消；旧完成回调既不能改写新进程，也不能在死进程上推进队列。可附加新消息，不附加则恢复为 idle。CLI resume 是其别名。dormant/closed 且 cleanup verified 的 agent 由 send/follow_up 直接唤醒（记录 auto_wake 事件，generation 增加）；orphaned、crashed、tainted 与 cleanup 未核验时仍要求显式 respawn，绝不静默启动。

受管会话不允许扩展切换、fork、reload 或导航到其他 session；关闭/恢复是唯一替换入口。Pi 配置、技能、provider、工具和普通生命周期扩展仍加载，TUI 专用界面按 headless 模式处理。

## 父代理关注与回答

`pi_wait_agent` 用 daemon 的事件条件等待，不轮询进度。默认最多等 10 分钟，单次最多 1 小时；timeout_seconds 以秒为单位（默认 600、最大 3600），是上限，不是固定延迟，0 只检查当前状态。默认 any 在任一任务完成时立即返回；all 等所有正常完成，但失败、崩溃、中断、取消、模型静默超时或需要输入仍立即返回。取消等待只断开这次读取，不停止子代理。显式设置更小的 max_wait_seconds 会限制默认值和允许的上限；等待超时与模型静默检测无关；任务没有总时长上限，工具执行和父代理问答不计模型静默时间。

`questions` 带 agent_id、run_id、问题 id、正文和选择项，供 `pi_answer_agent` 使用；模型可调用仅在受管子进程注册的 `ask_parent`，暂停工具执行直到显式回答。回答不会自动批准其他请求。标准安装使用 Codex 原生清单，为本插件设置 3630 秒 MCP 超时，覆盖一小时等待及传输余量；其他 MCP 客户端或手动使用通用清单时，仍需保证外层工具超时足够长。

wait 的每个终态结果最多预读 2 KiB，整页文本共用 8 KiB 预算；`has_more` 时从 next_offset 调 result。读结果不 ack，仍需精确 hash 确认。result 仅返回 run 身份/状态/非空错误、正文、单个 result_sha256、分页、acknowledged 和截断标记；不回显 offset，不重复整数 ack。时间戳、用量和结果路径在 inspect(detail=full)，包括空闲 agent 的最近一次任务。list 的 outstanding 保留未确认任务身份、状态、total/omitted；不返回 revision。

IPC 的连接、请求写入和响应读取共用调用预算；已写出结果后的交付回执最多等 1 秒，连接关闭最多再等 1 秒，不会因回执失败重复返回结果。daemon 响应写入和交付确认共用 10 秒预算。MCP stdout 非阻塞串行写入，单帧最多等 45 秒；半帧输出被取消或写失败时关闭该连接，避免后续 JSON 拼接损坏。上述传输失败不取消后台任务，不自动重发操作。

父会话身份来自 Codex 每次 MCP 调用的 `_meta.threadId`（CLI 则读自身 CODEX_THREAD_ID），不接受模型参数指定父代理。scope 默认值按父会话隔离；绑定后不能静默改指另一个父会话。其他会话仍可用显式 scope 读取旧结果，创建新任务需使用自己的 scope。list 的 parent_notifications 只返回 enabled，以及存在 unknown/recall_failed 时的 failed 数量。完整绑定和最近投递状态在 inspect(detail=full) 或显式管理 context 返回。

完成、失败、崩溃、取消、中断、超时或问题产生持久通知；插件调用 `codex queue --thread … --message …`，由原 Codex 进程读取队列。父会话已完成回合但仍加载时会自动开启后续回合；忙碌时等到空闲，跨进程检查通常约 10 秒。不会强制打断正在工作的父代理，也不会复活已经关闭或明确中断的父会话。通知只带任务定位和事件类型，明确标为自动子代理事件，不构成用户授权。

同一父会话对指定 run 的主动 wait 优先：等待期间暂缓对应通知；发送任务实际启动前再次检查 wait/消费状态，防止使用调度时的旧快照。若事件先前已经入队，必须在写出 wait 结果前完成该回执的撤回（或确认已经离队）；发送尚未取得回执时也等待该交接。MCP/CLI 写出响应后通过 IPC 回执把该响应中的事件持久标为 observed，不再排队唤醒。取消、断连、写出失败或 10 秒内未收到交付回执时恢复尚未消费事件的通知资格；若旧入队项已成功撤回，可创建新的自动唤醒。observed 只证明适配器已写出响应，不代表模型已处理，也不等于结果 ack；另一父会话的读取不能抑制原父会话通知。问题和终态分别记录，收到问题不会隐藏后续完成。正常使用始终等待明确的剩余 run_ids；未指定时仍返回未 ack 的任务，包括此前已读的终态。

queued 只是 Codex 入队回执。结果被精确 hash ack、同一父会话准备交付 wait 响应、或待答问题失效后，插件会用绑定的 CODEX_HOME 调用 Codex 现有的 `thread/queue/delete` 接口，只撤回该通知回执对应的消息 ID，不匹配内容、不清空队列，也不修改宿主代码或数据库。交付意图单独持久化；若此时还在发送，收到入队回执后继续撤回。wait 交接最多额外等 8 秒；未完成或失败时返回 notification_handoff_pending/notification_handoff_failed，不输出结果、不标为已消费。明确回执的撤回失败可再次 wait 重试，不确定的入队永不盲重发。仅读 result/list 不撤回通知。wait 交付不等于结果 ack。

通知状态包括 pending/sending/observed/queued/failed/unknown/superseded，以及 recalling/recalled/delivered/recall_failed。recalled 表示宿主确认删除；delivered 表示消息已不在队列（可能已消费或被手动删除），不能擦除已显示的消息。撤回接口缺失、超时或返回无效数据时记录 recall_failed，迟到通知仍可能出现，应忽略已处理事件。ack 最多等待撤回 20 秒，仍未结束或失败时额外返回 `notification_recall=pending/failed`；结果确认本身保持有效，文件不删除。单次撤回最多 10 秒，另有有界子进程清理。

独立父会话最多四路投递，每个父会话串行；先处理待撤回通知，再处理待发问题和终态，单次发送最多 30 秒。未发送的问题若已回答、结果若已 ack，则标为 superseded。提交超时、异常退出或发送中的 daemon 重启记为 unknown，绝不盲重发；没有可信入队回执就无法安全撤回。重启只重试被中断的确切 ID 删除，并清理旧版本遗留的已 ack 排队通知；未消费的交接被中断后，旧项成功撤回才恢复自动唤醒，已消费事件不重发。无可信父身份时仍以 wait/list 收取结果。

此机制要求 Codex 支持 queue，撤回另需 app-server 的 `thread/queue/delete`（均已用本地模拟模型验证）。使用绑定时的本地 CODEX_HOME；远程会话不在本机消息存储中时不能据此承诺唤醒。未修改 Pi/Codex 宿主，也不模拟键盘或重启用户会话。

等待接口不再接受 timeout_ms / --timeout-ms。当前 IPC 协议为 v3；协议升级后需在旧任务清理完成时正常重启插件 daemon/MCP 连接，混用旧客户端或旧 daemon 会明确返回 version_mismatch，不自动停止用户进程。
