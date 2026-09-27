# Architecture

## 总体

```text
Codex -> MCP stdio adapter --+
                            +-> Unix socket -> daemon -> guard -> plugin SDK child -> Pi SDK
Human/script -> CLI --------+                   |
                                                +-> SQLite + session/result files
```

Python 3.11+ 与 Node 标准库，加载用户已安装的 Pi SDK，无额外模型推理。MCP 只实现所广告的 stdio tools 子集：initialize、ping、tools/list、tools/call、取消通知。未广告 resources、prompts、sampling、HTTP transport 或 MCP Tasks。

支持协商的协议日期为 2025-06-18、2025-03-26、2024-11-05。更新客户端提出其他版本时返回一个受支持的协议版本，客户端决定是否继续。没有伪装成完整 MCP SDK。

## 核心模块

| 边界 | 所有者 |
| --- | --- |
| CLI、MCP → 本地 IPC | `cli.py` / `mcp.py` 共用 `schema.py`、`client.py`；`daemon.py` 持有单实例锁和断线语义 |
| agent/run 状态 | `runtime.py` 负责锁、启动协调、终态与队列推进；prompt/steer 共用投递入口，RPC 回执不覆盖已经观察到的消费 |
| 持久状态与读取 | `store.py`：SQLite WAL/FULL、幂等请求、结果事务与通知记录；`views.py` 只依赖账本、只读 Worker 查询与等待条件，按 scope 批量读取并渲染有界快照 |
| 子进程 | `worker.py`：JSONL、启动交接与 readiness；`worker_guard.py`：session 租约；`ownership()` 统一判断退出/恢复/清理 |
| 启动配置与继承 | `config.py` 生成 argv；`binding.py` 持有 scope 环境并重建启动计划；`inheritance.py` 解析来源与技能，CLI/MCP adapter 捕获父身份 |
| 父代理通知 | `parent.py` 持有 wait 保留、并发投递和关闭状态；独立的 queue 发送函数只处理外部 I/O，未知结果不重试 |
| MCP 配置规则 | `mcp_config.py`：字段兼容、工具权限、超时与凭证引用解析；不启动进程、不读取环境 |
| MCP 扩展 | `extensions/codex-mcp-bridge.ts`：私有管道 bootstrap、工具授权、连接生命周期与 readiness |
| MCP wire | `extensions/mcp/connection.ts`：共享协议、catalog 与 header schema；`stdio.ts` / `http.ts` 分别拥有进程和 HTTP 交换 |
| Pi SDK | `runtime/pi-sdk.mjs`：资源、公开 action、UI 与协议；`task-queue.mjs`：串行输入与异步归属；`protocol-output.mjs`：有界输出 |
| 受管 Pi 上下文 | `managed-context.ts` 保留 Pi 全局指令，启动时读取父 scope 的 `SUBAGENT-PI.md`；`managed-surface.ts` 按来源限制内建工具 |

机制模块不反向依赖 Runtime；MCP wire 不依赖 Pi 扩展 API。每个连接拥有自己的配置、包含完整性标记的 catalog 快照与未完成请求；HTTP 的 JSON/SSE 共用有界读流与关闭路径。SDK 队列将输入和其字节数放在同一项，任务归属由 AsyncLocalStorage 保持。HTTP 与 stdio 保留各自的协商/降级规则；工具调用失败后均不自动重放。运行设置只在受管 SDK 子进程内存中修改，不改 Pi 宿主。

## 数据结构

scopes、agents、runs、requests、receipts、events 是独立表。执行终态不等于 ack。agent generation 用于过滤旧实例事件。操作 request_id 在 scope 内唯一，参数不同不能重用。

账本 schema 版本由 `store.py` 的 `MIGRATIONS` 注册表按序号递进升级（当前 5）；比当前版本更新的数据库直接拒绝启动，不猜测、不降级。scopes.base_env 只保存非密级的基础环境键（PATH/HOME 等），使 worker 在 daemon 重启后仍能启动；其余绑定值只存在于内存。

最终结果文件先原子写+fsync，再提交 terminal 数据库记录。后台事件是有界规范化投影，不反复保存 streaming partial 的不断增长全文。原始 Pi session 由 Pi 自己管理，不自行修改其消息树。

## 服务端状态与主模型记忆

账本不依赖主模型记住全部 agent。未确认任务通过 list/outstanding 可恢复，wait 仅关注结果、失败、停止与输入问题，并返回有界结果与问题正文；all 模式遇到异常也提前返回。parent.py 将已绑定父会话的终态/问题投递到 Codex 官方消息队列；通知账本与任务终态同事务持久化，等待输入事件同样去重记录。父会话继续由原 Codex 进程拥有，不创建 competing resume，也不修改宿主。

工具 surface 是 11 个专用 MCP 工具（停止统一使用 pi_close_agent，旧 interrupt 调用与 CLI 仍兼容），避免复用 Codex 保留的 collaboration 名称。schema 不随当前 agent 列表变化。是否 deferred 取决于 Codex，不由 server 宣称。

## 安全与限制

作用域隔离是防串会话的正确性约束。Unix socket mode 0600、父目录0700、同 UID peer 检查不是针对同一用户恶意 worker 的安全沙箱。Pi 仍可能执行任意该用户能执行的 bash；reader 工具策略也不能限制路径到 cwd。

文件锁仅管理插件自己的 session。不会自动注入 Codex 沙箱、继承 approval policy 或替用户批准插件请求。审查结果不构成发布授权。

进程组检查无法绝对证明主动 daemonize 的任意后代均已退出。原进程身份未知时保守阻止恢复，而不对一个猜测 PID 发信号。

## 有意不做

没有 hooks、native /agents、自动 mailbox、自动review、scheduler、递归 fan-out、worktree 管理、多 harness backend、fork/clone、自动预算裁决、无限 transcript dump。

这一版不是 nicobailon/pi-subagents 的 fork，也没有把它作为隐含安装依赖。借鉴其控制回执、session 独占、进程终态与有界观察原则，但实现针对独立 Pi SDK 子进程 和无 hooks 的 Codex 宿主。
