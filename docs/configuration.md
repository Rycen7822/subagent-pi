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
max_wait_seconds = 3600
event_max_count_per_agent = 20000
```

`pi_command` 是 argv 数组，不经 shell。指向官方 Pi 可执行文件（可为符号链接）；插件据此定位 SDK，再启动自己的 Node 入口。任意 shell 启动器或原版 Pi RPC 服务不等价于 SDK transport，会在握手时拒绝。不要在这里写管道或命令拼接字符串。安装 `--pi` 仅在 config.toml 不存在时写入路径，不覆盖已有配置。

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
ambient_extensions = true   # 默认；false 则隔离 Pi 自身的 extensions
ambient_skills = true       # 默认；false 则隔离 Pi 自身的 skills
extensions = []
skills = []
tools = ["read", "bash", "edit", "write", "grep", "find", "ls"]
# model = "provider/model-id"
# provider = "provider"
# thinking = "medium"
```

默认不硬编码任何模型。首先使用 spawn 显式 model，其次 profile model，最后由 Pi 自行选择；启动完成后保存 Pi 报告的 provider/model ID，后续恢复继续使用。`thinking` 同样支持 spawn / CLI `--thinking` 覆盖 profile；未指定时由 Pi 的模型默认/全局设置决定。插件用实际 session.getAvailableThinkingLevels() 校验显式等级，不支持则在提交任务前报错并列出可用项；不会静默钳制。spawn/inspect/list 返回 thinking 与 available_thinking，恢复固定已选等级。它不会固定 API 服务端模型权重，也不会锁定可执行文件的全部依赖字节。

受管子代理默认就是一个正常 Pi 会话：Pi 自身的全局/项目 extensions、packages、skills、prompt templates、themes、settings 与上下文文件全部照常加载，然后才在其上继承 Codex 的 skills 与 MCP。Pi 用来定位自己配置目录的 `PI_CODING_AGENT_DIR` 会随 scope 绑定（与 `HOME` 同类的非秘密路径）：CLI/MCP 客户端进程里的取值经 scope 快照传到子进程，daemon 重启后仍按记录恢复；未设置时保持 Pi 默认（`$HOME/.pi/agent`），profile 的 `[profiles.x.env] PI_CODING_AGENT_DIR` 仍然优先。`ambient_extensions` / `ambient_skills` 默认 `true`，是**显式退出开关**（设为 `false` 会重新加上 `--no-extensions` / `--no-skills`）。

`tools` 决定子代理的 **builtin** 工具面，由随插件发布的 `extensions/managed-surface.ts` 在 session_start 按 Pi 报告的来源（`sourceInfo.path = "<builtin:NAME>"`）精确激活/停用：profile 列出的 builtin 生效，未列出的 builtin（包括本插件还不认识的）停用。刻意不使用 `--tools` **也不用** `--exclude-tools`：两者都按**工具名**过滤同一份注册表，会连带剔除 Pi 扩展注册的同名工具（例如扩展用自己的 `bash` 覆盖 builtin）。只有真正限制 builtin 的 profile 才会加载该扩展；扩展文件缺失时该 profile 直接拒绝启动，而不是放出无约束的子代理。扩展/自定义工具保持 Pi 自身判定；插件仅在受管子进程增加 ask_parent，用于向父代理提出阻塞问题。

限制 builtin 的 profile 不会只凭 argv 就宣称生效：扩展会在改动后回读 Pi 的实时注册表并把结果写到 stderr（`subagent-pi-surface applied ...`），daemon 记录为 `tool_surface` 事件，并在缺失或与期望不符时让本次启动失败（`tool_surface_unavailable` / `tool_surface_unapplied`）。

`access=read` 的含义：builtin 限制为 read/grep/find/ls（默认 reader profile），继承的 MCP 只暴露 readOnly 工具且每次调用确认，写者互斥规则不变。但它**不再**声称"只具备只读工具"、也不再拒绝加载扩展：Pi 自己的 extensions/skills/settings 按用户配置原样生效，扩展就是会在子进程里运行的代码，其工具与能力不受本插件约束。这些限制是受管工具面策略，不是 OS 沙箱。

需要自定义 provider 扩展时，配置一个明确 profile：

```toml
[profiles.custom]
extensions = ["/absolute/path/provider.ts"]
ambient_extensions = true    # 默认；false 则关闭 Pi 自身 extensions 发现
ambient_skills = true        # 默认；false 则关闭 Pi 自身 skills 发现
skills = []
tools = ["read", "bash", "edit", "write", "grep", "find", "ls"]
model = "provider/model-id"
```

该 profile 使用 access=write；read profile 现在同样可以加载扩展，只是工具面按上面的规则受限，不再有 read+extension 的硬拒绝。

加载 ambient extensions/skills 意味着个人/项目侧代码和行为会进入子进程；它可能启动额外 MCP、改变 provider、进行递归委托。需要隔离时把 `ambient_extensions`/`ambient_skills` 设为 `false`。`PI_AGENTS_MANAGED_CHILD=1` 只是协作标志，不是恶意子进程的权限屏障。

profile 支持 `[profiles.NAME.env]` 的字符串环境覆盖。值在每次启动时从配置重读，launch snapshot 只记录变量名；不要把凭据写进版本控制。

## 三种时限

startup_timeout_seconds 仅控制初始 Pi handshake；rpc_timeout_seconds 控制控制命令回执；default_run_timeout_seconds 控制一项运行的总时限。

wait 的 timeout_seconds 以秒计，只限制调用等待，不停止任务。标准 Codex 安装在本插件清单中设置 3630 秒工具超时；其他 MCP 客户端或手工安装需要自行保证外层超时足够长。插件不改全局配置或批准策略。

客户端对 IPC 调用的等待按 daemon 自身预算推导：wait 是 max(45s, timeout_seconds+10s)，其他普通操作为 45s；启动/回收类操作（spawn、respawn、close、interrupt）覆盖 daemon 的启动预算（由 startup_timeout_seconds 推导，默认约 90s），因为一次健康但缓慢的启动不应被报成失败。spawn 的 timeout_seconds 是**运行**时限，不影响该调用本身的等待。

模型 HTTP 请求时限、代理、认证和模型流解析交由 Pi。调整 Pi 自身的 httpIdleTimeoutMs 时，不要把它与本插件的 wait 或总运行时限混淆。没有输出不自动代表死进程，插件不以“多久没有 token”作为杀进程条件。

## 资源与保留

单条 Pi JSONL 帧最大 8 MiB，超出视为协议/资源错误并停止该 worker。每个 agent 保留最近约 20,000 条规范化事件，最多每 256 条批量裁剪；每条事件内容有界。最终结果最多保存 1 MiB 文本快照，发生截断会返回 result_truncated=true；原始 Pi session 仍由 Pi 保留。stderr 最多保留 512 KiB；不是无限增长日志。

结果、session、请求账本默认不自动 GC，防止清掉未处理证据。长期使用应人工归档已收尾的整个 scope 数据；本版不提供自动清理命令。磁盘耗尽是一个操作失败，不是安全完成。
