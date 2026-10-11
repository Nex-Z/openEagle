# Windows UIA sidecar：受限的语义文本适配器

2026-10-11。本轮新增实现与离线验收；**未执行真实 UIA 控件观察/SetValue，未启动新的前台测试窗口**。此前四轮 native 键盘测试仍未完整通过，不能用本轮 fake provider 测试覆盖该结论。详见 GUI_TEXT_INPUT.md。

## 实现与复用

Node/TypeScript/Pi 主体不变。`createWindowsUiaCapabilities` 导出同一 Capability/AgentRuntime 接口的 `uia_observe`、`uia_set_text`，复用审批摘要、资源锁、取消、记录和独立 verify。宿主显式提供 sidecar 绝对路径、自己授权的 HWND/PID 与最多 16 个 AutomationId；这些不能来自模型工具参数。注册到宿主 registry 后可作为普通工具使用；**本轮未自动接入生产 UI/Pi 会话，也未打包进 Electron 安装包**。原 nativeDesktopCapabilities 默认 unsupported 占位与原 nut-js 路径保留，不能称默认入口已支持 UIA。

`native/windows-uia` 是小型 C# 单请求进程，使用 FlaUI.UIA3 的 ValuePattern.SetValue，整段替换 Unicode 文本；没有键盘、剪贴板、SetForegroundWindow 或失败降级路径。不是固定业务脚本。仅支持暴露 ValuePattern 的可编辑 Edit 控件。目标以 HWND、PID、进程启动 ticks、根 RuntimeId 绑定；控件身份与旧值 fingerprint 每次复查。只在该根下按唯一 AutomationId 搜索，最多 256 节点、8 层；跨进程子树、歧义、密码、只读、禁用、缺失 pattern 均拒绝。复杂多进程应用可能因此不可用。

观察只返回身份和值的 SHA-256，不返回原值、控件名称或全桌面树。哈希不是加密，低熵原值仍可能被猜测。写入文本会进入现有 runtime 参数记录，宿主不能将密码或其他敏感内容交给此工具。输入最多 10,000 UTF-16 单元，拒绝 NUL/孤立 surrogate，支持空值、空格、中文、emoji、换行（最终是否接受由控件决定）。观察默认有效 2.5 秒，写入一次性消费；sidecar 写后校验和 Node 新进程独立读回都必须一致。

## 后台、授权与取消边界

UIA API 不主动发送系统输入，但 provider 可能改变焦点或拒绝后台操作。默认 `interactionMode=unknown`、`mayChangeFocus=true`、`usesSystemInput=false`。只有宿主对**具体目标/provider**独立验证后才可设置 backgroundVerified；不能让模型决定，也不能从一次 fake 测试推断。SetValue 前后检测前台 HWND 变化，变化后停止并标未知；这不能阻止 provider 已产生的焦点变化，也不能检测中途变化后恢复。

写操作声明 write + external-submit，经原 runtime 批准才执行；后者保守覆盖控件可能触发的外部行为。默认使用 runtime 的 desktop:system-input 资源锁，但它仅在同一 Node 进程内互斥，**尚未整合 Electron broker 的跨进程桌面 lease/用户确认 UI**；因此不能并行启用生产 UIA 和 native 操作。宿主须保持显式授权范围，后台限定模式不能注册 unknown 工具。

每操作一个隐藏子进程，默认 4 秒期限，stdin/stdout 限长、请求 UUID 校验、最小环境、不传模型 key；取消/超时杀自己创建的 worker 并等待退出，超出清理期限报错。COM provider 已接受的操作可能晚于 worker 退出生效，不能保证回滚。任何写入异常或校验失败都停止当前 session，不自动重试、不按键补救；需重新观察和人工决定后再开新会话。

## 依赖、构建、体积

现有本机 .NET SDK 10.0.303，无新 SDK/VS 安装、无管理员权限/服务/UIAccess、无不明预编译工具。官方 NuGet 源在 NuGet.Config 中唯一限定，packages.lock.json 固定内容哈希：

| 包 | 版本 | 用途 |
|---|---|---|
| FlaUI.UIA3 | 5.0.0 | UIA3 wrapper，直接依赖，MIT |
| FlaUI.Core | 5.0.0 | 公共 UIA 抽象 |
| Interop.UIAutomationClient | 10.19041.0 | COM interop |
| System.Management | 8.0.0 | FlaUI 传递依赖 |

FlaUI 官方源：https://github.com/FlaUI/FlaUI/tree/v5.0.0 。直接调用 Patterns/ValuePattern.cs 的 SetValue，未使用可能模拟输入的 TextBox.Enter。UIA2 仅作为未来兼容性备选，本轮没有引入。依赖许可见官方包元数据，发布分发需保留各包许可；本轮没有交付安装包。

```powershell
$env:DOTNET_CLI_TELEMETRY_OPTOUT='1'
$env:DOTNET_GENERATE_ASPNET_CERTIFICATE='false'
dotnet restore native/windows-uia/OpenEagle.Uia.csproj --locked-mode --configfile native/windows-uia/NuGet.Config
dotnet build native/windows-uia/OpenEagle.Uia.csproj -c Release --no-restore
& native/windows-uia/bin/Release/net10.0-windows/OpenEagle.Uia.exe --self-test
```

实际 framework-dependent publish：10 文件、1,125,687 字节（包含 PDB），不含 .NET runtime。最终用户无需 SDK/开发工具，但此产物需要 .NET 10 Windows Desktop Runtime；尚未制作 self-contained 版本，不能把上述体积作为免运行时安装包体积。产物/cache/bin/obj 均 gitignore。

## 本轮验收

- C# build/publish 零警告零错误；--health 正常；14 项纯策略自检通过，明确输出 osUiaExecuted=false。
- Node 新增 9 项测试：单次 Unicode 全值写入与新读回、过期观察、未知结果停止、读回不符、非法 selector/Unicode/取消前拒绝、真实子进程分片 UTF-8 与环境隔离、协议 UUID/输出上限、超时/取消退出、runtime 审批拒绝零写入。写入 provider 是 fake，子进程协议与终止是真实进程。
- Windows CI 编译 C# 并跑纯策略自检；Linux/Windows 跑 Node 测试。Hosted CI 不运行交互 GUI 或真实 UIA。
- 待显式通知并获准后的自有窗口验收：中英文/空格/多行/emoji、密码/只读/禁用、窗口重用/关闭、后台焦点保持、取消与延迟副作用。须保留时间、首次正确与补救次数；本轮没有这些实际 UIA 指标，不能报告模型准确率。
完整本机 agent-core 回归：86 项，81 通过、5 项 Linux-only 跳过；GUI 策略 16/16 通过。

## 后续工程闭环：共享 lease、产品入口与集中验收（2026-10-11）

本节更新上文初版的“尚未接入”状态：现已接入 **显式开启的安装版/开发版 Pi 通道**，不再只有工厂。默认旧入口仍保持关闭；真实 UIA 成功、后台 provider 行为仍未验收。

- `desktop-lease.ts` 被 Electron native broker 与 UIA 共用。相同 `%TEMP%/openeagle-gui-system-input.lock`，原子 mkdir + 随机 owner token；持有至 worker 清理完成。不同进程互斥，重复释放共享 promise；未知/遗留/所有者变化的锁不会自动回收。不同账户/session/TEMP 不提供全系统互斥保证，本任务不创建其他账户。
- `UiaSessionPanel → preload v2:uia → UiaHost → utility UiaSessions → ModelSessions/PiEngine → 同一 AgentRuntime` 已接通普通消息。UI 从宿主清单选择目标，原生对话框确认下一任务，授权五分钟过期且只消费一次。每次写入另行确认完整文本；停止会撤销待授权/活动目标并关闭自有 worker。批准对话框等待期间撤销不会使旧批准复活。
- 目标 HWND/PID、可执行程序路径与 AutomationId 白名单来自宿主，不是模型参数或任意网页选择器。程序路径固定在组件资源目录；宿主清单有大小/数量/字段限制。UI 不扫描桌面、不自动抢焦点、不自动设置 backgroundVerified。后台限定申请直接返回 needsForeground；产品注册始终 unknown。
- 关闭 utility 的兜底等待延长到 10 秒，覆盖 UIA 有界清理；若未知 provider 或进程清理失败，保留锁并报告，而非释放后继续操作。Stop UIA 停止 UIA 扩展，普通任务 Stop 才取消整个 Pi 任务。

### 显式入口与宿主清单

开发者/宿主先提供**已获授权的目标**清单（本任务只能使用自有测试窗口），示例结构如下，数值必须由目标宿主取得，不能照抄或猜测：

```json
[{"id":"owned-editor","label":"自有测试窗口","hwnd":"实际十进制HWND","processId":1234,"allowedAutomationIds":["entry"]}]
```

启动环境同时设置 `OPEN_EAGLE_V2_PI=1`、`OPEN_EAGLE_V2_UIA=1`、`OPEN_EAGLE_V2_UIA_TARGETS=<宿主清单绝对路径>`。没有开关或清单时不注册工具。Pi 模型/API/预算仍须原有显式配置；本轮只用假 transport，不读取旧 key，不产生付费调用。

界面打开“UIA 目标授权”，检查组件与目标 → 选择 → 明确允许可能改变焦点 → 在原生确认框授权下一任务 → 发送普通任务。每次写入另行批准。观察默认 2.5 秒过期；长时间审批导致过期会拒绝，不自动刷新/重试副作用。

### 打包和运行时

`uia:build` 使用已有 .NET SDK，官方源 locked restore，然后 framework-dependent publish。Windows 安装构建把产物作为外置 `resources/windows-uia`（排除 PDB），不会将 .NET runtime 打进包，也不会下载或静默安装 runtime。`UiaHost.status` 通过隐藏进程 `--health` 探测实际可启动性；组件缺失、运行时缺失/不匹配等统一显示可操作提示，其他入口不被强行阻断。

agent-core 作为 workspace 生产依赖进入包；OpenAI SDK 使用 Pi 已锁定的 7.19.0 显式根依赖，修复 electron-builder 对该传递依赖漏收集。无版本升级、无新增模型调用。打包检查只生成隔离的 win-unpacked，不创建/安装 NSIS，不运行前台应用。

### 集中真实验收入口（本轮未运行）

`pnpm v2:test:uia:plan` 只打印计划，不开窗口；本轮已经执行此模式。待先通知并获准后才可运行 `pnpm v2:test:uia:owned`。它只创建一个自有 WinForms 窗口，测试成功、密码/只读/禁用拒绝、共享锁、审批拒绝、过期/重复、取消和前台保持；使用 fixture 自己的控件值作为独立 oracle。每步记录毫秒、首次结果、零重试，报告写入自有临时目录。遇到意外结果或焦点变化即停止，不抢回前台；finally 仅关闭/终止自己创建的窗口进程。运行中的取消若无法观察到，不算通过，不自动重跑。

这是 UIA 语义写入验收准备，不是 native OS 键盘验收，也不是 LLM 准确率。脚本与自有控件 AutomationId 的实际兼容性还未运行验证，不能把“已准备”写成“已通过”。此前四轮 native 键盘失败/中断证据继续保留。

### 本轮离线证据与补救记录

- agent-core 90 项：85 通过、5 项平台限定跳过；GUI 策略 16/16；新增产品宿主策略 4/4；C# 纯策略14项保持通过。Pi 扩展测试运行真实 Pi 循环，但模型和 UIA provider 均为 fake。
- 跨进程 lease 使用真正另起 Node 进程验证互斥；撤销发生在 bind/批准等待中的竞态均覆盖。
- 首次普通离线 install 因无关可选依赖缺缓存失败，改成精确 workspace lock + frozen offline install 成功。首次解包归档因并发运行 TypeScript 编译造成快照不一致，不能算成功产物；随后按串行编译/打包修复。上述开发补救不计为真实 UIA 首次正确率。
补充打包修复：现有 electron-builder/pnpm 收集还漏掉 nut-js/Jimp 的 11 个已锁定传递依赖。将这些版本及其原 peer 快照显式列为生产依赖，冻结离线安装成功；未下载新驱动或升级版本。第二次完整性正确的归档已能加载 Pi/UIA/lease，但 native import 因上述缺失失败，不能算完整应用通过；随后继续修复验证。

最终解包验证已通过：打包内 Pi / UIA / lease / nut-js 在 ELECTRON_RUN_AS_NODE=1 下成功加载（PACKAGED_PI_UIA_LEASE_NATIVE_IMPORT_OK），外置组件 --health 成功。只检查模块/资源，不启动应用窗口，不执行鼠键、UIA 控件操作或模型请求。打包器剩余警告为 mac-only 权限包及构建期 node-addon-api；Windows 所需 native import 已通过。解包验证之后另补了一项 host claim 在途撤销测试，最终源码由离线测试和 CI 验证；解包目录不是交付安装器。

最终源码完整回归：91 项，86 通过、5 Linux-only 跳过，耗时 22.78 秒；UIA transport/integration 14/14、宿主策略4/4、GUI策略16/16、C#纯策略14/14，React/Electron/core类型检查与 Python compileall 通过。真实 UIA/前台窗口仍为 not run。

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

## 自有 WinForms 前台 UIA 单轮通过（pd1CN9）

用户明确批准一次前台填写，复用同一 sidecar/adapter、观察与独立验证；新增最小入口参数 --foreground-text-only（命令见下）。只显示一个自有 target，用户点击 Start 后开始，不自动激活，无鼠键/剪贴板降级。仅测试整段文本填写，未扩大场景矩阵。默认双窗口后台入口不变，生产 backgroundVerified=false，后台限定不能因此接受 unknown provider。

原始证据：[report.json](evidence/windows-uia-foreground-2026-10-11/report.json)、[manifest.json](evidence/windows-uia-foreground-2026-10-11/manifest.json)。报告逐字节保存并使用 -text 防止换行转换。

- 1项计划、1项首次通过，步骤661.296ms，重试0；脚本规划、模型调用0，不代表LLM准确率或重复运行可靠率。
- 从启动到完成总37.515秒，其中包含人工等待；明确开始后至清理完成864ms。总时间不能当作文本输入耗时。
- 内容包含中文、emoji、换行、首尾与连续空格；UIA SetValue 返回成功，另起 inspect worker 验证，再经 fixture 的独立 entry.Text oracle 比较，expected/actual完全一致。
- 28次采样全部为target，覆盖807.843ms，最大间隔32.0795ms；仅代表这些离散采样，provesNoTransientChange=false。
- 自有fixtureExited=true、leaseReleased=true；UIA_RELEASED已立即通知，无须用户继续配合。
- 此轮为受控自有WinForms**前台**UIA成功；此前后台轮次仍失败/值未知，未解释其焦点根因。取消专项not-run；不冒充OS鼠键、任意应用、真实模型或完整Pi→OS验收。

显式获准后使用：node scripts/verify-v2-uia.cjs --run-owned-window --foreground-text-only。仅查看计划可用 --foreground-text-only（不加 --run-owned-window），不创建窗口。等待开始最多120秒，执行最多20秒；失焦/Stop/未知结果停止，不重试。此处记录命令不授权新一轮运行。

最终离线验证：入口11/11（含前台target允许、失焦停止、后台默认仍要求sentinel）；C#构建及14/14纯策略自检通过。相关core单元80项：75通过、5项Linux专用跳过、0失败；core/React/Electron类型检查通过。CI终态另记录于PR，Hosted CI真实交互UIA为not run。
