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


## Pi 会话增量复用

选择性参考旧 `packages/agent-backend/src/application/pi-model.ts` 的明确 provider/API 映射，在新 `model-config.ts` 中保留三种受支持组合；不导入旧 BackendApplication、设置、key、模型目录或隐式认证。复用已有 PiEngine 与统一 runtime 的 prepare/execute/verify，不创建第二套权限执行器。新 `ModelSessions` 仅持有会话内凭据、预算及有界文本历史；专用 IPC 与普通 Envelope 分离。完整边界和下一步接口见 [PI_SESSION.md](./PI_SESSION.md)。


## Windows GUI 接线（2026-10-11）

复用 desktop-tools 通道契约、既有 DesktopCapabilityClient、原样 CapabilityTicketStore 与 input.ts/nut-js 驱动；GuiBroker 选择性适配原 CapabilityBroker 的请求/票据边界，增加专用窗口租约、身份/焦点/观察检查。GuiBinding 与 fixture 策略分离，Pi 宿主扩展注册同一工具。没有复制 BackendApplication 或新建 native 栈。原 capability-broker.ts SHA256：04f68f88d71a7620b0f2c55842ce3f1b6c91b617bd5399afc51238c6469014b8；desktop-tools.ts：1ad2ee1bc73af72fc296405cb551f24754b807fd96a5db54827abd5f4f8b9e59。实际 OS 验收阻塞和完整限制见 [GUI_WINDOWS.md](./GUI_WINDOWS.md)。

## 文本与按键分离（2026-10-11）

text_fill复用现有browser_fill的prepare/指纹/资源/effects/handle.fill/verify，注册在原browser能力集中；不新建执行器或扩大来源权限。demo改走整段语义填写，原browser_fill兼容保留。系统扫描码desktop_type保留为显式诊断；UIA桥、原生Unicode入口及安全剪贴板事务均未实现。已有Electron insertText只覆盖自有webContents，旧quick-assistant剪贴板快照没有并发保护，未拿来充当通用桌面文本能力。详见[GUI_TEXT_INPUT.md](GUI_TEXT_INPUT.md)。


## 2026-10-11 UIA sidecar 增量

新增受宿主绑定的 FlaUI.UIA3 sidecar 与通用 runtime 工具；仅离线验证，真实 UIA、生产 UI/Pi 注册及 Electron 打包尚未验收/接入。默认 unknown，不能宣称后台或 native 键盘问题已解决。依赖、构建、测试与边界见 [UIA_SIDECAR.md](UIA_SIDECAR.md)。
