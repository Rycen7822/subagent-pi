# Troubleshooting

## Doctor

```bash
subagent-pi doctor
subagent-pi daemon status
subagent-pi scope list
```

doctor 不调用模型，只检查本地程序路径、配置和运行时信息。它不验证 provider 登录有效性或当前模型额度，也不伪报 Codex plugin load 成功。

## Pi not found

安装时使用 `--pi "$(command -v pi)"`，或在状态目录 config.toml 设置 pi_command。对于 nvm/npm 多版本环境，daemon 的 PATH 可能来自启动它的宿主，与当前终端不同。优先使用绝对路径。

改变 PI_AGENTS_PI 或配置后要重启 daemon；已经启动的 daemon 不会观察新终端环境变化。

## 自定义模型不可用

默认禁用所有 ambient extensions，避免无关工具和递归子代理。如果 provider 由扩展注册，创建显式 profile 并添加那一个扩展；不要直接打开全部个人插件。认证、代理、模型支持仍由 Pi 负责。

## 启动失败或没有响应

检查 `$PI_AGENTS_HOME/daemon.log` 及 `agents/AGENT_ID/stderr.log`。JSONL stdout 只容纳协议；扩展往 stdout 写其他内容会被记录为 protocol_warning。超过 8 MiB 的一条事件会导致停止，而不是无限占用内存。

初始启动等待与正常控制 RPC 分开配置。出现 rpc_timeout 时，不能推断任务没有执行；查询账本并使用相同 request_id。磁盘很忙或首次启动 Pi 很慢时适度增加 startup_timeout_seconds。

Pi 的项目 trust、环境初始化或扩展装载行为可能影响 RPC 启动。本插件不绕过它们；先在同目录确认 Pi 自己能正常运行。

## Codex 看不到工具

插件安装后开始新会话；确认没有同时通过全局 MCP 又注册同一个服务。使用 `/mcp` 检查连接，并实际尝试 pi_context。deferred/tool-search 能否发现属于 Codex 版本和 provider 行为，不是本插件保证。

安装后的 MCP 配置是绝对路径。如果移动或删除安装源目录，Codex 缓存中的配置可能仍指向旧位置，需要重新安装。CLI 被移除不一定影响已经使用绝对 Python 路径的 MCP，但不能依赖残留环境。

## wait 经常立即返回

检查是否有已完成但未确认的 run。它们被故意保留。读取并确认后再等待其他任务，或精确传入仍运行的 run_ids。

只有进度而没有终态时，wait 不应提前返回。需要输入的 Pi 请求会返回 needs_input，使用 pi_answer_agent 明确回答，不自动批准。

## writer conflict

同一/嵌套 cwd 的另一个受管理 writer 仍驻留。只读审查选 access=read；写任务可 send 给原 agent、关闭已闲置 writer 后新建，或由用户准备互不重叠的工作目录。不能仅改 name 绕过限制。

## Permission denied / sandbox

Unix socket 与数据目录属于同一 OS 用户。Codex 宿主若阻止访问 socket 或启动进程，需要按宿主批准策略处理。不要自动关闭沙箱或使用提权参数。本插件不会复制 Codex 权限到 Pi，部署者应明确选择能接受的外部 worker 权限边界。

## 实际模型 smoke test

`scripts/live_smoke.py --allow-model-call` 会建立临时测试目录，启动一次 Pi reader 并读一个 marker 文件，可能消耗额度。它不修改真实 repo，也不验证 Codex UI；测试后会关闭 worker。运行失败时查看该测试 scope，而不是无界重试。
