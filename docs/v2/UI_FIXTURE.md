# Default-off Electron session fixture

This is a bounded integration test path, not a production agent mode. Set `OPEN_EAGLE_V2_FIXTURE=1` when launching an unpackaged Electron app to select a scripted engine and fake desktop broker. The environment flag is absent/disabled by default; the existing Python/WebSocket path remains the default. Packaged apps do not enable this mode.

The main process creates a new `openeagle-v2-app-*` directory under the system temporary directory before app readiness. It skips settings migration, Python backend startup and quick-assistant registration. Conversations/settings written by the existing UI go only to this new profile. Test profiles are retained for inspection; they are distinct from per-task `openeagle-ui-fixture-*` execution directories, which cleanup removes. Fixture windows enforce webSecurity, do not register the legacy asset protocol, and reject local asset conversion and desktop IPC operations. The utility receives a small explicit environment without provider keys. The fake broker implements only a fixed simulated click; no actual desktop input, browser account, model call or real workspace is used.

## Scope and reuse

- Selectively reuses the old Node migration's self-contained preload framing/sequence checks and backend-socket adapter. Default WebSocket construction preserves this checkout's legacy behavior.
- Selectively reuses its desktop capability client contract against a fake transport; no Electron capability tickets or actual desktop authority are granted.
- Adapts the protocol's envelope shape and confirmation broker's pending/request/sink lifecycle. The fixture broker binds decisions to unique confirmation IDs and original conversation IDs; the UI retains original request/conversation attribution across tab switches.
- Does not import BackendApplication, its composition root, configuration, memory, scheduler, IM, durable recovery or the old Pi version. The existing new core Pi adapter remains independently tested; this UI path uses ScriptedEngine.

Each session retains at most 128 admitted request IDs; exact retries do not execute again. Reusing an ID with different payload is rejected. One task per conversation is admitted at a time; the UI also synchronously blocks repeated clicks while its fixture task is active. Retention is in-memory and lasts for the port session, not across app restarts. Cancellation targets the original request and invokes TaskHandle.cancel; result emission follows runtime cleanup. Normal port disconnect and app exit await utility cleanup. A 3-second shutdown timeout logs forced termination and must not be interpreted as successful cleanup. The fixture transport is not a replacement for the full old migration's durable MessagePort/recovery implementation.

## Manual run

With locked dependencies installed, compile without packaging:

```powershell
corepack pnpm@10.7.0 --dir packages/agent-core build
corepack pnpm@10.7.0 exec tsc -p tsconfig.electron.json
node scripts/ensure-cjs-package.cjs
corepack pnpm@10.7.0 exec vite --host 127.0.0.1 --port 1420 --strictPort
```

Keep Vite running. In a second terminal, set `$env:OPEN_EAGLE_V2_FIXTURE='1'` and launch the installed Electron executable with this repository as its argument. The yellow `V2 测试模式` notice identifies the mode. Send any text, then allow/reject the simulated tool or use `取消测试任务`. Unset the environment flag to restore the default startup path. No automatic fallback from a failed fixture to a real model/backend occurs.

## Automated UI check

With Vite and compiled outputs ready, run `corepack pnpm@10.7.0 v2:test:ui`. If Electron was not installed by lifecycle scripts, set `OPENEAGLE_ELECTRON` to a verified Electron executable. The check creates hidden windows and fresh profiles, asserts the flag label, asset protocol rejection, full correlated lifecycle, cross-conversation result routing, cancellation, and pending-task cleanup on port disconnect and window close. It does not launch the default real-data application.

Validated locally with Electron 42.2.0 from the existing cache, checked against the installed package's SHA256 (`6e034b748ad5ed9445bd3da4b7d0792ed49556774a541217b507c156b00dd69a`). All three UI assertions groups passed; no renderer exception or forced-cleanup timeout was observed. Independent review found and verified fixes for asset access, disconnected-port cleanup, socket close propagation, app-exit cleanup and confirmation attribution. UI screenshot capture of a hidden window was unreliable; functional checks use DOM and actual protocol events.

This fixture UI was published at 8b13077a233511cb4ed73ea8568fbeab4d739c14; both platforms passed in run 38072515137. The separate default-off Pi session now has its own [Chinese setup and verification guide](./PI_SESSION.md).

## 2026-10-11 手动批准的真实 OS 输入与离线修复

用户亲自在专用窗口批准；原生聚焦调用 0 次。真实 nut-js 点击输入框成功（trustedClicks=1），随后真实键盘输入部分完成（trustedInputs=18，最终值 OpenEaglenativei），触发 GUI_POINTER_TAKEOVER 后停止；未点击保存，saved 为空，最终 exact=false。不能称完整 GUI 验收通过，也不能把触发原因归因于用户移动鼠标。两张目标窗口 OS 截图均在打字前，第二张可见输入框焦点；不把截图当成完整打字成功证据。

证据见 [手动验收原始 oracle](evidence/windows-gui-manual-2026-10-11/oracle.json) 与同目录 manifest.json（原始文件 SHA-256）。4 个工具步骤耗时分别为 observe 38.64ms、click 100.94ms、observe 29.94ms、type 205.72ms；前 3 步首次成功，输入步骤失败，重试/补救动作 0 次，脚本规划、模型调用 0 次。全流程首次成功率不能计算为 100%，未执行的保存不算成功。256 秒脚本总时长包含人工批准等待和终态反馈故障，不能当成原生操作耗时。

本轮旧日志未保存预期/实际指针坐标、对应屏幕缩放和移动时序，因此无法事后判断 DPI、多屏、驱动移动或物理输入的原因。点击及之后 observe 均通过精确坐标检查，差异在输入期间出现；这不能排除上述原因。另有缺空格/部分文字问题未定位，不修改系统输入法、不放宽坐标容差、不自动重试。新增审计保存自身计划移动的 DIP/物理坐标、目标屏幕缩放/边界/屏幕数量及每次检查的阶段、时间、预期/实际坐标和差值，精确相等保护不变。

窗口已经关闭但验收器未报告终态：离线复现确认，未调用原生聚焦的报告含 undefined 字段，被未改动的 preload 严格 JSON 校验拒绝。报告改用 null，回归直接验证真实 broker.close 的输出；验收器监测端口 error/close，独立监测宿主队列结束、窗口销毁且租约释放后的释放标记，输出“可恢复鼠键”，释放后缺终态最多再等 3 秒。5 分钟仅供手动批准交接，不适用于释放后的等待。

离线 GUI 策略回归 10/10 通过；本次修复后未再次打开 GUI，真实完整链路、其他真实 GUI 场景及 fake-model→真实 GUI 完整验收仍待用户方便时明确确认下一轮。CI 不运行交互 GUI。

## 2026-10-11 第二轮手动 OS 验收与文字输入调查

原始证据：[round2 oracle](evidence/windows-gui-manual-2026-10-11-round2/oracle.json)，同目录两张 OS 截图及 SHA-256。用户在精确自有窗口批准，自动聚焦 0、重试 0。63 次指针检查全部差值 0；双屏、目标屏幕缩放 1.5、物理点 (1811,1113)。本轮没有 GUI_POINTER_TAKEOVER，不能反推上轮原因。

observe/click/observe/type 为 49.51/88.15/27.54/236.51ms，前三步首次成功，type 为 unverified。最终 Open Eaglenativeinput，不等于 OpenEagle native input，trustedClicks=1、trustedInputs=22、saved 空；GUI_INPUT_UNVERIFIED 停止，保存未执行。脚本总时长约 32.99 秒含人工等待。06:13:18.720Z 宿主已释放输入，终态正常到达，窗口/进程/租约/服务清理；反馈修复本轮有效。

调查边界：旧 oracle 仅有最终值与事件计数，未记录原始 keydown/beforeinput/input/composition 及选区，因此不能恢复当时按键和输入法状态，也不能声称已证实现场根因。焦点保持 entry、HWND 检查通过；这不能排除输入法处理。

已安装 nut-js 4.2.6 的字符串分支按字符 await provider.type；provider 同步调用 libnut.typeString 后 resolve。对同版 Windows 原生 [keypress.c](https://github.com/ZachJW34/libnut-core/blob/v2.7.5/src/win32/keypress.c) 和 [N-API](https://github.com/ZachJW34/libnut-core/blob/v2.7.5/src/main.cc) 的核对表明它走键码/扫描码，而非 Unicode 文本注入，返回不等待控件接收；UTF-8 解码后转 char 的路径也不足以支持通用多字节文字。现有 ASCII 限制继续保留；多字节、孤立代理项和控制字符在任何输入前拒绝，不修改 native 包、不切换输入法、不用剪贴板。

确定性离线测试验证真实 input 适配器和已安装 nut-js 的空格传递顺序正确，并未复现现场变化。因此本次是已证实的“缺少控件接收确认”缺口修复，不冒充现场错字根因已经定位：每字符发出后，宿主提供的 textReceipt 必须确认精确前缀、可信 input 数量、输入框焦点、选区及非组合状态；每次等待继续核对原生焦点、指针和取消，最多 500ms，失败停止且不发后续字符。缺少宿主 receipt 的文字能力拒绝执行。原生 driver 和旧非 GUI 路径保留。

仅自有 fixture 新增有界原始按键/输入/组合事件审计，记录时间、key/code/data、修饰键、选区和目标值；broker 记录逐字符调用和接收时间。审计事件不加入观察指纹，避免异步 keyup 日志使已批准目标无意义失效。无全局键盘钩子，不收集其他应用输入。

离线 GUI 回归 15/15，涵盖首尾/连续空格、多字节明确拒绝、异步次序、接收超时不继续输入、等待中取消、旧延迟设置恢复及终态释放。本轮代码修复后未再打开 GUI；新接收确认能否解决现场错字仍需未来明确授权的实测，完整 GUI 与 Pi→OS 验收仍未通过。
