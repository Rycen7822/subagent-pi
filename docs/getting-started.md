# Getting started

## 环境

本版支持 Linux / WSL2 和 Windows 原生，依赖 Python 3.11+、Node.js 22.19+、已安装并配置好模型/认证的 Pi ≥0.99.1，以及支持本地插件或 MCP 的 Codex CLI。状态目录使用对应系统的本地文件系统：WSL 使用 Linux 文件系统，Windows 使用本地 NTFS。文件锁和 SQLite WAL 不适合网络文件系统。

先确认：

```bash
python3 --version
node --version
pi --version
codex --version
```

插件从所选 Pi 可执行文件定位公开 SDK，再启动自己的受管入口，不修改 Pi 安装。具体 SDK 验证版本和测试边界见 [testing.md](testing.md)。日常 interrupt 取消任务并尽量保留进程；close 明确卸载；已结算进程默认空闲 30 分钟后卸载，持久会话和结果保留。

## 本地插件安装

从源码或解压后的完整插件目录执行：

```bash
python3 scripts/install.py --pi "$(command -v pi)" --register
export PATH="$HOME/.local/bin:$PATH"
```

默认安装目录：`~/.local/share/subagent-pi-marketplace/plugins/subagent-pi`。本地 marketplace 清单：`~/.local/share/subagent-pi-marketplace/.agents/plugins/marketplace.json`。CLI：`~/.local/bin/subagent-pi`。

源码包保留通用 `plugin.json` / `mcp.json`；安装到 Codex 时选择 `.codex-plugin/plugin.json` / `.mcp.json` 原生清单，并在安装副本移除优先级更高的通用 plugin.json。原生 MCP 配置固定解释器、入口和状态目录，透传 `CODEX_HOME` / `XDG_RUNTIME_DIR`，并设置 `tool_timeout_sec=3630`，覆盖一小时 wait；不改 Codex 全局配置或宿主源码。自定义 Codex 目录必须传到 adapter，否则父 thread ID 可能被绑定到错误目录；CLI 与 MCP 也需使用相同的 runtime 目录。

`--register` 只注册 marketplace。接着在 Codex `/plugins` 中从 Subagent Pi Local 安装 Subagent Pi，或使用当前支持该命令的 CLI：

```bash
codex plugin add subagent-pi@subagent-pi-local
```

完成后开启新会话，让 MCP 工具和 skill 使用同一安装版本。也可分开安装文件与注册 marketplace：

```bash
python3 scripts/install.py --pi /absolute/path/to/pi
codex plugin marketplace add "$HOME/.local/share/subagent-pi-marketplace"
```

安装脚本会创建 CLI 链接；复制源码或 git push 不会更新已有安装。模型和推理深度按 [configuration.md](configuration.md) 选择。

## Windows 原生安装

在 Windows 原生 PowerShell 中使用 Python 3.11+、Node.js 22.19+ 和官方 npm 安装的 Pi ≥0.99.1；Codex 也安装在 Windows。插件需要 Pi 的公开 SDK，单独的打包版 pi.exe 不保证附带 SDK。Pi 的 Bash 工具需要 Git for Windows；Git 安装在非默认目录时，旧 Pi SDK 需要在自己的 settings.json 中显式设置 `shellPath`（例如 `D:/app/Git/bin/bash.exe`），插件不会自动替换 shell；支持 PowerShell 工具的 Pi 版本也可按 Pi 官方配置选择它，插件不会替你修改 Pi 设置。

```powershell
python --version
node --version
pi --version
codex --version
python scripts/install.py --pi (Get-Command pi.cmd).Source --register
$env:Path = "$HOME\.local\bin;$env:Path"
subagent-pi.cmd doctor
```

默认目录与上文相同，`~` 展开为 Windows 用户目录。安装器为 Codex 固定 python.exe、Python 入口和状态目录；CLI 生成 `.cmd` / `.ps1`，无需创建符号链接。命令入口支持中文和空格路径。只对该启动器进程设置 PowerShell 执行策略，不修改用户执行策略。跨盘符或反斜杠路径按原生 Windows 路径传入；JSON/TOML 中使用正斜杠或正确转义反斜杠。

```powershell
subagent-pi.cmd codex -C 'D:\project\my repo'
```

daemon 使用私有 Named Pipe，worker 使用独立 Job Object 追踪和清理后代。中断仍优先取消任务并保留 Pi，close 才卸载；权限不足或归属无法证明时继续拒绝自动恢复。现有 npm Node `.cmd` 入口会解析为 Node + 脚本，不通过 shell 拼接参数；任意自定义 batch 启动器不受支持，改为显式可执行程序及 args。Windows 与 WSL 的运行时、路径和状态目录各自独立。

## 首次使用与 scope

普通 Codex 会话中，首次调用 `pi_spawn_agent` 时传实际工作区绝对路径：

```json
{"cwd":"/absolute/path/to/repository","name":"cache-review","access":"read","task":"检查缓存失效逻辑，只报告问题，不修改文件。","request_id":"cache-review-1"}
```

这是 `pi_spawn_agent` 的参数示例。连接未绑定且未显式指定 scope 时，adapter 会打开该工作区的 scope 并绑定后续调用；若启动器提供了 `PI_AGENTS_SCOPE`，会使用该已有 scope。保存返回的 scope、agent_id、run_id；同一调用方的连接已绑定后，可省略 scope。此时 spawn 的 cwd 只选择子代理目录，省略则使用 scope 目录。

显式传 scope 会选择那份账本，不替换连接的默认 scope。恢复旧 scope、跨 scope 读取或另一个 MCP 连接，应显式传该 ID；相同 cwd 不会让两个 Codex 会话自动合并。另一父会话可读取旧结果，新委托使用自己的 scope。`pi_context` 仍是显式管理调用，不在 9 个日常工具的发现列表中。

也可通过可选启动器先建立 scope：

```bash
cd /absolute/path/to/repository
subagent-pi codex
# 或直接指定 Codex 工作区：
subagent-pi codex -C /absolute/path/to/repository
```

启动器识别 Codex 参数中位于内部 `--` 分隔符之前的 `-C DIR`、`--cd DIR`、`--cd=DIR`、`-CDIR`，并把全部参数原样交给 Codex。`subagent-pi codex -- -C DIR` 的第一个 `--` 是 wrapper 分隔符，会先移除；此后的内部 `--` 之后的文本不参与目录选择。启动器把选定目录的 scope ID 和 cwd 通过环境交给 Codex。

CLI 也能独立开始：`subagent-pi spawn --cwd DIR --task TEXT` 在未给 scope 时自动打开 scope；其他控制命令要传 `--scope ID` 或设置 `PI_AGENTS_SCOPE`。CLI 打开 scope 不会绑定一个已存在的 MCP 连接。

## 工作目录不同与恢复

子代理始终读取 scope 目录的 `SUBAGENT-PI.md`，不加载其工作目录或祖先目录的 AGENTS/CLAUDE；新建、唤醒和 respawn 会重新读取。并发 writer 的 worktree 必须保留原 workspace scope，步骤和示例见 [lifecycle.md](lifecycle.md#scope-与-worktree)。

查找已有 scope 和未确认结果：

```bash
subagent-pi scope list
subagent-pi list --scope SCOPE_ID
```

恢复 Codex 工作可使用 `PI_AGENTS_SCOPE=SCOPE_ID subagent-pi codex resume`；scope 与工作区必须对应。Pi 的历史 agent 可按 [recovery.md](recovery.md) 继续工作。scope 是账本分区，不是对同一 OS 用户的鉴权凭据；自动父通知要求可信的父会话绑定，未绑定客户端使用 wait。

## 不支持插件的 Codex 版本

只在插件模式不可用时采用手动 MCP + skill，避免重复注册：

```bash
codex mcp add subagent-pi -- python3 "$HOME/.local/share/subagent-pi-marketplace/plugins/subagent-pi/bin/subagent-pi" mcp
mkdir -p "$HOME/.agents/skills"
ln -s "$HOME/.local/share/subagent-pi-marketplace/plugins/subagent-pi/skills/subagent-pi"       "$HOME/.agents/skills/subagent-pi"
```

已有同名 skill 时先检查来源。Skill 通过相对路径访问插件 docs，保留整个安装目录；模型实际加载的 skill、MCP 代码和 schema 应来自同一版本。工具是否 deferred 由宿主决定。

## 升级与移除

先收取结果并关闭仍驻留的 agent，再停止该状态目录的 daemon。在新源码目录执行文件升级：

```bash
subagent-pi close AGENT_ID --scope SCOPE_ID
subagent-pi daemon stop
python3 scripts/install.py --force
```

close 示例需覆盖仍驻留的 agent；`daemon stop --force` 会中断所有 scope 的 resident 任务，只有明确需要时才使用。自定义安装路径、bin-dir 或 state-home 时，升级沿用原参数。已注册的本地 marketplace 无需再次 `--register`。

文件升级后还需刷新 Codex 安装缓存：在 `/plugins` 中重新安装，或对支持下列命令的 CLI 移除旧插件缓存后重新安装：

```bash
codex plugin remove subagent-pi@subagent-pi-local
codex plugin add subagent-pi@subagent-pi-local
```

最后开启新会话。`subagent-pi --version` 和 `subagent-pi schemas` 只证明当前 CLI 的版本；0.7.0 应发现 9 个工具且不含 pi_ack_result；还应检查新会话发现 `pi_send_message`、`pi_followup_task`、`pi_interrupt_agent` 等日常工具，并使用对应的 skill。运行中的连接和已载入 skill 不会因源码、marketplace 文件或 git 提交变化而刷新。

升级保留状态目录，旧安装备份在 `plugins/subagent-pi.previous`。支持的历史账本会自动按迁移注册表升级；比当前程序更新的数据库拒绝打开，不自动降级或删除。0.6.0 使用 IPC v4 / schema 7，移除手工 ACK，并把旧 ACK 和通知交付状态迁移为统一关注状态，保留结果及排队撤回意图。IPC 版本不匹配时需先正常结束旧任务，再更新客户端与 daemon；旧版本不能打开升级后的账本。

移除时先停止 daemon，再卸载 Codex 插件并按需移除安装目录及 CLI 链接。状态目录独立保留，可能含未交付结果和重要 session。
