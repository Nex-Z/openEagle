# feat: 新增可替换 Pi 引擎的通用 Agent headless 首版

新增独立 `@open-eagle/agent-core`，将模型工具循环与权限、执行记录、结果验证、取消和资源锁分离。同一任务可通过统一能力目录组合工具；Pi 是可替换引擎，不负责授予权限。原 Electron/React 与 Python 后端保留，当前 UI 默认链路未切换。

## 已实现

- 默认拒绝副作用动作；审批绑定动作、参数、目标和观察指纹，执行前复核。
- 结构化执行记录、独立验证与成功证据、取消/超时/预算、进程内共享资源锁。
- 真实 Pi agent-core 循环适配，以及受控本地浏览器、Linux 文件和固定 CLI profile 适配器。
- 真实 Chrome 验收发现并修复跨源重定向泄漏；所有 HTTP 3xx 均拒绝，POST 不自动重试。
- Windows files/CLI 明确不可用，增加直接 prepare 拒绝回归；新增 Windows headless CI。

## 验证

- 云端交接基线：31 项 runtime/Pi/Linux filesCLI 测试通过；此为 Linux 结果，不代表 Windows 文件/CLI 通过。
- Windows Node 24.8.0 / 命令级 pnpm 10.7.0，官方 registry 冻结锁文件安装成功。
- Agent、React、Electron TypeScript 检查通过。
- Windows unit：27 passed / 0 failed / 5 skipped；Pi 使用真实包循环加模拟模型传输，没有真实模型或付费调用。
- Windows Chrome 154.0.8037.99：真实临时 profile、本地 fixture，10 passed / 0 failed / 1 skipped；混合 browser/file/CLI demo 跳过。
- `git diff --check` 通过。远端 CI 尚未运行；详细记录见 `docs/v2/VERIFICATION.md`。

## 限制与后续

- 这是 headless core，尚未接入 Electron UI、审批界面或安装包。Windows UIA、macOS Accessibility 未实现。
- Windows files/CLI 与完整 browser→file→CLI 闭环未完成；没有弱化 Linux 保证，也没有启用可信工作区替代模式。
- 浏览器仅用于受控本地 fixture；拒绝所有重定向，响应经 fetch 缓冲且无网络响应体硬上限，不支持通用登录站点/流式网站，不是 OS 网络沙箱。
- DOM/同源脚本仍不可信；审批指纹不是对抗任意页面脚本的事务隔离，页面后台同源副作用不保证逐项审批。
- 依赖安装使用 `--ignore-scripts`：支持已验证的 TypeScript/headless 路径，但 Electron 二进制和 native 依赖安装脚本未执行，不能据此宣称桌面启动或打包可用。
- 没有读取用户浏览器 profile、模型密钥或真实网站账户，未修改真实系统设置。

保持 Draft，等待 Windows 权限模式与后续集成设计审阅；不合并。

独立审查补充：构造器强制字面 loopback origin，能力声明 fixture-only，会话绑定 task ID。loopback 不证明服务为 fixture，宿主仍须只传自己创建的端口。模型/审批空闲时取消尚无通用 adapter disposal hook，宿主必须每任务建立独立 session，并在 task.result 的 finally 中关闭，未实现完整 UI 生命周期。
