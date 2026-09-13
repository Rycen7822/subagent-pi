# 测试报告 — Subagent Pi 0.1.0

验证日期：2026-09-14。平台：Linux x86_64；Python 3.13.5。

## 实际执行结果

```text
python3 scripts/validate_package.py
Package structure OK; 12 MCP tools; no hooks.

python3 -m unittest discover -s tests -v
Ran 52 tests in 82.185s
OK
```

全量测试：**52 通过、0 失败、0 错误、0 跳过**。本报告对应最终代码修改后的回归运行。
`test-report.json` 保存机器可读的数量、环境与验证边界。运行时间是本构建环境的一次测量，不是性能承诺。

| 测试文件 | 数量 | 主要覆盖 |
|---|---:|---|
| test_package.py | 5 | 双格式清单、无 hooks、短 Skill、临时目录实际安装、绝对 MCP 路径、UTF-8、PID 重用识别 |
| test_runtime.py | 36 | spawn/send/steer/follow-up、控制回执、结果确认、幂等、cwd/作用域、单 writer、中断、恢复、deadline、轨迹分页 |
| test_transport.py | 11 | 真实 stdio MCP 与 Unix socket、CLI/MCP 同账本、断线幂等、wait 取消、MCP 退出、daemon 重启、存活孤儿进程处理 |

测试不是仅调用 stub 函数：模拟 Pi 以真实操作系统子进程运行，通过 stdin/stdout JSONL 通信；相关测试还会创建真实后代进程，并终止或重启 daemon。安装测试在临时目录中复制完整插件、生成 marketplace/MCP 配置，并执行安装后的 CLI。

## 重点故障用例

- 同一请求并发重试不重复启动；不同参数不能重用同一 request_id。
- 已排队但未消费的 steer 不报告为已消费。
- clear_queue 不支持或 abort 后仍在工作时，回退到显式硬停止。
- wait 超时或客户端取消等待，不取消 worker。
- MCP 退出后，daemon 和 worker 不被连带终止。
- daemon 被 SIGKILL、Pi 仍存活时，恢复后的状态是 orphaned；不得直接 respawn。
- 单 writer 约束同时应用于首次 spawn 与旧 session 恢复。
- 读取结果不会确认收尾；错误的结果 hash 不被接受。
- UTF-8 字节分页和小输出预算不会静默越界。

## 未验证的内容

本环境没有 Pi 或 Codex 可执行程序，没有调用模型或任何用户凭据。**以上通过结果不代表已通过真实 Pi/provider/extension 或 Codex UI 联调。**

`validate_package.py` 是本地结构检查，不是远程官方 schema 验证器或 Codex 插件加载测试。便携式与兼容格式清单依据官方文档编写；实际宿主仍需安装验收。

Python 3.11 为代码要求，当前实际自动化运行在 Python 3.13.5，未在所有 3.11+ 次版本或 WSL 发行版上矩阵验证。未实现原生 Windows 支持。

## 在用户环境完成验收

安装插件后按 `getting-started.md` 在新 Codex 会话测试 reader 任务。需要直接验证真实 Pi 时，可显式运行：

```bash
python3 scripts/live_smoke.py --allow-model-call
```

该命令会使用本地 Pi 的模型和认证，可能消耗额度。它验证真实 Pi RPC，不替代 Codex 插件 UI 验收。

## 打包完整性

发行 ZIP 包含完整源码、测试、安装脚本、文档、清单、Skill 和许可证，不包含测试临时账本、模型会话、凭据或虚拟环境。`FILES.sha256` 记录包内文件 SHA-256；ZIP 旁的 `.sha256` 校验整个压缩包。安装器会生成机器相关路径，因此安装后的清单不同于源码清单是预期行为。
