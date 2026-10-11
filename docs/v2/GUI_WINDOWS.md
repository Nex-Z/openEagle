# Windows GUI 接线与本机验收边界

日期：2026-10-11。状态：**实现接线与后台回归完成；真实 OS GUI 交互验收阻塞，不能标记通过。**

## 实现与复用

- 复用原 desktop-tools 的 capability/client/ticket 通道契约，保持 DesktopCapabilityClient → Electron 主进程 broker → 原 input.ts/nut-js 驱动。未复制旧 BackendApplication，也未新增 native 栈。
- CapabilityTicketStore 与旧工作区源文件 SHA256 完全相同：e1f0e1a5f1d5b42a4901375362fa197c1f0552b6ab12889b6a53839c30099c53。错误消费也烧毁票据；绑定 request、参数摘要、authority epoch 和时限，不能重用。
- gui-capabilities.ts 是通用工具适配器：desktop_observe、desktop_click、desktop_type；动作参数为 target、observationId、窗口内容区 DIP 坐标或文本，不包含 entry/save 或固定业务文本。
- GuiBroker 接收宿主 GuiBinding，负责窗口身份、焦点、新鲜观察、票据、串行执行及停止。GuiFixture 独立提供唯一获授权的自有 BrowserWindow、固定三步批准策略、控件布局和独立结果 oracle。当前可绑定宿主提供的 Electron 窗口；任意外部 HWND 应用绑定未实现、未授权。
- screen.capture 改用已有 nut-js screen.grabRegion 与 screenshot.ts PNG 编码器，仅请求目标内容区域；不使用 capturePage、Playwright 截图或 sendInputEvent 作为 OS 证据。只支持完整处于主显示器内的目标，越界拒绝。
- Pi 入口通过 ModelSessions 的宿主扩展注册同一工具，不另造执行器。普通任务仍仅临时只读工具；专用 GUI 任务才可扩展。fake transport 驱动真实 Pi 循环的接线单元测试通过；真实 Pi → OS 输入端到端仍未验收。

## 后台与前台

后台限定请求返回 needsForeground，不打开窗口、不注册输入工具、不自动重试。当前无 UIA 后台驱动，nut-js 如实标记 foreground、usesSystemInput=true、mayChangeFocus=true，取消为输入间协作式。观察工具不使用系统输入，但仍要求当前前台租约，不能称后台窗口捕获能力。

UI 先显示后台检查，再单独启动专用前台窗口；正常路径还需要窗口内批准固定流程。只有显式宿主测试旗标可使用已有测试授权，renderer 无法设置旗标。Stop 按钮、Esc、聊天取消、断连、窗口关闭、失焦、移动/缩放/导航均使租约失效；不调用 SetForegroundWindow 或 bringToFront 重抢焦点。输入抛错或结果未验证也停止整个租约，禁止未知副作用后的盲目重试。

桌面互斥包括进程内 owner 与临时目录独占 mkdir，阻止同一用户的多个本应用进程同时输入。异常退出遗留锁会失败关闭，不自动删除来猜测旧进程已死亡。输入前后核对目标原生 HWND 与 Electron 焦点；鼠标移动后再次核对，输入每个字符前后再次检查；只释放本次调用按下的鼠标按钮，不进行全局 reset，不阻断用户输入。

**限制**：没有可靠的用户物理键鼠识别。指针位置检测只能发现部分接管；无法承诺零干扰，也不能消除检查到输入之间的 OS 调度竞态。不支持安全桌面、锁屏、任意遮挡、跨显示器坐标、任意应用或 UIA。当前文本模型只收到语义状态/PNG 文件名及摘要，不收到视觉图像块。

## 已执行的验证

| 验证 | 结果 | 能证明什么 |
|---|---|---|
| Windows 完整核心测试 | 76 项：71 passed、0 failed、5 Linux-only skipped | 真实 Chrome loopback、files、CLI 与核心/Pi 回归；含 2 个 GUI 接线单元测试 |
| GUI admission/ticket 策略 | 4/4 passed | 重放、停止、过期、状态变化、参数/请求/epoch 绑定、拒绝与一次性票据 |
| 隐藏 Electron Pi UI | 通过 | 既有模型配置、隔离、假模型工具、预算、清理；不证明 OS GUI 输入 |
| 核心/React/Electron 类型检查 | 通过 | 编译契约 |
| Vite 构建、Python compileall | 通过 | 构建/语法 |
| 真实 GUI 自动初始化第 1 轮 | GUI_NATIVE_HANDLE_MISMATCH，输入前停止 | Electron isFocused 与 Windows 前台不一致，安全检查生效 |
| 真实 GUI 诊断第 2 轮 | 同样 HWND 不匹配，输入前停止 | 未通过放宽检查或抢焦点补救 |
| 真实 GUI 等待人工批准轮 | 30 秒 GUI_APPROVAL_TIMEOUT，随后关闭 | 本次没有获得窗口内批准，没有输入 |

两次自动轮次加一次人工等待均未完成；不是“成功 3 次”。真实成功任务 0，成功输入步骤 0，成功 OS 截图证据 0；成功步骤耗时、首次准确率、最终准确率均 **N/A（未验证）**，不能填成 100%。没有原生动作自动重试。实现过程发生两次编译问题修复（编辑换行/截断、Pi transcript 类型）和一次新增票据测试预期修正；不声称首次实现准确。

成功后脚本会记录每步 durationMs、firstAttempt、retries=0、工具状态、独立 oracle、受信任 click/input 计数和 PNG SHA256。fake Pi 流程预计 8 次假模型调用、7 次工具调用；这些计数已经由 fake broker 单元测试验证，**不是成功的 OS 执行数据，也不是 LLM 准确率**。脚本规划、假模型、真实模型须分开标记。

真实场景脚本已覆盖 success、denied、stale、closed、focus、cancel、lease、replay、pi 的断言，但因前台初始化阻塞，后续交互场景未执行通过。不能把“已写测试”记成“已验收”。CI 仅运行 headless 核心与 GUI 策略测试，交互 GUI 在托管 CI **not run**。

## 复现

先使用项目命令级 corepack pnpm@10.7.0 编译核心及 Electron，运行 scripts/ensure-cjs-package.cjs，再启动 127.0.0.1:1420 的既有 Vite 入口。OPENEAGLE_ELECTRON 可指向本机已有 Electron 42.2.0 二进制，无需重新下载。

- 纯 GUI UI：OPEN_EAGLE_V2_GUI=1；Pi UI：OPEN_EAGLE_V2_PI=1。均默认关闭、仅开发环境，保留旧路径和 fake fixture 回退。
- 本机可交互且用户明确腾出前台时：运行 node scripts/start-v2-gui-manual.cjs；该一次性入口只运行 success，打印精确窗口标题和目标原生 HWND，等待 30 秒，在专用窗口内点击批准。失焦就结束本次测试，不重抢。
- 后续逐个运行其余场景；Pi 场景仅 OPEN_EAGLE_V2_PI_TEST 的假模型，不读取旧 key，不请求真实供应商。脚本配置的 8 次额度仅供此假 transport。
- GUI 策略：corepack pnpm@10.7.0 v2:test:gui-policy。

此轮没有安装 VM、新系统用户、新依赖或管理员权限，没有操作日常软件、用户文件或 browser profile。测试窗口由 finally/close 销毁；任务 workspace 清理，证据目录单独保留。本轮未使用全局进程名终止或终止 WSL。前两轮使用宿主受限测试授权，已通过产品批准门；诊断轮 Electron isFocused=true，目标 HWND=4850774、nut-js 系统前台 HWND=461958。无法从现有证据断定锁屏，只能确认 Windows 前台身份不匹配；第三轮才是人工批准超时。此前完整随机标题未记录，下一次一次性入口会打印精确标题。下一次真实验收需要用户在电脑前解锁（如锁定）并主动批准自有测试窗口。
