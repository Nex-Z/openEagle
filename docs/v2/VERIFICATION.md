# 首个切片验证与 Windows 移交

日期：2026-10-10。基础提交：`a0b3cdc98a9e3ff1e794435f1daaeeb699f85f7e`。
云端 Linux，Node v24.19.0。未访问用户 Windows 工作区，未推送，未创建 PR。

## 通过

- `./node_modules/.bin/tsc --noEmit`：原 React 类型检查
- `./node_modules/.bin/tsc -p tsconfig.electron.json --noEmit`：原 Electron 类型检查
- `./node_modules/.bin/tsc && ./node_modules/.bin/vite build`：原前端生产构建通过（2065 modules）
- `UV_CACHE_DIR=/tmp/openeagle-uv-cache uv run --no-project python -m compileall -q backend/app`：Python 语法检查通过，不代表 Python 依赖/行为回归通过
- `./node_modules/.bin/tsc -p packages/agent-core/tsconfig.json`：新包编译通过
- `node --test packages/agent-core/dist/test/runtime.test.js packages/agent-core/dist/test/files-cli.test.js packages/agent-core/dist/test/pi.test.js`：31 passed、0 failed、0 skipped
  - 10 个运行时用例：默认拒绝各副作用、验证失败、批准参数/版本绑定、共享锁、取消/超时、动作预算、不支持能力、展示异常隔离、防记录篡改、准备取消
  - 5 个文件/CLI 用例：真实临时文件与 SHA-256、越界/symlink、命令白名单与最小环境、非零退出/超限/超时/取消、普通后代清理
  - 16 个真实 Pi 1.1.0 循环 + 离线假传输用例：宿主工具结果、失败语义、取消、轮数预算、schema/未知工具、模型自报成功不能制造证据
- 最终复跑曾暴露共享锁测试依赖定时器的偶发失败；改为显式开始/释放屏障后再次运行，31 项全部通过（修复测试同步，不放宽断言）
- 独立只读审查复跑上述 31 项及新包类型检查通过；修复记录可修改、表单字段未绑定、prepare 未取消、contenteditable 验证问题后再次审查通过
- pnpm 10.7.0 `install --lockfile-only --frozen-lockfile --ignore-scripts --offline` 通过，确认锁文件与当前 workspace 一致
- `git diff --check` 通过

安装说明：首次实际依赖安装使用环境提供的 pnpm 11.25.0，`--ignore-scripts`，仅官方 registry。随后用项目约定 pnpm 10.7.0 校验 frozen lockfile。未更新原根依赖的声明范围；新包依赖精确固定，锁文件包含新增传递依赖及 peer 快照调整。曾遇默认 npm/pnpm cache 路径不可写，改用 /tmp 缓存；几次在线 pnpm10 lock-only 尝试被工具自动审批取消，没有把取消报告成成功，最终以现有锁文件做离线 frozen 校验。

## 阻塞 / 尚未通过

真实浏览器执行：云端受限运行时无法启动 Chromium。原五个真实浏览器用例全部在启动阶段失败（0 skipped），未执行页面操作；获批升级权限重试仍相同失败。没有改成 mock 或忽略失败。

尝试命令：在 `packages/agent-core` 运行 `node --test dist/test/browser.test.js`，默认和获批 escalated 两种执行模式。可执行程序 `/usr/bin/chromium`，Playwright 默认已含 `--no-sandbox`，临时独立 user-data-dir、`--remote-debugging-pipe`、headless。

核心错误：

```text
FATAL:chrome/browser/process_singleton_posix.cc:297
Check failed: . socket() failed: Operation not permitted (1)
```

伴随信息：只读 HOME 下 crash report settings.dat 无法写入、ptrace 不允许；进程 SIGABRT。失败后浏览器与本地 fixture server 已清理。没有连接用户浏览器、登录资料或外部站点。

新增三项浏览器回归（隐藏字段/submitter 改动、页面无限 JS 时取消 prepare、contenteditable 验证）已编译，但按用户最新迁移指示不再重跑云浏览器。最终浏览器 suite 是 8 项，均需在允许 Chromium 的环境实际执行。跨 browser/file/CLI 演示与独立服务端/字节/摘要断言已实现，但完整闭环未通过云端验收；不可宣称成功。

未运行：真实付费/远程模型、完整 Python unittest suite、Electron GUI/安装包、Windows/macOS 原生操作、远端 CI。既有安全问题没有并入此次切片。

## Windows 接手必读

1. 在独立目录导入本地 bundle/patch，勿覆盖用户已有未提交 Node 迁移
2. 先读 `docs/v2/README.md` 和本文件，确认 main/master 的基础差异再整合
3. 可先跑类型检查、Pi/运行时测试和 Chromium browser suite；需要安装 Node >=22.19、pnpm10.7、Playwright Chromium
4. 当前 files/CLI 实现明确 Linux-only，Windows 状态 unavailable；文件/CLI Linux tests 和完整 demo 在非 Linux 被明确 skip，不能把 skip 当 Windows 实测通过
5. Windows 下完成安全文件操作和 Job Object/等效进程监督适配后，再运行完整 browser→file→CLI demo 与独立 oracle
6. 后续才接 Electron/CJS 与 ESM entry、可信批准 UI、取消映射、身份认证传输和 Python feature flag 回退。现 UI 仍完全走旧 Python
7. 本地提交作者/提交者必须是 `ZZM-user <z2657272578@gmail.com>`。没有替用户改 git 全局配置，没有推送

## Windows 承接实测（2026-10-10）

- 在独立 `E:\WorkSpace\project\openEagle-v2` 导入 `82c75b24e6e10250437d4e82f9fd6b0f9a72f6e3`；原脏树未改动。Library 原样 helper 在本机现有 Ubuntu 24.04 的临时 Linux 文件系统运行，保留文件 ID/version xattrs；bundle 55,735 bytes 与交接 SHA256 完全匹配，Windows Git 通过 UNC 直接读取、verify、fetch、ff-only 导入。
- Node 24.8.0；命令级 Corepack pnpm 10.7.0；官方 registry 冻结锁文件安装成功（ignore-scripts）。未改全局 pnpm，未安装 Electron/native 生命周期产物。
- 新包 typecheck、原 React `tsc --noEmit`、Electron `tsc -p tsconfig.electron.json --noEmit` 均通过。
- unit：32 tests，27 passed、0 failed、5 skipped。16 项真实 Pi 包循环使用模拟模型传输；10 项 runtime；1 项新增非 Linux 不支持回归。5 项 Linux 文件/CLI 实测在 Windows 跳过，不能算通过。
- browser：使用已安装 Chrome 154.0.8037.99、临时 profile 与本地 fixture，10 tests，9 passed、0 failed、1 skipped。混合 browser/file/CLI demo 因 Windows 适配器不可用跳过。
- 首轮真实浏览器检测到跨源重定向泄漏（外部本地 fixture 收到 1 个请求）；修复后原回归通过，并新增 POST 307 与同源 302 拒绝回归。当前保守拒绝全部 HTTP 3xx，不透明支持重定向；fetch 缓冲响应，尚无网络响应体字节硬上限，限受控本地 fixture。
- 新增 Windows headless CI job；远端 CI 尚未运行。`git diff --check` 通过。

未完成：Windows 安全文件句柄/完整 reparse-point 边界、Job Object 进程生命周期、Windows browser→file→CLI 闭环、真实模型、UIA/macOS Accessibility、Electron UI 接入/安装包。未读取模型凭据、用户浏览器 profile 或真实网站账号，未付费调用。没有用弱化的可信工作区模式冒充现有 Linux 保证；需先审阅 native helper 或明确 opt-in 能力层级设计。

独立审查后追加验证：browser suite 11 tests / 10 passed / 0 failed / 1 skipped；构造器强制字面 loopback fixture、能力声明限制、会话 task ID 绑定回归通过。模型/审批等待期间取消的通用 adapter disposal hook 尚未实现，宿主必须逐任务创建会话并在 task.result finally 关闭；详见 README 生命周期约束。

## 可信 Windows 与指标整合更新（取代前述不可用/无cleanup状态）

用户明确接受普通账号可信工作区模式后，新增独立opt-in factory；旧Linux gate保留。完整suite一次实测51项：46 passed、0 failed、5 Linux-only skipped；审查后新增指标覆盖回归并定向重跑7项全部通过。随后补审批逐动作确认/argv复制断言再运行相关suite，见最终提交说明。Agent build和React/Electron tsc均通过。

真实Windows混合demo通过，浏览器审批等待取消cleanup测试通过。独立审查发现并修复：主进程退出未确认时工作区隔离、父子目录绕过隔离、batch oracle覆盖率混淆、失败trace丢失、cleanup runtime并发复用、合法..name误拒绝。未强制模拟无法杀死的OS主进程，因此quarantine兜底为静态复核，不宣称该故障注入实测。

指标报告：4样本（3正常混合任务+1刻意拒绝），总成功/无补救成功均3/4；首次语义checkpoint 12/13，coverage100%仅指预定义四种checkpoint；p50 871.18ms、探索性p95 1138.61ms、range16.73–1138.61ms。含失败的调用/成功任务=25/3，模型调用0，费用unknown。失败任务runtime completed而taskOracle fail，展示两者区别。每run原始结果和单调事件已保存；不是旧新对比或真实LLM质量指标。

最终unit复核：40 tests，35 passed，0 failed，5 Linux-only skipped；已补逐动作审批和argv变更断言。真实browser suite含混合demo为12项通过（完整suite运行中），之后同逻辑混合demo报告又完成3次。


## Review fixes and terminal CI diagnosis (2026-10-10)

Published commit 7380f31 CI run 38070713371 reached terminal failure on both platforms. Windows unit tests passed; mixed demo approval compared a short temporary path against its canonical long path. Linux selected system Chromium instead of the installed pinned Playwright browser, with launch timeouts. The demo now canonicalizes its temporary root, tests default to Playwright Chromium unless OPENEAGLE_CHROMIUM is explicitly provided, and browser launch has a separate 30-second budget. These fixes have not yet been validated by a new remote CI run.

P1 immediate cancellation: check the signal before invoking the engine and attach a rejection handler when an already-created promise meets an aborted signal. An independent Node child process with --unhandled-rejections=strict verifies immediate cancellation and in-engine cancellation followed by rejection, no engine start in the first case, cancelled results, exactly one cleanup, and clean exit.

P2 overlapping workspace coordination: ResourceLocks now conflicts canonical workspace roots in both ancestor/descendant directions. Windows comparison is case insensitive and component bounded; sibling prefixes and separate drives remain independent. Regression covers queued cancellation, atomic multiple-key acquisition, reverse nesting, duplicate release and POSIX nesting. This remains in-process coordination, not filesystem isolation; adapters must provide canonical roots, and FIFO fairness is not guaranteed.

Local Node 24.8.0 / installed Chrome: compiled agent-core and ran all 54 tests, 49 passed, zero failed, five Linux-only skipped. Independent read-only review found no blocking issue. No UI integration is claimed by this patch; no real model calls or user credentials used. No push performed.


## Default-off Electron UI fixture verification

Published f38021d run 38071293459 completed successfully on Linux and Windows. The subsequent UI patch is local pending review/push authorization.

Core build, React and Electron typechecks pass. Local full fixture regression: 58 tests, 53 passed, zero failed, five Linux-only skipped. Follow-up cleanup-failure regression verifies one cleanup attempt and an honest failed terminal event. Four protocol-session tests plus the default WebSocket/fixture socket-close test pass.

Real hidden Electron 42.2.0 UI test (`scripts/verify-v2-fixture.cjs`) passed: correlated start/progress/confirmation/trace/result, approval after conversation switch with result preserved in original conversation, cancellation, asset protocol rejection, and execution directory removal after both pending-confirmation window close and MessagePort disconnect. No page errors or forced-cleanup timeout observed. Dedicated app userData profiles remain in temporary storage for inspection. Independent review verified fixes for its five findings and reported no remaining blocker. See UI_FIXTURE.md for reproduction and limits. No real model, keys, desktop actions or old checkout mutations.


## Pi 实验会话：本机离线验收

- 新 `model-session.test.ts` 15/15：三种已安装 SDK 的假 fetch SSE 解析、调用/输出/时间预算、零重试、错误/EOF/取消、跨 chunk/轮次脱敏、真实 Pi + 只读工具、会话历史与凭据隔离、链接拒绝、unknown 费用。
- 全量 Windows 回归 74 项：69 通过，5 项 Linux 专用明确 skip；浏览器使用已有 Chrome，仅 literal-loopback fixture。初次运行误用了浏览器环境变量，修正为 `OPENEAGLE_CHROMIUM` 后全量通过。
- core、React、Electron 三层 TypeScript 检查通过。
- 实际隐藏 Electron 42.2.0 的 `verify-v2-pi-session.cjs` 通过：配置后密码框清空、流式文本和已验证 workspace_read、调用 2/2、费用 unknown、新会话隔离、清除禁用、旧 configure 回调不能启用新会话，以及退出删除工作区、fresh profile/log/storage 无假 key 明文。
- 独立审查发现跨轮密钥拼接风险，修复为每任务持续 redactor，加入两轮工具调用回归；复核无阻塞。
- 本次没有真实 provider 网络/付费调用、旧用户数据访问或真实桌面操作。真实供应商和费用验收仍待用户明确选择。

- 最终 fixture UI 回归发现可选 modelStats 的 undefined 值被 preload 严格 JSON 校验拒绝；改为有值才发送，并加入协议 JSON 往返断言。相关 19/19 通过；原 fixture 的跨会话确认/结果、取消、关窗和 port 断连三组实际 Electron 验收全部通过。


## Windows GUI 接线本轮（2026-10-11）

完整核心回归 76 项：71 passed、0 failed、5 Linux-only skipped；新增 GUI 策略 4/4，包含在核心内的 Pi GUI/后台拒绝接线 2/2。既有隐藏 Pi UI 回归通过；三层类型、Vite、Python compileall 通过。真实 GUI 两轮 HWND 不匹配在输入前停止，一轮人工批准超时；成功 OS 输入/截图证据为 0，性能和准确率未验证，不能标通过。详见 [GUI_WINDOWS.md](./GUI_WINDOWS.md)。

用户确认本次前台时段后的单次受限激活仍返回 GUI_INITIAL_ACTIVATION_REJECTED（约 6.10 秒），stage/截图/输入计数均为 0，停止原生路径。生产 UI 与独立测试批准来源回归增至 5/5；精确窗口标题、原始证据和诊断歧义见 GUI_WINDOWS.md。

恢复开发只读诊断确认自有 HWND、native 枚举和 Session 1/Default 桌面一致。独立前台核对后的单次激活仍 GUI_NATIVE_HANDLE_MISMATCH，零截图/输入，停止重复原生尝试。新增 2 个激活结果/取消回归，GUI 策略 7/7；细节与原始 JSON 见 GUI_WINDOWS.md。

## 2026-10-11 手动批准的真实 OS 输入与离线修复

用户亲自在专用窗口批准；原生聚焦调用 0 次。真实 nut-js 点击输入框成功（trustedClicks=1），随后真实键盘输入部分完成（trustedInputs=18，最终值 OpenEaglenativei），触发 GUI_POINTER_TAKEOVER 后停止；未点击保存，saved 为空，最终 exact=false。不能称完整 GUI 验收通过，也不能把触发原因归因于用户移动鼠标。两张目标窗口 OS 截图均在打字前，第二张可见输入框焦点；不把截图当成完整打字成功证据。

证据见 [手动验收原始 oracle](evidence/windows-gui-manual-2026-10-11/oracle.json) 与同目录 manifest.json（原始文件 SHA-256）。4 个工具步骤耗时分别为 observe 38.64ms、click 100.94ms、observe 29.94ms、type 205.72ms；前 3 步首次成功，输入步骤失败，重试/补救动作 0 次，脚本规划、模型调用 0 次。全流程首次成功率不能计算为 100%，未执行的保存不算成功。256 秒脚本总时长包含人工批准等待和终态反馈故障，不能当成原生操作耗时。

本轮旧日志未保存预期/实际指针坐标、对应屏幕缩放和移动时序，因此无法事后判断 DPI、多屏、驱动移动或物理输入的原因。点击及之后 observe 均通过精确坐标检查，差异在输入期间出现；这不能排除上述原因。另有缺空格/部分文字问题未定位，不修改系统输入法、不放宽坐标容差、不自动重试。新增审计保存自身计划移动的 DIP/物理坐标、目标屏幕缩放/边界/屏幕数量及每次检查的阶段、时间、预期/实际坐标和差值，精确相等保护不变。

窗口已经关闭但验收器未报告终态：离线复现确认，未调用原生聚焦的报告含 undefined 字段，被未改动的 preload 严格 JSON 校验拒绝。报告改用 null，回归直接验证真实 broker.close 的输出；验收器监测端口 error/close，独立监测宿主队列结束、窗口销毁且租约释放后的释放标记，输出“可恢复鼠键”，释放后缺终态最多再等 3 秒。5 分钟仅供手动批准交接，不适用于释放后的等待。

离线 GUI 策略回归 10/10 通过；本次修复后未再次打开 GUI，真实完整链路、其他真实 GUI 场景及 fake-model→真实 GUI 完整验收仍待用户方便时明确确认下一轮。CI 不运行交互 GUI。

后台回归：GUI policy 10/10；core 76 项中 71 passed、5 Linux-only skipped、0 failed；core/React/Electron 类型检查与 Python compileall 通过。首次完整后台测试未设置 OPENEAGLE_CHROMIUM，Playwright 缺自带浏览器导致失败/等待；停止已确认的本轮 browser.test.js 子进程后，显式使用现有 Chrome 重跑通过（未安装浏览器）。此环境纠正计为一次回归补救，不能称所有检查首次通过。

## 2026-10-11 第二轮手动 OS 验收与文字输入调查

原始证据：[round2 oracle](evidence/windows-gui-manual-2026-10-11-round2/oracle.json)，同目录两张 OS 截图及 SHA-256。用户在精确自有窗口批准，自动聚焦 0、重试 0。63 次指针检查全部差值 0；双屏、目标屏幕缩放 1.5、物理点 (1811,1113)。本轮没有 GUI_POINTER_TAKEOVER，不能反推上轮原因。

observe/click/observe/type 为 49.51/88.15/27.54/236.51ms，前三步首次成功，type 为 unverified。最终 Open Eaglenativeinput，不等于 OpenEagle native input，trustedClicks=1、trustedInputs=22、saved 空；GUI_INPUT_UNVERIFIED 停止，保存未执行。脚本总时长约 32.99 秒含人工等待。06:13:18.720Z 宿主已释放输入，终态正常到达，窗口/进程/租约/服务清理；反馈修复本轮有效。

调查边界：旧 oracle 仅有最终值与事件计数，未记录原始 keydown/beforeinput/input/composition 及选区，因此不能恢复当时按键和输入法状态，也不能声称已证实现场根因。焦点保持 entry、HWND 检查通过；这不能排除输入法处理。

已安装 nut-js 4.2.6 的字符串分支按字符 await provider.type；provider 同步调用 libnut.typeString 后 resolve。对同版 Windows 原生 [keypress.c](https://github.com/ZachJW34/libnut-core/blob/v2.7.5/src/win32/keypress.c) 和 [N-API](https://github.com/ZachJW34/libnut-core/blob/v2.7.5/src/main.cc) 的核对表明它走键码/扫描码，而非 Unicode 文本注入，返回不等待控件接收；UTF-8 解码后转 char 的路径也不足以支持通用多字节文字。现有 ASCII 限制继续保留；多字节、孤立代理项和控制字符在任何输入前拒绝，不修改 native 包、不切换输入法、不用剪贴板。

确定性离线测试验证真实 input 适配器和已安装 nut-js 的空格传递顺序正确，并未复现现场变化。因此本次是已证实的“缺少控件接收确认”缺口修复，不冒充现场错字根因已经定位：每字符发出后，宿主提供的 textReceipt 必须确认精确前缀、可信 input 数量、输入框焦点、选区及非组合状态；每次等待继续核对原生焦点、指针和取消，最多 500ms，失败停止且不发后续字符。缺少宿主 receipt 的文字能力拒绝执行。原生 driver 和旧非 GUI 路径保留。

仅自有 fixture 新增有界原始按键/输入/组合事件审计，记录时间、key/code/data、修饰键、选区和目标值；broker 记录逐字符调用和接收时间。审计事件不加入观察指纹，避免异步 keyup 日志使已批准目标无意义失效。无全局键盘钩子，不收集其他应用输入。

离线 GUI 回归 15/15，涵盖首尾/连续空格、多字节明确拒绝、异步次序、接收超时不继续输入、等待中取消、旧延迟设置恢复及终态释放。本轮代码修复后未再打开 GUI；新接收确认能否解决现场错字仍需未来明确授权的实测，完整 GUI 与 Pi→OS 验收仍未通过。

## 2026-10-11 第三轮：首字符进入组合输入的直接证据

[第三轮原始 oracle](evidence/windows-gui-manual-2026-10-11-round3/oracle.json) 与同目录两张目标 OS 截图、manifest 已归档，原始字节保持不变。窗口 OpenEagle GUI 17eaec4f-8d9f-4b38-a4bd-8b192691b890；真实 UI 批准；自动激活/重试/模型调用均0。

只发出一次字符 O（codePoint79）；原始事件依次包含 Shift keydown、KeyO/Process、compositionstart、compositionupdate(O)、beforeinput/input 的 insertCompositionText，最后仍 isComposing=true，没有 compositionend。最终 value=O、trustedInputs=1、composing=true、焦点entry、选区(1,1)、saved为空。GUI_TEXT_RECEIPT_TIMEOUT 在约500ms确认等待后停止，没有发出第二字符，也没有尝试提交/取消候选或修改输入法。87次指针差值全部0。

observe/click/observe/type 为48.97/107.83/35.92/524.02ms，前三步首次成功，第四步failed；整体脚本11.53秒含人工批准等待，不等于输入耗时。06:24:20.437Z已释放鼠键，终态正常到达，窗口/进程/租约/服务清理。完整链路仍失败，但本轮实测证明逐字符确认会阻止未提交的组合文本继续累积。

本轮已证实的是“首字符进入未完成组合输入，触发确认超时”；不能将前两轮缺少逐键记录的错字全部归因为同一个原因。当前驱动与IME方案边界见 [GUI_TEXT_INPUT.md](GUI_TEXT_INPUT.md)。归档后不再打开窗口或自动切输入法，等待用户对下一轮的明确答复。

## 2026-10-11 第四轮与方向调整

[第四轮原始 oracle](evidence/windows-gui-manual-2026-10-11-round4/oracle.json) 保留原始10条键盘/组合事件及全部坐标。用户同意自行切英文后运行，但事件仍显示首O进入compositionstart/insertCompositionText，无compositionend，不能把用户点击当成英文直输已生效的证明。没有自动切输入法、激活或重试。

首45次原生坐标读数均为(1811,1113)，移动完成358.70ms后变为(1810,1113)，前次读数到差异约14.85ms；此期间没有再次setPosition，也没有再算DIP，故不能将迟发变化直接归为首次1.5缩放的确定性取整。驱动同版源码moveMouse使用绝对坐标归一化，getMousePos使用GetCursorPos；OS采样/坐标上下文变化或其他移动来源仍未确定。未扩大容差、未回移或重试。

停止GUI_POINTER_TAKEOVER，value=O、saved空，stage1。observe/click/observe/type为39.50/111.43/33.77/217.95ms，前三步首次成功，第四步failed；脚本总11.19秒含人工等待。06:28:06.182Z已释放鼠键，清理完成。

用户纠正后不再把逐字oracle作为产品默认填写方式：新增独立text_fill，复用现有browser_fill整段填写与独立验证，当前目标仅受支持的隔离loopback浏览器元素；跨工具demo已改走text_fill。系统按键诊断desktop_type明确ASCII扫描码，并在prepare前拒绝不支持文本；已观测IME组合状态现在明确报GUI_IME_COMPOSITION_ACTIVE，不等待笼统超时。没有新增native桥、UIA或剪贴板操作。详细架构、候选和社区线索见[GUI_TEXT_INPUT.md](GUI_TEXT_INPUT.md)。

本轮后台最终验证：core共77项，72 passed、5 Linux-only skipped、0 failed；GUI离线16/16；core/React/Electron类型和Python compileall通过。混合Unicode整段填写含独立HTTP回执，跨工具demo包含text_fill且不调用desktop工具。开发回归曾修正编辑字符串转义、受控fixture说明及新增机制证据后的计数断言，不宣称首次全绿；这些修正未触发现场重试。


## 2026-10-11 UIA sidecar 增量

新增受宿主绑定的 FlaUI.UIA3 sidecar 与通用 runtime 工具；仅离线验证，真实 UIA、生产 UI/Pi 注册及 Electron 打包尚未验收/接入。默认 unknown，不能宣称后台或 native 键盘问题已解决。依赖、构建、测试与边界见 [UIA_SIDECAR.md](UIA_SIDECAR.md)。


## UIA 后续产品接线（2026-10-11）

现已完成共享跨进程桌面 lease、默认关闭的 UI/Pi 宿主目标授权入口，以及 framework-dependent Windows 资源接线和运行时提示；普通 Pi 消息可注册同一 runtime UIA 工具。集中自有窗口验收入口已准备，本轮仅执行 plan 模式，真实 UIA 仍未验收。详见 [UIA_SIDECAR.md](UIA_SIDECAR.md) 后续工程闭环章节。

## 首轮真实自有窗口尝试（2026-10-11）

用户明确批准单轮后，在 `0e20577` 干净工作树、活动 console session、无桌面锁的条件下执行了一次 `v2:test:uia:owned`。专用 WinForms 窗口创建成功（PID 42896 / HWND 1513828），但首个自有控件 oracle 报告窗口不在前台，立即以 `FIXTURE_NOT_FOREGROUND_NO_REFOCUS` 停止。没有激活/抢回焦点，没有重试、鼠键或剪贴板降级。

结果：**启动前置条件失败；所有 UIA 写入、拒绝、取消和焦点保持场景均未运行**。`osUiaExecuted=false`、步骤0、模型调用0、重试0，不能算 UIA 成功或失败准确率。启动未首次通过；没有实际步骤耗时可报告。没有足够证据判定是 Windows 前台授权、隐藏启动参数还是 Shown/激活时序所致，不能归因用户操作。

原始报告逐字节保存在 [report.json](evidence/windows-uia-owned-2026-10-11/report.json)，SHA-256、源提交和独立清理检查见 [manifest.json](evidence/windows-uia-owned-2026-10-11/manifest.json)。报告称 fixtureExited=true，随后独立检查 PID 已不存在、桌面锁不存在，立即告知用户可以恢复操作。

已做离线工程修复：仅对显式授权的可见测试 fixture 使用可见启动；增加 `Start this one authorized UIA round` 按钮。runner 最多等待120秒，必须收到用户点击开始且该窗口在前台的自有 oracle 才启动 UIA；点击后失焦直接失败，不反复 refocus。立即显示窗口标题和按钮说明，关闭/超时有明确失败。新增3项离线门槛测试；**修复后的窗口模式没有再次运行**。下一次真实尝试需要先说明并取得新的单轮授权。
## 双自有窗口后台验收入口（当前版本，尚未现场执行）

本节替代旧的“target 必须在前台”验收门槛。UIA adapter 本身没有该前提；目标必须保持在后台，用户操作独立 sentinel 开始。

- 后台目标标题：`openEagle UIA background target`；仅包含已授权的 entry/readonly/password/disabled 控件。
- 前台 sentinel 标题：`openEagle UIA foreground sentinel`；用户点击 `Start one background UIA round`。任一窗口关闭或 sentinel Stop 都关闭这两个自有窗口。只初次显示，不调用 Activate/BringToFront/SetForegroundWindow，也不发送鼠键、剪贴板输入。
- 开始条件为用户明确点击、两个不同自有 HWND 与实际 child PID 身份匹配、随后成功取得共享 desktop lease。sentinel 当前为前台是这项后台测试的观察基准，不是强求目标前台。每个 UIA 操作仍由 adapter 复核 target PID/启动时间/RuntimeId。
- 单独采样线程请求每20ms读取前台 HWND，仅输出 sentinel/target/other/none 分类、序号和单调时钟；不读取其他窗口标题、文本、截图或保存其 HWND。记录实际采样间隔与最大间隔。遇到非 sentinel、乱序、丢样、超过250ms的采样间隔/接收陈旧或8192条上限即停止，不抢回焦点。
- 这是**轮询采样**，并非完整前台事件捕获；即使全部样本都是 sentinel，也只能报告 `sentinel-at-all-samples`，`provesNoTransientChange=false`。不得声称没有采样间隙中的瞬时变化。
- 控件 oracle 使用带请求ID的独立回复；sample事件不会被当成文本结果。报告分别包含 `textResult`、`focusResult`、`cancellationResult`；写入未验证与取消未知副作用不包装成成功，取消结束不等于回滚。
- 就绪等待5秒、用户开始等待120秒、实际运行预算60秒、oracle回复3秒。停止时先取消UIA并等待worker清理，再关闭自有fixture输入流；若1.5秒未退出，仅终止该 child，并再等1.5秒。退出无法确认则报 `UIA_CLEANUP_INCOMPLETE`，不假报释放。

### 现场回传约定

runner 在窗口就绪立即输出 `UIA_READY`（含两个自有窗口身份、sentinel标题及按钮说明）；执行代理看到这个明确事件后**立即结束当前turn回传父级**，不能把 pnpm 启动行当作窗口就绪。待父级告知用户后另行接续观察。

清理确认后立即输出 `UIA_RELEASED`，包括文本/焦点/取消分项状态；执行代理收到后**立即结束当前turn回传**，首先告知用户可以恢复电脑操作。证据整理、提交和CI均用后续后台turn，不能夹在释放与回传之间。清理失败输出不同事件，不告知“已释放”。

### 本轮证据

没有运行 `--run-owned-window`，没有新建任何真实窗口，没有UIA调用。仅运行 plan模式、10项离线模拟测试（开始/身份/焦点分类/采样缺口/回复关联/超时/取消/关闭/有界自有进程清理）、C#构建与14项纯策略自检。双窗口的实际WinForms行为、控件AutomationId、后台SetValue及焦点保持仍待一次另行授权的现场验收。
## 双窗口现场轮次 onCd4Y：后台证据分析（未重跑）

源提交 `52afe4614e25a97054c476723837234ac1d88c0b`。原始报告逐字节保存于 [report.json](evidence/windows-uia-background-2026-10-11/report.json)，SHA-256及事实摘要见 [manifest.json](evidence/windows-uia-background-2026-10-11/manifest.json)。本节分析只使用自有证据，没有查询其他窗口内容或推断用户行为。

| 步骤 | 结果 | 首次耗时 ms |
|---|---|---:|
| 共享 lease 拒绝并发 | 通过 | 0.996 |
| readonly 拒绝 | 通过 | 172.612 |
| password 拒绝 | 通过 | 162.598 |
| disabled 拒绝 | 通过 | 172.975 |
| runtime 审批拒绝且控件不变 | 通过 | 528.449 |
| Unicode 整段填写 | 中止、值未验证 | 532.019 |
| stale / replay / 取消专项 | 未运行 | — |

预定9项，执行6项，其中5通过、1中止；后3项未运行。全部重试0，脚本规划、模型调用0。上述不是 LLM 准确率，也不是后台文本能力成功率。

焦点样本1—60均为 sentinel；第61帧变为自有 target。第60帧时间13001.8390ms，第61帧13033.0444ms，间隔31.2054ms；从首帧到变化为1835.4702ms。最大采样间隔32.3429ms，没有 other/none 样本。只能确定变化发生在该采样间隔内，不能精确到某条COM指令。

监测记录 `FIXTURE_FOREGROUND_CHANGED` 后中止活动 worker，该请求报 `UIA_CANCELLED`；这是**monitor 触发的取消**，不是取消专项通过。清理随后完成，报告 fixtureExited/leaseReleased 均为true，现场已立即回传释放。没有重新激活、鼠键、剪贴板或现场重试。

### 能证明与不能证明的调用

- 真实 UIA bind/inspect 已执行：三个控件政策拒绝以及审批拒绝前的 entry 观察均来自实际 sidecar/UIA；不是 fake provider。
- Unicode步骤确实进入填写流程，但原报告没有记录每个 worker 的 operation 及 SetValue 边界。UIA_CANCELLED 可能来自该步骤的观察、fresh inspect 或 set worker；532ms时长不能替代调用证据。
- **不能确认 COM SetValue 是否被调用/返回，也不能确认最终值**；mayHaveChanged=true保留unknown。不能声称“没写入”，也不能声称“写入成功后被取消”。

### 根因范围与最小工程修复

60帧稳定后才变化，不支持“monitor 刚启动即误把背景状态当变化”的解释。首个变化样本在清理开始前被记录，所以不是 finally 关闭窗口造成的这个首个变化。期间已有多个 bind/inspect worker 完成，未出现 worker 自己窗口对应的 other 类别；这削弱“一启动任何worker就必然抢焦点”的假设，但不能排除特定worker或provider间接激活target。

已核查 WinForms 官方 v10.0.0 的 [TextBoxBaseAccessibleObject](https://github.com/dotnet/winforms/blob/v10.0.0/src/System.Windows.Forms/System/Windows/Forms/Controls/TextBox/TextBoxBase.TextBoxBaseAccessibleObject.cs) 和 [AccessibleObject](https://github.com/dotnet/winforms/blob/v10.0.0/src/System.Windows.Forms/System/Windows/Forms/Accessibility/AccessibleObject.cs) SetValue路径。本机安装程序集显示10.0.11，不能把相邻版本静态源码当成本轮执行轨迹。当前**没有确证的焦点变化根因**，不据此替换provider、放宽监测或再加操作门槛。

确定的工程缺口是审计不足。本次仅补：固定阶段的worker诊断（request-validated / set-call-boundary / set-returned / operation-returned）、worker启动/退出/取消请求，同一Node单调时钟的step起点和采样接收时间，以及monitor原始stopTrigger。诊断不含文本/目标内容，按请求ID匹配、8192字节限长；观察器异常不能改变执行结果或触发重试。新增离线回归验证这些边界。

`set-call-boundary`只表示到达调用前边界，不保证已进入COM；`set-returned`才证明调用返回，仍需独立值oracle才算写入验证成功。新增审计不能追溯补全旧报告。

下一步仅在另行授权后做有界验证：先用新审计定位变化相对worker/set边界的顺序；若已确认只发生在SetValue边界，再单独分析目标provider。当前保持backgroundVerified=false；本轮没有重新运行现场，也没有新增测试门槛替代能力。
本次后台验证：UIA transport / Pi integration / sentinel 离线回归 26/26，C# 纯策略自检 14/14，TypeScript core、React、Electron 类型检查通过。新增审计测试首次因测试内子进程脚本换行转义错误失败，修正测试后通过；这不是现场重试，也不计入现场首次成功率。未再运行真实窗口。
