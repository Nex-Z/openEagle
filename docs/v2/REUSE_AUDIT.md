# 能力复用审计

来源：远端基线 a0b3cdc 与本机原 openEagle 未提交 Node/Pi 迁移，只读审查；未运行旧树脚本、未改动旧树。存在源码和测试不等于此次验证通过。

| 能力 | 已有位置 | 本轮选择与必要差异 |
|---|---|---|
| 文件读写/搜索/复制移动删除 | Python default_tools.py；Node tools/native/file-tools.ts、shared.ts | 已有能力。抽取既有路径/目录快照 helpers 接新 Capability 契约；补 Windows 保留名/ADS、审批身份hash与独立结果验证，不重做全套文件工具 |
| CLI/取消 | Python command_runner.py；Node native/system-tools.ts、process-tree.ts、process-registration.ts；Electron utility-child-process-registry.ts | 已有 shell命令和taskkill清理。新固定exe/窄argv模式不直接包装 shell:true，避免扩大授权；沿用自有PID树清理策略，增加有界等待与cleanup不确定性，不宣称新命令行功能 |
| 桌面截图/输入/open URL | Python solo_executor.py；Node native/desktop-tools.ts、desktop-capability-client.ts；Electron capability-broker.ts/input.ts/screenshot.ts | 已存在，应包装broker与一次性票据，不能换成另一个桌面驱动。本轮不操作真实桌面 |
| 浏览器 | 既有open_url +截图/鼠标键盘的视觉执行；未找到独立Playwright DOM能力 | 本轮受控fixture DOM适配与独立oracle是新增验收手段，不替代既有通用桌面浏览器能力 |
| Pi/调度 | runtime/pi-runtime-adapter.ts、routing、workers、application/agent-runtime-host.ts | 已有Pi与分派，不重做供应商/上下文；新AgentEngine契约应包现有adapter，升级Pi 0.80.7→1.1.0需独立兼容，不整体替换 |
| 权限/恢复/记录 | tools/tool-gateway.ts、policy-engine.ts、ledger.ts、recovery、confirmation-broker.ts | 已有且比headless内存记录更完整。后续保留gateway为单一执行权威，将新验证证据映射进去，避免双重审批和重放 |
| UI/协议/生命周期 | packages/protocol、MessagePort transport、src/lib/backend-socket.ts、Electron utility supervisor | 已有迁移成果，不能将v2单包称为UI接入。本轮仅给出接线计划 |

实际选择性复用：`src/adapters/reused-workspace.ts` 抽取 shared.ts 从 isInside 到 displayPath 前的纯路径helpers（省去工具schema包装；审查后修复 isInside 对合法 ..name 的误拒绝，仅拦截 .. 目录段）。来源 SHA256：8cc5673b521d0ffe79d4942549a31680ff494458839f2628090114b849d27753

最小UI接入路径：在现有Node BackendApplication消息处理处增加默认关闭的引擎选择；保留Envelope和request/conversation ID；将ActionRecord映射现有trace/progress，把authorize接ProtocolConfirmationBroker，将取消接TaskHandle.cancel并等待cleanup；桌面能力始终走现有DesktopCapabilityClient/Electron broker，旧Python/现有Node路径可回退。先做协议fixtures和不碰桌面的fake broker集成测试，再启用UI。由于原迁移尚未提交且牵涉整套protocol/utility/preload，不在本轮复制整个脏树或未经验证打开flag。


## Selective UI fixture integration

See UI_FIXTURE.md for scope and exclusions. Old dirty checkout remained read-only. Source snapshots:

- `electron/preload.ts` SHA256 `bb0b3404fa03cbda06887e4db64266be30be9fe4c019aa4a1c49484fec3192d4`
- `src/lib/backend-socket.ts` SHA256 `578915e77bf5fb1194740bdce50db2de13b40157ee2d41b906e9fa9e2ebac3a4`
- `packages/agent-backend/src/capabilities/desktop-capability-client.ts` SHA256 `69857c89f956ca7240a6bd2e8bb7f6ccccda9522b07247f4df685b2a6e3f7213`
- `packages/agent-backend/src/application/confirmation-broker.ts` SHA256 `24a751e936e95849bda4c6d9d7063327a0a99ba0f748af3b767e9874cffbb907`

Preload guards/framing, socket facade and desktop client are selectively copied/adapted; confirmation behavior/envelope shape are adapted to core contracts. No whole-backend or dirty-tree copy. Desktop client cleanup removes abort listeners on timeout/cancellation. The narrow fixture main relay does not claim the full old transport durability/heartbeat/authority feature set.
