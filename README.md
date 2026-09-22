# Subagent Pi — Codex 插件

**MCP + 极薄 Skill + CLI + 一个持久化运行时。无 hooks。**

让 Codex 把本地 Pi coding agent 当成可控制的外部子代理：异步启动、运行中 steer、排队 follow-up、检查工作轨迹、中断、关闭、恢复原会话、收取结果。

这是完整源码版 `0.2.9`，针对 **Linux / WSL2，Python 3.11+**。运行时使用 Python 标准库、Node.js 22.19+ 和已安装 Pi 的 SDK，无额外 pip/npm 构建依赖；Pi 和 Codex 需要你已自行安装。它不进入 Codex 原生 `/agents`；父代理唤醒使用 Codex 官方消息队列，无需 hooks，适用范围见下文。

受管子进程使用 **原版 Pi 0.87.0 的 SDK**，无需修改或补丁化 Pi。插件拥有串行输入队列及任务完成协议；steer 在当前 SDK 调用完成后有序续跑，仍归属原任务。interrupt 终止并核验自己的子进程，保留 session 供恢复。正常交互式 Pi 不受影响，详见 `docs/lifecycle.md`。

受管子代理首先是一个正常 Pi 会话：Pi 自身的全局/项目 extensions、packages、skills、prompt templates、themes、settings 与上下文照常加载（SDK 的 settings 更新仅在子进程内存中保存）（`ambient_extensions`/`ambient_skills` 默认 `true`，可显式关掉）；Pi 的配置目录沿用打开 scope 的客户端进程里的 `PI_CODING_AGENT_DIR`（未设置即 Pi 默认 `$HOME/.pi/agent`，跨 daemon 重启保持）。在此之上按需继承你的 Codex 全局 skills（`~/.codex/skills`）与 MCP（`~/.codex/config.toml` 的 `mcp_servers`）：只读原文件、原位引用、内存中传递，不落盘、不迁移；普通 `pi` 不受影响。

同名 skills 由 Pi 自己在加载边界裁决：Pi 已加载的同名 skill 保留，Codex 版本不会被注册；每次 boot 都会把结果记进 `inheritance_skills` 事件（跳过的 Codex 路径 + 保留的 Pi 路径）。MCP 不按 server 名去重，因为 Pi 没有 MCP 体系、也不提供 server 注册表（只保证 `(server, tool)` 寻址：不同 server 的同名工具互不冲突）。`access=read` 只约束本插件控制的 builtin 与 MCP 暴露面，不声称"只具备只读工具"、也不是 OS 沙箱。详见 `docs/inheritance.md`。

## 安装

在 **WSL/Linux 终端**中解压，再执行：

```bash
cd subagent-pi
python3 --version
command -v pi
command -v codex
python3 scripts/install.py --pi "$(command -v pi)" --register
export PATH="$HOME/.local/bin:$PATH"
subagent-pi doctor
```

然后在 Codex 输入 `/plugins`，从 **Subagent Pi Local** 安装 **Subagent Pi**，并开始一个新会话。脚本注册的是本地 marketplace，不会替你点击插件安装或授权。

安装脚本不直接改写 `~/.codex/config.toml`，不修改 `~/.pi`，也不安装任何 hooks。`--register` 仅调用你现有的 `codex plugin marketplace add`。不支持该命令的版本可采用 `docs/getting-started.md` 的 MCP+Skill 手动回退方式。

建议在项目目录中启动：

```bash
cd /absolute/path/to/project
subagent-pi codex
```

该可选启动器为本次工作创建 scope，并通过环境变量交给 MCP；不是 hook，不向提示词注入状态。已有会话仍可直接使用插件，由 `pi_context` 指定 cwd（恢复时指定 scope）；同一 MCP 连接后续省略 scope，spawn 省略 cwd 时沿用作用域目录。

## 已实现

| 能力 | 方式 |
|---|---|
| 当前项目异步启动 | `pi_spawn_agent` / `subagent-pi spawn`，记录明确的绝对 cwd |
| 任务内有序续跑 | `pi_send_input(mode="steer")`，区分 queued / consumed / not_consumed / unknown |
| 后续任务 | `mode="follow_up"`，daemon 持久排队，每项拥有独立 run ID |
| 等待一个或多个任务 | `pi_wait_agent`，携带小结果与 hash；失败、停止、问题在 all 模式也立即返回 |
| 检查工作轨迹 | `pi_inspect_agent`，有界输出、增量 cursor、控制回执 |
| 中断 / 关闭 | `pi_close_agent` 取消排队工作，终止并核验受管理的进程组 |
| 恢复 / respawn | 保留 agent ID，增加 generation，重新加载已持久化 Pi session |
| 防重复执行 | scope + request_id + 参数摘要，持久保存已完成回执 |
| 结果交接 | 读取不等于确认；按 run ID + SHA-256 显式确认 |
| MCP / CLI 互通 | 同一个 daemon、同一个 SQLite 账本 |
| 父代理自动唤醒 | 完成、失败、停止、问题 → Codex 持久消息队列；pi_context/list 返回投递状态 |
| 子代理询问父代理 | 模型 `ask_parent` 或扩展 UI 请求 → wait 返回问题 → `pi_answer_agent` 显式回答 |
| 推理等级 | spawn `thinking` 按所选 Pi 模型校验；省略则继承 Pi 设置 |
| 文档按需读取 | `subagent-pi guide TOPIC --section SECTION` |

## 使用示例

直接对 Codex 说：

> 使用 Subagent Pi，在当前项目创建一个只读子代理检查缓存失效逻辑。继续你的其他工作，必要时读取增量轨迹；完成后读取结果再确认收尾。

CLI 对等示例：

```bash
# 此命令自行创建一个新 scope，并返回 scope / agent_id / run_id。
subagent-pi spawn --access read --task '检查缓存失效逻辑，只报告问题，不修改文件'

# 使用上一条返回的真实 ID：
subagent-pi inspect AGENT_ID --scope SCOPE_ID
subagent-pi steer AGENT_ID --scope SCOPE_ID --message '优先检查索引刷新路径'
subagent-pi wait RUN_ID --scope SCOPE_ID --timeout-ms 3600000
subagent-pi result RUN_ID --scope SCOPE_ID
subagent-pi ack RUN_ID --scope SCOPE_ID --sha256 RESULT_SHA256
subagent-pi close AGENT_ID --scope SCOPE_ID
```

`AGENT_ID` 等是示意占位符，不是可直接使用的 ID。脚本调用可用 `subagent-pi call OP --json -` 从 stdin 传 JSON，避免 shell 引号问题。

## 重要边界

- **Pi worker 不继承 Codex 的沙箱或逐工具批准机制。** 它使用当前 OS 用户和 Pi 自身权限。受管子代理默认加载 Pi 自身 extensions/skills（可用 profile 的 `ambient_extensions`/`ambient_skills` 显式关闭）；`access=read` 只把本插件控制的 builtin 限制为 read/grep/find/ls（由 `extensions/managed-surface.ts` 按 Pi 报告的来源应用、并回读验证后才算生效；扩展注册的同名工具不受影响）并收窄继承 MCP 的暴露面，Pi 自己的扩展、其工具与代码不受约束，因此这不是 OS 安全隔离。
- 不安装 hooks。pi_context 从 Codex 的调用元数据绑定父会话；任务结束或提问时，通过官方 codex queue 入队提醒。仍在运行且空闲的父会话会自动续跑，跨进程通常约 10 秒检查一次；忙碌时排队，不强制打断。没有父会话身份的客户端仍使用 wait。
- MCP 进程退出不终止 Pi。**daemon 崩溃后不能重新接回旧 stdin/stdout**；先确认并关闭遗留进程，再显式恢复。
- interrupt/close 会终止并验证受管理进程组；主动脱离该组的后代不受绝对保证。恢复需显式 respawn。
- 同一或嵌套 cwd 只允许一个受管理 writer；不能阻止 Codex 主 agent、编辑器或其他进程同时改文件。
- 没有自动重试已执行的 shell，没有自动 model fallback，没有工作流脚本引擎、自动 review、worktree 管理或 clone。
- 内部 SQLite/原始 Pi session/结果可能含敏感代码和文本。目录为当前用户私有；这不是对同一用户的恶意进程的安全边界。

## 验证

```bash
python3 scripts/validate_package.py
python3 -m unittest discover -s tests -v
```

默认测试使用确定性受管子进程和本地 MCP 服务。设置 `SUBAGENT_PI_LIVE_PI=1` 可运行原版 Pi 0.87 SDK 的隔离进程测试，使用离线 mock provider。命令和验证边界见 [测试指南](docs/testing.md)；离线通过不等于真实模型或 Codex 安装联调通过。

本机可进行一次**明确会调用模型、可能消耗额度**的实测：

```bash
python3 scripts/live_smoke.py --allow-model-call
```

## 文档

`docs/getting-started.md`：安装与首轮使用；`lifecycle.md`：操作语义；`configuration.md`：模型与配置；`recovery.md`：恢复；`troubleshooting.md`：诊断；`cli.md`：命令；`architecture.md`：实现；`testing.md`：测试；`sources.md`：协议和源码参考。
