> 当前 Windows 可信工作区 opt-in 已实测；文中早期 Linux-only 和缺少 cleanup 的记录由末尾更新取代。UI 仍未接入。

# openEagle v2：通用 Agent 的首个 headless 切片

## 当前交付边界

这不是 coding agent，也不是只能操作网页的脚本框架。一个任务共享目标、工作区和已验证证据，由一个可替换引擎使用统一能力目录；文件、命令和浏览器可以混合调用。Pi 只负责模型/工具循环，不拥有授权、资源锁或执行器。

本次增加独立 ESM workspace `@open-eagle/agent-core`。现有 Electron/React 和 Python 后端没有移除，也没有切换聊天默认路径。当前可运行入口是 headless API 与本地演示；没有声称完成 Windows/macOS 桌面功能、真实模型验收或安装包整合。

## 使用

要求 Node >=22.19.0、仓库约定 pnpm 10.7.0。首次安装：

```sh
pnpm install --frozen-lockfile --ignore-scripts
pnpm --filter @open-eagle/agent-core exec playwright install chromium
pnpm v2:typecheck
pnpm --filter @open-eagle/agent-core test:unit
pnpm --filter @open-eagle/agent-core test:browser
pnpm v2:demo
```

`--ignore-scripts` 适用于此 headless 切片，避免启动无关 Electron/native 安装脚本；它不是完整桌面安装说明。已有 Chromium 时可设置 `OPENEAGLE_CHROMIUM=/usr/bin/chromium`，没有时使用 Playwright 安装的 Chromium。浏览器测试需要允许启动本地进程和 loopback/socket；受限执行环境可能阻止 Chromium，必须报告失败，不能替换为 mock 后声称通过。

演示会创建临时工作区、全新浏览器上下文和仅监听 127.0.0.1 的夹具服务器：

1. 从真实页面读取随机编号
2. 填入并提交本地表单，页面与服务端产生回执
3. 用文件适配器保存回执
4. 用严格匹配参数的 Node CLI 配置计算 SHA-256
5. 再读文件，并在引擎之外独立核对服务端提交状态、文件字节和 CLI 摘要

结束后关闭浏览器/服务器并清理临时文件。演示的规划器是 ScriptedEngine，不是模型。Pi 测试另行运行真实发布包的循环，但传输是确定性假传输，没有访问任何模型或密钥。

## 嵌入接口

入口：`packages/agent-core/src/index.ts`，编译后 `dist/src/index.js`。

```ts
import { AgentRuntime, CapabilityRegistry, PiEngine, createFileCapabilities } from '@open-eagle/agent-core';
const registry = new CapabilityRegistry();
createFileCapabilities().forEach(capability => registry.register(capability));
const runtime = new AgentRuntime({
  registry,
  authorize: async (request, signal) => trustedHostApproval(request, signal),
  onRecord: record => showActionRecord(record),
});
const engine = new PiEngine({model, streamFn, maxTurns: 12});
const task = runtime.start({goal: userText, workspace: approvedWorkspace}, engine);
// task.cancel();
const result = await task.result;
```

其中 model、streamFn、trustedHostApproval、showActionRecord 均须由宿主显式提供，不是本仓库已经接好的模型设置/UI。PiEngine 不自动读环境密钥、选择供应商或加载 coding 工具。源码只在 Pi adapter 依赖 Pi 类型，替换引擎不影响能力契约。

`TaskResult.status=completed` 只表示引擎正常结束，不代表用户目标已验证。逐动作的 `succeeded` 需要适配器独立校验；`denied`、`failed`、`unverified`、`cancelled`、`timed-out` 都必须展示。模型答案不能改变记录。仅 succeeded 的证据加入共享 task facts。

## 授权与执行

- 不提供 authorize 时，仅 read 效果自动执行，其余默认拒绝。write、execute、destructive、external-submit、sensitive-setting 由可信适配器声明，模型传入 `approve:true` 无效
- 批准绑定 action ID、task ID、参数、目标、资源、效果、版本指纹和摘要。锁内重新准备，并在批准后重新核对；批准不跨动作复用
- 文件覆盖会额外标为 destructive；所有 CLI 都是 execute；浏览器导航、点击和填值均需外部提交授权（填值可能触发 oninput 网络请求）
- 单进程共享资源锁协调多个 runtime；文件和 CLI 使用同一工作区键，浏览器使用会话键。没有实现跨进程分布式锁
- 任务/动作截止时间、动作预算、Pi 轮数预算与取消分别有界。模型/批准等待可中断；动作执行取消后必须清理完毕才释放锁，不用 Promise.race 假装停止副作用
- 适配器和授权回调是可信宿主代码，不是隔离插件。执行器必须遵守 AbortSignal；任意不配合的第三方执行器仍可能卡住，需要进程隔离
- 记录默认仅驻留内存；参数与输出可能含用户内容，生产持久化之前需要脱敏、访问控制和保留策略

## 各执行面的真实边界

### 文件

当前 Linux 实现通过目录描述符和 O_NOFOLLOW 拒绝路径越界/符号链接，普通文本最多 1 MiB；父目录必须存在。写入后读取真实字节校验 SHA-256。没有删除工具。新增文件原子链接，覆盖原子替换。

这是可信工作区适配器，不是针对恶意并发进程的 OS 沙箱；另一个程序仍可能移动已打开的目录，或在检查/替换之间竞争。Windows/macOS 当前明确 unavailable，等待等效安全打开机制；不以 realpath 预检查冒充抗竞争隔离。

### CLI

宿主注册命名命令配置，固定绝对可执行文件，校验 argv，固定工作区 cwd，只使用该配置的最小环境，shell=false，限制输出和时间。没有任意 shell 或模型提供可执行路径。Linux 进程组清理普通后代；主动 setsid 逃逸需要容器/cgroup 等 OS 隔离。本版本 CLI 不宣称是 OS 沙箱。Windows/macOS 暂不可用，需 Job Object/等效进程监督。成功退出码仅证明命令正常退出，不证明任意业务目标完成。

### 浏览器

新建隔离 Chromium 上下文，不连接用户 profile、CDP 或当前登录会话。默认没有允许来源；宿主指定精确 HTTP(S) origin。阻止其它来源子资源/重定向、WebSocket、service worker、弹窗、下载，并限制在单一来源会话。DOM 输出是外部未可信内容，只能作为观察，不能给自己授权。先观察得到 ref，再填值/点击；绑定观察指纹并执行前复核。点击必须看到指定的新增文本/URL 才标为 verified，否则为 unverified。

来源限制不是 OS 网络防火墙，不代表允许向该来源做任何交易。普通网页自动脚本/同源后台请求可能在导航后运行，数据类型和业务提交仍需可信宿主审查。此切片不用于登录、支付、敏感信息或任意不可信站点。真实受控环境仍需网络层隔离。没有截图/坐标兜底，也没有把页面文字或 DOM 变化当作业务完成保证。

### 原生桌面

`NativeDesktopHelper` 描述未来 Windows UIA / macOS Accessibility 语义观察、基于 observationId 的操作和关闭边界。两项能力均声明 unsupported，不进入模型工具目录；没有假 helper 或 Linux 冒充 Windows 测试。

## 后续整合顺序

1. 在 Electron 主进程/专用 Node 子进程加入单一执行所有者，建立 authenticated IPC 与可信批准 UI；CJS 主进程动态加载 ESM 或使用子进程，不让 renderer 直接导入能力
2. 映射现有 Envelope 的 requestId/conversationId、逐动作记录、取消与批准。当前 client:send_message 仍走 Python，旧 stopSolo 不会取消新 runtime
3. 保留 Python feature-flag 回退，补 Windows 文件/CLI helper，再实现 UIA 和 macOS Accessibility，在对应 OS 做真实验收
4. 接入用户显式配置的模型，做同任务同模型的成功率、成本、时延对照；当前没有性能收益或 ChatGPT 级能力结论
5. 单独处理旧 WebSocket/asset/webSecurity 安全问题，不能认为此 headless 包已经修复旧通道

## 依赖与官方资料

2026-10-10 使用 npm registry 元数据确认发布版本并精确固定：`@earendil-works/pi-agent-core@1.1.0`、`@earendil-works/pi-ai@1.1.0`、`typebox@1.3.27`、`playwright@1.64.0`。以安装后的发布包 API/类型为准，不照抄未发布 main。

- [Pi 官方源码与使用文档](https://github.com/earendil-works/pi/tree/main/packages/agent)
- [Pi agent-core 官方 npm metadata](https://registry.npmjs.org/@earendil-works%2fpi-agent-core/1.1.0)
- [OpenAI Computer Use 官方指南](https://developers.openai.com/api/docs/guides/tools-computer-use)
- [自有 UI 工具整合](https://developers.openai.com/api/docs/guides/tools-computer-use-integration)

以上 OpenAI 资料是公开 API/集成建议，不代表知道闭源 ChatGPT 客户端内部实现。

实际通过/阻塞记录见 [VERIFICATION.md](./VERIFICATION.md)。

## Windows 本机验收补充

Windows 可使用已安装的 Chrome，仍由 Playwright 创建专用临时 profile：

```powershell
$env:OPENEAGLE_CHROMIUM='C:\Program Files\Google\Chrome\Application\chrome.exe'
corepack pnpm@10.7.0 --filter @open-eagle/agent-core test:browser
```

当前网络策略明确拒绝所有 HTTP 3xx 响应（包括同源重定向）。真实浏览器验收发现 `route.continue()` 后的重定向可能绕过来源检查，因此改为 `route.fetch({maxRedirects:0,maxRetries:0})` 并拒绝 3xx，再传递非重定向响应。不会重试表单提交。此策略不适用于依赖登录跳转、重定向或流式响应的网站；fetch 会缓冲响应，DOM 字符上限不是网络响应体上限。仅用于受控本地 fixture，不是通用网络沙箱。

Windows 文件与 CLI 仍默认且明确不可用，增加了直接 prepare 也拒绝执行的回归。没有用 realpath/lstat 或 taskkill 的 best-effort 行为替代 Linux 现有保证。安全路径句柄与 Job Object 实现需单独设计；不把 cwd 检查称为 OS 沙箱。真实 Windows browser→file→CLI 混合闭环仍未完成。

### 浏览器宿主生命周期约束

构造器只接受规范 origin 且 hostname 为 `127.0.0.1` 或 `[::1]`；拒绝 DNS 名称及外部来源。loopback 也可能是真实本地服务，宿主必须仅传入自己建立的 fixture 端口。能力描述同步披露此限制，会话首次 prepare 绑定 task ID，拒绝跨任务复用。

每任务必须创建独立 browser session，并在 `await task.result` 的 `finally` 中 `await session.close()`。动作运行时取消会关闭会话，但模型/审批等待时 runtime 不自动持有并关闭 adapter；宿主不清理时，已打开页面的同源后台脚本可能继续运行。当前 headless API 尚无通用 adapter disposal hook，不能宣称任意时刻取消即完成浏览器资源回收。原生 UI 接入前须补齐生命周期集成回归。

## Windows opt-in 与复用整合更新

已有文件/命令/桌面工具不是本轮发明；参见 [REUSE_AUDIT.md](REUSE_AUDIT.md)。Linux factory 保持原行为。Windows 通过单独导出显式选择：

```ts
createWindowsTrustedFileCapabilities({mode: 'trusted-workspace'});
createWindowsTrustedCliCapability({mode: 'trusted-workspace', profiles: approvedProfiles});
new AgentRuntime({registry, authorize, onMetric: collector.collect, cleanup: () => browser.close()});
```

只用于用户批准的可信本地目录、普通用户权限；不会提权。文件拒绝ADS/保留名/设备路径/尾点空格和已识别junction，复用原Node目录快照再加文件身份/hash复核。Node的lstat不是完整reparse-tag扫描，无法抵御恶意并发替换或别名竞态；不是OS沙箱。

CLI只运行宿主固定exe和窄argv，shell:false、windowsHide、最小env、输出/超时有界。取消时taskkill仅针对自有存活PID树，结果如实标descendants-unverified；主进程退出仍未确认则该进程内隔离相交工作区，确认exit后解除。进程重启不保留隔离；正常主进程结束后的逃逸后代不保证清除。退出码0仅是进程检查，不等于任务语义成功。

cleanup现在由runtime在结果交付前等待，覆盖模型/审批等待期间取消。带cleanup的runtime只允许一个任务，宿主每任务创建独立registry/session；cleanup失败结果failed，cleanup不响应时会等待，不能用超时race假装已释放资源。

Windows混合验收：设置 `OPENEAGLE_CHROMIUM`，并仅为本地demo显式设置 `OPENEAGLE_WINDOWS_TRUSTED=1`，运行 `corepack pnpm@10.7.0 v2:demo`。真实浏览器/文件/CLI已闭环，规划是ScriptedEngine，非真实LLM。

## 最小可观测性与验收

`MetricsCollector`输出不含prompt/参数/内容的JSONL事件：task/step/attempt ID、单调时钟、phase span、调用计数、handoff状态、provider返回的usage。model/prepare/lock/approval/tool/verify/cleanup分开；并发span相加是resource-time，不能用walltime相减推导。费用无版本化价格来源时unknown。重试/回退/重规划/重复观察/无效动作/人工接管由显式metric事件标注；不能自动推断所有恢复，未完整观察时cleanSuccess unknown。正常确认和必要观察不算补救。

runtimeStatus、adapterStatusCounts、taskOracle分开；独立语义checkpoint的最终oracle覆盖率与首次尝试可判定覆盖率分开，unknown不算正确。现有fixture预定义nonce、服务端回执、文件字节、CLI摘要四个checkpoint，不要求未来任务遵循唯一工具序列。

运行 `corepack pnpm@10.7.0 --filter @open-eagle/agent-core report:fixtures` 生成JSON。已提交WINDOWS_FIXTURE_REPORT.json及脱敏TRACE.jsonl。样本为3个随机nonce混合任务+1个刻意拒绝写入任务，失败开销保留；这是报告管线验收，不是总体能力成功率。旧版、真实模型、相同条件的性能对比均未测，不宣称提升。
