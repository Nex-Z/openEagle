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
