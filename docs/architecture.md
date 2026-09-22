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

common.py：JSONL、UTF-8 边界、私有目录、原子写、Linux 进程身份。
store.py：SQLite WAL/FULL、请求幂等、结果事务、事件索引。
config.py：用户配置、profile、确定化启动 argv。
worker_guard.py：session writer 租约和双进程身份。
runtime.py：agent/run 状态机与 op 路由，账本状态迁移的唯一所有者。
worker.py：单个 SDK 子进程的 JSONL 通道、启动交接 fd、回执读取与进程组归属（`ownership` 是 reaping/恢复/terminate 共用的唯一判定）。
binding.py：scope 绑定、子进程环境、每次启动重建的 Codex 继承计划。
views.py：brief/inspect/result/wait 的有界只读投影，不修改状态、不确认结果。
daemon.py / client.py：本地 IPC、单实例锁、断线语义。
schema.py：MCP 与 IPC 共用 schema 和校验。
mcp.py / cli.py：两个薄入口，不各自维护任务状态。

依赖方向固定为 runtime → {worker, binding, views} → {common, store, inheritance}；机制模块不反向引用 runtime，因此它们可脱离 Runtime 实例单独测试。

runtime/pi-sdk.mjs：资源加载、公开 SDK action 绑定、headless UI 与 JSONL 协议。
runtime/task-queue.mjs：单个任务的串行输入、异步归属和完成；不复制 Pi 的 agent loop。

## 数据结构

scopes、agents、runs、requests、receipts、events 是独立表。执行终态不等于 ack。agent generation 用于过滤旧实例事件。操作 request_id 在 scope 内唯一，参数不同不能重用。

账本 schema 版本由 `store.py` 的 `MIGRATIONS` 注册表按序号递进升级（当前 3）；比当前版本更新的数据库直接拒绝启动，不猜测、不降级。scopes.base_env 只保存非密级的基础环境键（PATH/HOME 等），使 worker 在 daemon 重启后仍能启动；其余绑定值只存在于内存。

最终结果文件先原子写+fsync，再提交 terminal 数据库记录。后台事件是有界规范化投影，不反复保存 streaming partial 的不断增长全文。原始 Pi session 由 Pi 自己管理，不自行修改其消息树。

## 服务端状态与主模型记忆

账本不依赖主模型记住全部 agent。未确认任务通过 list/outstanding 可恢复，wait 仅关注有意义事件。没有 hook 或宿主事件注入；客户端彻底不再查询时不会被自动唤醒。

工具 surface 是 12 个专用 MCP 工具，避免复用 Codex 保留的 collaboration 名称。schema 不随当前 agent 列表变化。是否 deferred 取决于 Codex，不由 server 宣称。

## 安全与限制

作用域隔离是防串会话的正确性约束。Unix socket mode 0600、父目录0700、同 UID peer 检查不是针对同一用户恶意 worker 的安全沙箱。Pi 仍可能执行任意该用户能执行的 bash；reader 工具策略也不能限制路径到 cwd。

文件锁仅管理插件自己的 session。不会自动注入 Codex 沙箱、继承 approval policy 或替用户批准插件请求。审查结果不构成发布授权。

进程组检查无法绝对证明主动 daemonize 的任意后代均已退出。原进程身份未知时保守阻止恢复，而不对一个猜测 PID 发信号。

## 有意不做

没有 hooks、native /agents、自动 mailbox、自动review、scheduler、递归 fan-out、worktree 管理、多 harness backend、fork/clone、自动预算裁决、无限 transcript dump。

这一版不是 nicobailon/pi-subagents 的 fork，也没有把它作为隐含安装依赖。借鉴其控制回执、session 独占、进程终态与有界观察原则，但实现针对独立 Pi SDK 子进程 和无 hooks 的 Codex 宿主。
