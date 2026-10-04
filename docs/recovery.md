# Recovery

## MCP 断线

MCP adapter 只是客户端。断开、Codex 退出、shell 等待取消不停止 daemon 或 Pi。重新连接后使用相同 scope 和 agent/run IDs。用 CLI 检查：

```bash
subagent-pi scope list
subagent-pi list --scope SCOPE_ID
subagent-pi inspect AGENT_ID --scope SCOPE_ID --detail full
```

新的 MCP 连接显式传入原 scope ID；读取结果不要求恢复 Pi 进程。直接以相同 cwd 重新 spawn 不会自动找回旧 scope，显式 scope 也不会设置该连接的默认值。CLI scope open 与 MCP 连接默认值的区别见 [lifecycle.md](lifecycle.md#scope-与-worktree)。

daemon 重启后需要重新捕获环境引用时，由所属客户端使用管理 pi_context 重开原 scope，或执行 `subagent-pi scope open --cwd ORIGINAL_WORKSPACE --scope SCOPE_ID`。仅在 spawn 中传旧 scope 不会重采集环境。CLI 重开后仍需向 MCP 显式传 scope；来源/凭据边界见 [inheritance.md](inheritance.md)。

## 空闲卸载后的继续工作

已核验卸载的 agent（dormant/closed 且 cleanup=verified）保留 session 和结果。可直接给它后续任务，无需先 respawn：

```bash
subagent-pi followup-task AGENT_ID --scope SCOPE_ID \
  --message '继续检查索引刷新路径；先核实已完成的工作。' --request-id continue-index-1
```

pi_followup_task / followup-task 在空闲时唤醒并启动新 run，活动时加入当前 run；pi_send_message / send-message 在空闲时只写入 history，不启动模型。正常软中断后通常仍是 idle；若已卸载则按上述路径恢复。旧结果继续保留；原父会话成功读取后自动消费通知，仍可按 run ID 和 hash 复核正文。模型/推理深度沿用该 agent 已解析的选择。

## 幂等和不确定结果

带 request_id 的任务控制操作使用稳定 key；数据库记录操作和参数摘要。管理 scope_open / pi_context 没有 request_id，不使用这套重试账本。同一 key、相同参数重试返回历史回执；相同 key 改参数报 idempotency_conflict。历史回执标 replayed，不代表里面的旧 state 是当前实时状态。

若 daemon 在操作执行中崩溃，只有 pending 没有 durable reply，返回 request_uncertain。不自动重放。先查询 scope/agent/result，以及 Pi session 和代码差异，再决定采用新请求。

带 --request-id 的 CLI 操作未指定该参数时会自行生成并返回 ID；需要可靠跨进程重试的脚本应从一开始显式传入 ID。MCP 则要求模型显式提供。没有实现或宣称“跨进程任意副作用 exactly once”。

## Daemon 崩溃

新 daemon 不能重新附着旧匿名 stdin/stdout。它检查 owner.json、guard/Pi PID、Linux boot ID 与启动 tick。运行中 run 标为 crashed，queued run 标为 cancelled；这不是断言旧进程已经停止。

owner.json 缺失时，已启动过的 worker 一律按 orphaned/unknown 处理：死 PID 和空进程组无法排除 detached 工具后代。只有未启动过的行，或有效 owner 记录证明领导进程、进程组和后代清理完成，才允许 verified。正常 guard 使用 Linux subreaper 接收并回收 orphan 后代，记录 descendants_cleanup；guard 被强杀或该证据未知时拒绝自动恢复。重启、close 和 respawn 共用这一判据。

旧进程仍可确认活动时 agent 标为 orphaned。先用 close 验证身份并清理其进程组，再 respawn。没有活动旧进程且 session 可用时可以直接恢复。无法证明所有者状态时拒绝猜测。

```bash
subagent-pi close AGENT_ID --scope SCOPE_ID --request-id cleanup-old-instance
subagent-pi respawn AGENT_ID --scope SCOPE_ID --message '先检查之前的修改和测试状态，不重复执行不确定的操作'
```

## Session locks

每个受管理 agent 有独立私有 session 目录，guard 通过 flock 保证该 agent 的单一 writer；owner 记录同时保存 guard 与 Pi 的进程身份。锁范围只覆盖本插件管理的会话，不会阻止你在另一个终端绕过插件直接打开同一个 Pi session。

如果 guard 在创建 Pi 后、保存完整 owner 信息前死亡，spawning 标记会保守阻止再次恢复。此时不要自动删除锁或仅因 PID 失效就认定安全。人工确认没有旧 writer/其进程组后再处理旧 owner 记录，保留副本；拿不准时创建新的独立 agent 并给予明确的恢复说明。

对于 owner metadata 损坏、权限不足、无法验证的残余进程组，close 也可能返回 ownership_unknown。该状态需要人工干预，不提供猜测性的强制接管 API。

## Respawn 的边界

恢复保留逻辑 agent_id、已解析启动配置和 Pi session，generation 增加。不会无损续接正在执行的 shell、修复任何损坏的 Pi session、恢复服务端 KV cache，或自动重新提交已中断任务。

没有形成持久化 session 的首次启动失败，可能不可恢复。需要新工作时显式新建 agent，不伪造旧上下文。respawn 附带的新 message 创建新 run；任务没有总时限，模型静默仍受 idle_timeout_seconds 约束，工具执行和等待回答期间暂停检测。没有额外 token/费用硬预算框架。

## 未处理结果

结果先 fsync 写入，再用 SQLite 事务保存 terminal 状态和结果 hash。wait / result 成功输出后自动消费该终态关注；输出失败、取消或无交付回执时继续保留在 outstanding。list / inspect 不消费关注。已经交付的结果文件仍可按 run ID 重新读取。

通知处于 unknown 或 recall_failed 时，普通交付仍要求先结算通知；可直接只读结果，不改变未决状态：

```bash
subagent-pi result RUN_ID --scope SCOPE_ID --peek
```

--peek 不消费关注、不撤回或重放通知，也不意味着旧通知已安全处理。需要分页时继续带 --peek，使用返回的 run.id / next_offset。先用 inspect --detail full 查看通知错误，修正绑定或宿主问题后再处理正常交付；不要自动重试未知提交。旧 scope 的父绑定不会因修改安装清单而自动修复。

极端故障中，artifact 已写但事务未提交可能留下孤立文件；它不会被当作正常完成的证据。原始 Pi session 可能包含更多部分信息。不要把恢复时的空结果解释为子代理从未产生副作用。
