# Configuration

## 配置文件

默认路径：`~/.local/state/subagent-pi/config.toml`。`PI_AGENTS_HOME` 改变整个账本、配置、session、日志的根；`XDG_STATE_HOME` 影响默认位置。复制 `examples/config.toml` 可以查看全部选项。

修改配置后要 drain 并重启 daemon；已创建 agent 的 launch snapshot 不跟随配置变化，避免恢复时悄悄改变工具或模型。新配置应用于新 agent。

```toml
pi_command = ["/home/you/.local/bin/pi"]
max_resident_agents = 4
max_agents_per_scope = 16
rpc_timeout_seconds = 20
startup_timeout_seconds = 30
default_run_timeout_seconds = 1800
max_wait_seconds = 600
event_max_count_per_agent = 20000
```

`pi_command` 是 argv 数组，不经 shell。用于已有 Pi 可执行文件，也可以配置受信任的启动器。不要在这里写管道或命令拼接字符串。安装 `--pi` 仅在 config.toml 不存在时写入路径，不覆盖已有配置。

## Codex 继承（受管子代理）

```toml
[inheritance]
enabled = true   # 总开关（默认 true）
skills = true    # 继承 <codex_home>/skills
mcp = true       # 继承 config.toml 的 mcp_servers
# codex_home = "/abs/path"  # 显式受信任来源；缺省按 显式设置 → CODEX_HOME → ~/.codex 解析
```

来源在 scope 绑定时确定并持久化（非秘密字段）；显式 `codex_home` 或已设置的 `CODEX_HOME` 不存在时报错而非静默回退。环境变量的**值**只保存在 daemon 内存中，重启后需所属客户端重新绑定；子进程基础环境同样来自 scope 快照（不复制 daemon 全量环境），模型认证如依赖环境变量，需在 `child_env` 中显式授权（只写名字，值不落盘）。respawn 总是从原文件重建继承，不追加历史参数。诊断：`subagent-pi doctor --inheritance`。完整语义（冲突策略、只读子代理、审批模型、required 回执、取消的"结果未知"语义）见 `docs/inheritance.md`。

## Profiles

```toml
[profiles.default]
ambient_extensions = false
ambient_skills = false
extensions = []
skills = []
tools = ["read", "bash", "edit", "write", "grep", "find", "ls"]
# model = "provider/model-id"
# provider = "provider"
# thinking = "medium"
```

默认不硬编码任何模型。首先使用 spawn 显式 model，其次 profile model，最后由 Pi 自行选择；启动完成后保存 Pi 报告的 provider/model ID，后续恢复继续使用。它不会固定 API 服务端模型权重，也不会锁定可执行文件的全部依赖字节。

`reader` 默认只有 read/grep/find/ls。`access=read` 会剥离其他 builtin 并拒绝任何扩展加载，以免扩展重新引入写工具；这是工具面约束，不是 OS 沙箱。

需要自定义 provider 扩展时，配置一个明确 profile：

```toml
[profiles.custom]
extensions = ["/absolute/path/provider.ts"]
ambient_extensions = false
ambient_skills = false
skills = []
tools = ["read", "bash", "edit", "write", "grep", "find", "ls"]
model = "provider/model-id"
```

该 profile 使用 access=write；如果它只是 provider 扩展仍希望严格只读，本版不尝试证明扩展没有其他能力，因此仍保守拒绝 read+extension。

可以明确启用 ambient extensions/skills，但这会加载个人/项目侧代码和行为；它可能启动额外 MCP、改变 provider、进行递归委托。无扩展的默认 profile 不暴露 subagent 工具。`PI_AGENTS_MANAGED_CHILD=1` 只是协作标志，不是恶意子进程的权限屏障。

profile 支持 `[profiles.NAME.env]` 的字符串环境覆盖。它们会进入当前用户私有的 launch snapshot；不要把凭据写进版本控制。常规继承的环境凭据不复制进数据库。

## 三种时限

startup_timeout_seconds 仅控制初始 Pi handshake；rpc_timeout_seconds 控制控制命令回执；default_run_timeout_seconds 控制一项运行的总时限。

wait 的 timeout_ms 只是调用等待，不停止任务。MCP 客户端还有自己的超时，长 wait 须由使用者调整宿主配置；插件不自动改 Codex 的工具超时或批准策略。

客户端对 IPC 调用的等待按 daemon 自身预算推导：普通操作是 max(45s, timeout_ms+10s)；启动/回收类操作（spawn、respawn、close、interrupt）覆盖 daemon 的启动预算（由 startup_timeout_seconds 推导，默认约 90s），因为一次健康但缓慢的启动不应被报成失败。spawn 的 timeout_seconds 是**运行**时限，不影响该调用本身的等待。

模型 HTTP 请求时限、代理、认证和模型流解析交由 Pi。调整 Pi 自身的 httpIdleTimeoutMs 时，不要把它与本插件的 wait 或总运行时限混淆。没有输出不自动代表死进程，插件不以“多久没有 token”作为杀进程条件。

## 资源与保留

单条 Pi JSONL 帧最大 8 MiB，超出视为协议/资源错误并停止该 worker。每个 agent 保留最近约 20,000 条规范化事件，最多每 256 条批量裁剪；每条事件内容有界。最终结果最多保存 1 MiB 文本快照，发生截断会返回 result_truncated=true；原始 Pi session 仍由 Pi 保留。stderr 最多保留 512 KiB；不是无限增长日志。

结果、session、请求账本默认不自动 GC，防止清掉未处理证据。长期使用应人工归档已收尾的整个 scope 数据；本版不提供自动清理命令。磁盘耗尽是一个操作失败，不是安全完成。
