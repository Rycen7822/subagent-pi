# Testing

## 离线自动测试

```bash
python3 scripts/validate_package.py
python3 -m unittest discover -s tests -v
```

使用 tests/fake_pi.py 构造真实本地 subprocess、stdin/stdout JSONL、事件、UI 请求、延迟、中断、crash 和 shell 后代。不使用外部模型、网络或真实仓库。测试自己的临时目录会被清理。

runtime 测试覆盖任务状态机、幂等、read/write 冲突、steering 消费证据、follow-up、wait、interrupt、close、恢复、UTF-8 分页、结果确认和 schema。

transport 测试通过真正的 MCP stdio 和 Unix socket，覆盖并发初始化、MCP/CLI 共享账本、取消等待、adapter 退出、daemon 重启和断线幂等。

包结构测试只验证当前包内部清单/路径/Skill长度/工具定义，不等同于官方 marketplace 审核，也不替代安装到实际 Codex 的验收。

## 实际 Pi 测试

```bash
python3 scripts/live_smoke.py --allow-model-call
```

该测试需要现成 Pi 认证，可能产生费用。它只检查独立 Pi RPC 可以读临时 marker、返回结果、确认并关闭；不能证明任意扩展/provider 组合均兼容。

## 实际 Codex 验收

安装插件、新建 Codex 会话，用 pi_context + reader 启动一个小任务；在其运行时 inspect 和 steer，再 wait/result/ack。另测中断后继续、关闭后恢复、MCP 重连后同 scope 查询。

当前源码环境没有 Pi/Codex 可执行程序，因此真实 Pi/模型和 Codex UI 验收均不包含在离线报告中。请保留这一差异，不把模拟器的 test pass 写成生产兼容证明。
