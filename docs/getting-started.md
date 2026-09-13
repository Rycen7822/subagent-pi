# Getting started

## 环境

本版针对 Linux / WSL2，依赖 Python 3.11+、已安装并登录/配置完成的 Pi、支持本地插件或 MCP 的 Codex CLI。不要从原生 Windows Python 启动此运行时。WSL 状态目录建议位于 Linux 文件系统，不放在 `/mnt/c` 或网络文件系统；文件锁和 SQLite WAL 是本地文件系统设计。

先确认：

```bash
python3 --version
pi --version
codex --version
```

Pi RPC 需要 `get_state`、`prompt`、`steer`、`abort`。支持 `clear_queue` 的 Pi 可保留进程进行中断；旧版不支持时，本插件转为终止进程，不假装已经清掉队列。插件实现依据 2026-09-14 读取的 Pi 文档，不按未经测试的版本号作兼容承诺。

## 本地插件安装

```bash
python3 scripts/install.py --pi "$(command -v pi)" --register
export PATH="$HOME/.local/bin:$PATH"
```

默认安装目录：`~/.local/share/subagent-pi-marketplace/plugins/subagent-pi`。本地目录清单：`~/.local/share/subagent-pi-marketplace/.agents/plugins/marketplace.json`。CLI：`~/.local/bin/subagent-pi`。

脚本生成 portable `plugin.json` / `mcp.json`，同时提供旧式 `.codex-plugin/plugin.json` / `.mcp.json` 回退。安装后的两份 MCP 配置指向相同的**绝对 Python 与程序路径**；不是让模型推断插件路径，也不依赖 MCP 启动 cwd。

在 Codex 输入 `/plugins`，安装 Subagent Pi Local 中的 Subagent Pi，再开启新会话。`--register` 不等于已经通过 Codex UI 安装插件。旧版不支持 portable manifest 时依赖兼容清单；宿主能力仍需本机验证。

安装可以不调用 Codex：

```bash
python3 scripts/install.py --pi /absolute/path/to/pi
codex plugin marketplace add "$HOME/.local/share/subagent-pi-marketplace"
```

不要只把源目录移动到某个地方，就假定 PATH 中已经有 `subagent-pi`。分发源清单使用 `subagent-pi mcp`；安装脚本会生成不依赖 PATH 的实际入口。

## 首次使用与 scope

从实际项目目录启动是最明确的方式：

```bash
cd /absolute/path/to/repository
subagent-pi codex
```

启动器创建一个 scope，将 `PI_AGENTS_SCOPE`、`PI_AGENTS_CWD` 传给 Codex。不要在这个启动命令后再用 Codex 的目录切换参数改变项目；先 `cd` 到目标目录。

Codex 使用 `pi_context(cwd="/absolute/path/to/repository")` 获取并复用该 scope。若宿主没有把环境变量传给 MCP，则仍然可以显式传入 scope。不依赖未经验证的父线程 ID 环境变量。

也可以在普通 Codex 会话里直接调用 pi_context。相同 MCP adapter 进程会记住该 cwd 最近绑定的 scope。adapter 重新启动后，没有显式恢复 ID 就不会自动猜测旧会话归属。通过 CLI 查找：

```bash
subagent-pi scope list
subagent-pi list --scope SCOPE_ID
```

恢复旧 Codex 工作时：

```bash
cd /absolute/path/to/repository
PI_AGENTS_SCOPE=SCOPE_ID subagent-pi codex resume
```

scope 不会把工作目录相同的两个 Codex 会话自动合并。它是账本分区，不是对同一 OS 用户的鉴权凭据。

## 不支持插件的 Codex 版本

只在插件模式无法使用时，采用这一回退方式，避免重复注册：

```bash
codex mcp add subagent-pi -- python3 "$HOME/.local/share/subagent-pi-marketplace/plugins/subagent-pi/bin/subagent-pi" mcp
mkdir -p "$HOME/.agents/skills"
ln -s "$HOME/.local/share/subagent-pi-marketplace/plugins/subagent-pi/skills/pi-subagents" \
      "$HOME/.agents/skills/pi-subagents"
```

已有同名 Skill 时不要覆盖，先检查。Skill 通过相对路径访问插件内部 docs，建议保留整个安装目录。MCP 工具是否 deferred 由 Codex 决定；插件不伪造不存在的 defer 配置。

## 升级与移除

升级前先关闭 resident agents，再停 daemon：

```bash
subagent-pi close AGENT_ID --scope SCOPE_ID
subagent-pi daemon stop
python3 scripts/install.py --force --register
```

`daemon stop --force` 会中断正在运行的任务，请只在确实希望停止时使用。升级不会清空状态目录；旧安装保留在 `plugins/subagent-pi.previous`。如果协议/数据库版本不兼容，程序拒绝运行，不自动迁移或删除数据。

移除时先在 Codex 卸载插件并停止 daemon，再按需移除安装目录和 CLI 链接。状态目录不会自动删除，里面可能仍有未确认结果和重要 Pi session。
