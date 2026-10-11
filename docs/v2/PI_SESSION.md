# Pi 实验会话：真实入口，离线验收

本阶段把现有 `PiEngine` 接到 Electron utility process 和聊天 UI。默认仍走原 Python/WebSocket 路径；`OPEN_EAGLE_V2_FIXTURE=1` 仍选择原 scripted/fake desktop fixture。仅未打包开发环境的 `OPEN_EAGLE_V2_PI=1` 打开本入口，没有失败后自动回退。没有迁移整套旧 BackendApplication、配置系统或旧用户数据。

本次验收全部使用 fake transport / injected fetch、临时文件和本机浏览器。已安装 SDK 的三种真实序列化和 SSE 解析也只接假 fetch。**尚未进行真实供应商或付费调用；用户的 provider、模型和费用预算仍待确认。**

## 启动和界面

使用已有锁定依赖，在仓库根目录编译，并在一个终端保持 Vite 运行：

```powershell
corepack pnpm@10.7.0 --dir packages/agent-core build
corepack pnpm@10.7.0 exec tsc -p tsconfig.electron.json
node scripts/ensure-cjs-package.cjs
corepack pnpm@10.7.0 exec vite --host 127.0.0.1 --port 1420 --strictPort
```

第二个终端运行离线 UI 验证：

```powershell
# 若 Electron 安装脚本未执行，可把 OPENEAGLE_ELECTRON 指向已经验证的 Electron 42.2.0 可执行文件。
corepack pnpm@10.7.0 v2:test:pi-ui
```

验收脚本始终同时设置 `OPEN_EAGLE_V2_PI=1` 和 `OPEN_EAGLE_V2_PI_TEST=1`，使用隐藏窗口、新临时 profile 和假凭据，绝不读取既有设置。它覆盖配置、密码框清空、流式文字、只读 trace、额度、新会话不继承凭据、主动清除、旧会话异步回调、退出清理，以及新 profile 中无假凭据的 UTF-8/UTF-16 明文。

需要手动体验离线界面时，在第二个终端仅为该进程设置两个上述变量，然后用已经安装的 Electron 可执行文件启动此仓库。顶部必须显示 **Pi 离线验收**。在“配置本次会话模型”填写 `https://fixture.invalid/v1`、模型 ID `offline-test` 和任意至少 8 字符、不含空白的假 key，确认两次调用 / 256 输出 tokens / 10000 ms / 0 重试，然后发送“读取本次临时 sample.txt，报告观察结果”。两次调用分别用于工具选择和最终回答。

只有在用户另行确认 provider 和预算后，才考虑真实小额测试：保留 `OPEN_EAGLE_V2_PI=1`，去掉 `OPEN_EAGLE_V2_PI_TEST`，顶部应显示 **Pi 实验会话**；在同一面板填写准确接口、HTTPS API 根地址、模型 ID、新 key 和限制。**key 只在密码输入框输入，不要发到聊天、命令行或 PR 中。**本次工作没有执行此步骤。关闭应用或去掉 Pi 标志会结束本实验会话，默认路径不会继承它的配置。

## 配置、历史和凭据边界

- 选择性复用旧 `pi-model` 的 provider/API 显式映射思想，固定支持 `openai → openai-responses`、`openai-like → openai-completions`、`anthropic → anthropic-messages`。不支持的组合直接拒绝，没有模型目录猜测或隐式 provider fallback。
- HTTPS endpoint 不允许用户名、密码、query 或 fragment；请求 fetch 仅能访问配置的 origin 和路径前缀，拒绝 redirect。SDK 收到显式 key、空 env、SSE、0 重试、timeout 和 maxTokens，不查旧 key 或 ambient auth 文件。
- 新凭据经专用 `v2:model:*` IPC 到 utility 内存，验证主窗口和 main frame。普通 chat Envelope 不携带配置/key；status 只返回 provider、模型和预算摘要。配置不进入 AppSettings，Pi 模式禁止 settings 同步与保存。provider stderr 不转发应用日志。
- 每个 conversation 独立配置、额度和文本历史，最多 8 个配置。运行期间禁止重配或清除；重配是显式重置额度与历史。超过 10 分钟后不能启动新任务，失效记录在访问时移除；断连退出清空表。JS 字符串没有可保证的内存擦除，不声称退出前发生密码学擦除。
- 主机只保留成功对话的脱敏 user/assistant 文本，最多 8 条、JSON 24k 字符。失败/取消不追加；不接受 renderer 注入的 system/tool 历史；每个任务重新建立临时样本，旧工具证据不冒充新文件状态。UI 使用已有 message_delta、message 和 trace 协议，切换会话保持 request/conversation 关联。
- 输出按任务持续脱敏，覆盖跨 chunk、跨模型轮次及最终多 text block；前缀不在单轮结束时 flush。provider 异常只返回固定错误码。输入上限 64k 字符，响应体与解析后事件各 512000 bytes，最多 4096 事件；异常 EOF、取消、超时明确失败。

## 预算与费用

宿主在进入每次 provider 调用前消耗一次额度，失败也计入，不自动重试。一次配置最多 1–12 次调用，每次请求 32–2048 输出 tokens、1000–60000 ms 超时；统一 runtime 另有默认 120 秒任务期限。取消会传播 signal，并在临时目录清理后给终态。

输出 token 参数依赖供应商遵守；终态 usage 超额也会拒绝结果，但不能撤销已发生的计费。HTTP 代理/供应商内部重试、网络中断后的远端执行、系统强杀等也不构成可证明的账单上限。**这些是调用和本地执行限制，不是 USD 硬上限。**

UI/status 一律显示费用 `unknown`。SDK 模型对象要求的零 cost 字段只是占位，不能解读为免费。`estimateCost` 只有在提供已验证价格与完整有效 usage 时返回 `estimated`，缺字段、缺价格或不完整 usage 返回 unknown；当前会话没有可信价格来源，所以不启用估算。

## 工具与后续接口

本阶段工作区选择仅有 **temporary-sample**：宿主新建 `openeagle-pi-workspace-*` 并写入固定 `sample.txt`。只注册 `workspace_list`、`workspace_read`、`workspace_status`，均走 CapabilityRegistry / AgentRuntime 的 prepare、锁、execute、verify 和 SHA256 evidence。拒绝任意路径、额外参数、硬链接、符号链接及超大文件；任务结果前移除临时目录。这是受信任临时目录边界，不声称是操作系统沙箱。

不注册桌面、外网浏览器、命令、附件或日常文件工具。未来接入真实目录要另做目录授权、生命周期与路径边界；桌面继续通过现有 NativeDesktopHelper / broker 接口，浏览器继续通过受限 capability 和 task 生命周期，不能直接把 provider 或 renderer 接到高权限 API。

## 验证命令

```powershell
corepack pnpm@10.7.0 v2:typecheck
corepack pnpm@10.7.0 exec tsc --noEmit
corepack pnpm@10.7.0 exec tsc -p tsconfig.electron.json --noEmit
corepack pnpm@10.7.0 v2:test
corepack pnpm@10.7.0 v2:test:pi-ui
corepack pnpm@10.7.0 v2:test:ui
```

本机浏览器回归通过 `OPENEAGLE_CHROMIUM` 指向已有 Chrome；CI 安装锁定 Playwright 的 Chromium。UI 两项需要前述 Vite 和编译产物。真实供应商账单、生产恢复、真实桌面以及任意用户目录仍不在此次验收范围。


## GUI 接线补充（2026-10-11）

显式选择后台检查后，可单独启动前台专用 fixture 并在窗口内批准；本次任务通过 ModelSessions 的宿主扩展注册同一 desktop_observe/click/type 工具，保持 PiEngine → 统一 runtime → DesktopCapabilityClient → Electron broker → nut-js。普通 Pi 对话仍只注册临时只读工具，不会因先前任务的 GUI 权限获得后续权限。预算默认 2 不变，显式可配置上限调整为 12；离线 GUI 流程需要 8 次假模型调用。

当前已通过假 broker 的 Pi 接线回归，真实 Windows GUI 输入因原生前台 HWND 不匹配及人工批准超时而阻塞，未验收通过。截图仅记录窗口区域 PNG 和摘要，当前文本 Pi 配置不会接收视觉图像块；fixture 规划使用独立语义观察，不代表视觉模型能力。详细证据与限制见 [GUI_WINDOWS.md](./GUI_WINDOWS.md)。
