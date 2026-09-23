# Testing

## 离线自动测试

```bash
python3 scripts/validate_package.py
python3 -m unittest discover -s tests -v
npm run setup
npm run typecheck
```

默认使用 fake 子进程验证 MCP/CLI → daemon → guard 的协议、任务身份、回执、持久化、进程清理、故障和恢复。Node 队列测试由 Python 套件调用，覆盖串行归属、异常退出、handled 输入和旧回调不能混入新任务。没有模型请求。

资源回归使用隔离进程和真实 socket/stdio，覆盖 RPC/UI/IPC 背压与有界关闭、MCP 半帧取消、结果交付确认、Worker/锁回收和幂等记录保留。退出与替换并发用例验证旧任务完成清账、旧队列取消、迟到回调隔离和统一容量准入；后代保留管道时也要及时结算。Node 测试覆盖任务输入队列与输出通道的容量、进度合并、关键帧顺序和输出超时；真实 SDK 测试暂停自有 daemon 读取，验证大量工具进度后仍能提问、收取结果和执行后继任务，输入或关键输出过载则崩溃并取消队列。父通知与事件裁剪的积压测试检查有界扫描和跨短进程代际的保留上限。

静默回归覆盖 text/thinking/toolcall 增量、无关通知、并行长工具、工具结束后恢复计时、父代理问答和锁等待后的复验；真实 SDK 使用离线流和无输出工具验证超过静默阈值仍可正常完成，静默模型才停止。这些确定性故障测试不等同于多日驻留测试。

继承回归覆盖客户端环境绑定、Pi-first 同名 skill、builtin 来源判定、扩展同名工具保留、MCP 私有 bootstrap/receipt、stdio/HTTP 协议协商与错误路径。bridge 测试需要已安装 Pi 提供依赖；缺少时明确 skip。

## 原版 Pi SDK 进程测试

```bash
SUBAGENT_PI_LIVE_PI=1 python3 -m unittest discover -s tests -v
```

使用原版 Pi 0.87.0 的 SDK，隔离 HOME、PI_CODING_AGENT_DIR、workspace 和 daemon。`SUBAGENT_PI_LIVE_PI_BIN` 可选择另一份官方 Pi 安装。无需宿主补丁，不改安装缓存。

真实 MCP → daemon → SDK 子进程使用离线 mock provider，fetch 被替换为抛错函数。覆盖正常任务、before-settle/native continuation、多条及嵌套扩展输入、串行预处理与 FIFO、强制 system prompt、handled 输入、有序 steer、中断预处理、替换进程、迟到计时器隔离与显式 UI 确认；另验证启动期本地命令完成、无归属消息拒绝和扩展 stdout 不破坏协议事件。provider context、结果/hash 和进程清理是行为证据。父通知测试另覆盖 MCP 元数据归属、共享连接隔离、终态/问题投递及不确定提交不重试。

`RealPiSkillBoundary` 另验证真实资源加载、Pi-first skills 和 builtin/扩展工具区分，不提交模型任务。全量 live 包括这一层。

## 打包与类型

```bash
mkdir -p .work/package
python3 scripts/package.py --output .work/package/subagent-pi.zip
sha256sum -c FILES.sha256
```

重复打包必须得到相同 manifest/ZIP。包包含 SDK transport 和队列，不含 `.work`、node_modules 或宿主补丁。开发声明从当前 Pi 安装 stage，重复 setup 不得破坏符号链接目标。

GitHub CI 的 Python lane 跑默认离线套件；integration 固定 Pi 0.87.0、Node 22.19.0，执行类型检查、完整 live 离线套件和打包检查。

## 需要单独授权的验证

真实模型请求可能计费，需要显式同意。离线 MCP 和 SDK 证明不等于已经安装到 Codex。安装后还需新建 Codex 会话检查工具发现、spawn/steer/result/ack、interrupt/respawn，以及实际使用的扩展组合。

尚未证明全部第三方扩展兼容，也未把 Pi 0.85/0.86 或更新版本列为本 SDK transport 的验证目标。扩展自己创建的其他 SDK session、独立模型调用及脱离进程组的后代不受任务队列保证。

父代理唤醒集成：`SUBAGENT_PI_LIVE_CODEX=1 PYTHONPATH=tests python3 -m unittest test_parent_live`。真实 Codex 与 Pi SDK 使用隔离 HOME 和本地 mock 模型，验证父会话完成后由问题/完成通知重新开启同一会话；不使用真实认证或模型端点。

长等待集成：`SUBAGENT_PI_LIVE_CODEX_WAIT=1 PYTHONPATH=tests python3 -m unittest test_wait_live`。隔离安装原生插件，由真实 Codex app-server 直接调用 MCP，等待 fake Pi 310 秒后完成；验证跨越宿主默认 300 秒限制及省略参数时的 IPC 预算，无模型调用。日常 MCP 回归则用一小时上限验证短任务/延后提问即时返回及取消等待不杀子代理。
