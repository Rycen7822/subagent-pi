# Recovery

## MCP 断线

MCP adapter 只是客户端。断开、Codex 退出、shell 等待取消不停止 daemon 或 Pi。重新连接后使用相同 scope 和 agent/run IDs。用 CLI 检查：

```bash
subagent-pi scope list
subagent-pi list --scope SCOPE_ID
subagent-pi inspect AGENT_ID --scope SCOPE_ID --detail full
```

## 幂等和不确定结果

所有 mutation 使用稳定 request_id；数据库记录操作和参数摘要。同一 key、相同参数重试返回历史回执；相同 key 改参数报 idempotency_conflict。历史回执标 replayed，不代表里面的旧 state 是当前实时状态。

若 daemon 在操作执行中崩溃，只有 pending 没有 durable reply，返回 request_uncertain。不自动重放。先查询 scope/agent/result，以及 Pi session 和代码差异，再决定采用新请求。

CLI 未指定 --request-id 会自行生成并返回 ID；需要可靠跨进程重试的脚本应从一开始显式传入 ID。MCP 则要求模型显式提供。没有实现或宣称“跨进程任意副作用 exactly once”。

## Daemon 崩溃

新 daemon 不能重新附着旧匿名 stdin/stdout。它检查 owner.json、guard/Pi PID、Linux boot ID 与启动 tick。运行中 run 标为 crashed，queued run 标为 cancelled；这不是断言旧进程已经停止。

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

没有形成持久化 session 的首次启动失败，可能不可恢复。显式新建 agent 即可，不伪造旧上下文。恢复后的新 message 是一项新 run，有自己的总时限；没有额外 token/费用硬预算框架。

## 未处理结果

结果先 fsync 写入，再用 SQLite 事务保存 terminal 状态和结果 hash。结果读取不会 ack。父会话中断后，未确认结果仍在 outstanding 集合。

极端故障中，artifact 已写但事务未提交可能留下孤立文件；它不会被当作正常完成的证据。原始 Pi session 可能包含更多部分信息。不要把恢复时的空结果解释为子代理从未产生副作用。
