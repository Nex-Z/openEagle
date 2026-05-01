<p align="center">
  <h1 align="center">openEagle</h1>
  <p align="center">一个能看见你屏幕、替你操作桌面的 AI Agent。</p>
</p>

<p align="center">
  <a href="./README.md">English</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/version-0.1.0-blue" alt="version" />
  <img src="https://img.shields.io/badge/license-MIT-green" alt="license" />
  <img src="https://img.shields.io/badge/platform-Windows%20first%20%7C%20macOS%2FLinux%20planned-lightgrey" alt="platform" />
</p>

---

> 当前处于项目早期阶段，可能还会有各种问题，欢迎来提 issue。

![openEagle 演示](docs/demo.gif)

![openEagle 界面截图](docs/image.png)

## 为什么做这个

大多数 AI 助手能聊天，能执行命令，但很少能**像人一样去真正看着屏幕去动手做事**。

openEagle 填补了"理解你想做什么"和"在你的电脑上实际完成"之间的鸿沟。它实时观看你的屏幕，分析看到的内容，然后操控鼠标、键盘和应用程序——同时通过三级安全模型让你始终保持控制。

**Chat 模式**适合需要 AI 搜索信息、读写文件、执行命令的场景。**SOLO 模式**适合需要 AI 操作 GUI 界面、填写表单、跨应用完成多步骤流程的场景——那些通常只有人类才能做的事情。

> 已尝试的场景：
>  - 直接操作浏览器收集信息
>  - 操作客户端软件（播放暂停、音乐）
>  - ...

## 快速上手（5 分钟）

### 环境准备

- [Node.js](https://nodejs.org/)（需启用 corepack）
- [Python](https://python.org/) >= 3.12
- [uv](https://docs.astral.sh/uv/)（Python 包管理器）
- [Rust](https://rustup.rs/)（Tauri 编译需要）

### 安装与运行

```bash
# 启用 pnpm
corepack enable
corepack prepare pnpm@10.7.0 --activate

# 安装前端依赖
pnpm install

# 安装后端依赖
uv sync --project ./backend

# 启动应用
pnpm tauri:dev
```

搞定。开发模式下 Tauri 会自动拉起 Python 后端；打包版本会通过 sidecar 启动，无需手动启动服务器。

### 底层流程

```
Tauri (Rust)  →  开发模式启动 uv/Python，打包版本启动 Python sidecar（随机端口）
Python        →  向 stdout 输出 [AGENT_READY] WS_PORT: <端口>
Rust          →  解析端口，通知前端
Frontend      →  通过 WebSocket 连接 ws://127.0.0.1:<端口>/ws
```

## 架构

```
┌──────────────────────────────────────────────────────┐
│                  Tauri 壳层 (Rust)                     │
│  进程生命周期 · 截图 · 输入注入 · 悬浮窗 · 通知        │
├──────────────────────────────────────────────────────┤
│              Python 后端 (FastAPI)                     │
│  Main/Sub-Agent Runtime · SOLO 编排 · 工具执行         │
│  安全评估 · Prompt 引擎 · 多模型路由                    │
├──────────────────────────────────────────────────────┤
│             React 前端 (TypeScript)                    │
│  对话界面 · SOLO 悬浮窗 · 执行面板 · 设置               │
│  深色/浅色主题 · 响应式布局                             │
└──────────────────────────────────────────────────────┘
         ↕ WebSocket（Envelope 协议）
```

### 技术栈

| 层级       | 技术                              |
| ---------- | --------------------------------- |
| 桌面壳     | Tauri 2, Rust                     |
| 前端       | React 18, TypeScript, Vite        |
| 后端       | Python 3.12+, FastAPI, WebSocket  |
| LLM        | OpenAI 兼容 API（可配置）         |
| 视觉模型   | VL 模型，通过 OpenAI 兼容接口接入 |
| 自动化     | mss（截图）, pyautogui（输入）    |
| Agent 框架 | agno                              |
| 搜索       | baidusearch（免费，无需 API Key） |
| 远程 IM    | 飞书长连接、Telegram Bot 长轮询   |

### Main/Sub-Agent Runtime

所有用户消息都会先进入轻量 main agent。main agent 只负责理解意图和调度：直接回答、委派给干净 worker、启动 SOLO，或控制已有 SOLO。worker 使用内部 scoped conversation id 执行任务，前台会话只保留摘要、证据和最终结果，减少上下文污染。

内置 worker 类型：

| Worker | 用途 |
| ------ | ---- |
| `general` | 通用解释、轻量工具、只读检查 |
| `coding` | 代码修改、文档、构建、测试、写文件任务 |
| `research` | 搜索、查询、信息整理 |
| `solo` | 通过 SOLO 执行视觉桌面任务 |

只读 worker 可以保守并发；代码/写入任务与 SOLO 串行执行，避免文件和桌面操作互相打架。

### SOLO 模式 — 工作原理

SOLO 是一个**目标驱动的 Agent**，而非按固定脚本执行的任务器：

1. **观察** — 截取当前桌面屏幕
2. **思考** — VL 模型分析截图 + 任务目标 + 历史记录
3. **行动** — 执行一个操作（点击、输入、滚动等）
4. **循环** — 重复直到任务完成或手动停止

模型在每一步自主决策。没有预编程脚本，没有脆弱的元素选择器。纯视觉理解 + 推理。

SOLO 的稳定性策略保持温和：用 action signature 判断真正重复的动作，用 no-op / uncertain / failed 区分执行结果，只在恢复模式下降级 batch。`open_url`、点击、双击、`press_keys` 这类可能触发页面加载的动作会做短暂的动作后截图稳定采样，避免下一轮模型看到过早的 loading 帧。这样可以减少重复点击和卡屏，同时不把通用任务框成固定脚本。

### 远程 IM 控制

在 **Settings -> IM** 中启用飞书或 Telegram 后，openEagle 可以从远程会话接收任务。

- 飞书需要填写应用的 `App ID` 和 `App Secret`。白名单支持单个用户的 `open_id`，也支持私聊或群聊的 `chat_id`。如果不知道该填什么，可以先给机器人发一条消息，再从 IM 状态面板里的“最近拦截 open_id / chat_id”复制。
- Telegram 需要填写 Bot Token。白名单支持 `user_id` 或 `chat_id`。
- 白名单为空时默认拦截所有远程消息。
- 远程普通文本默认启动 SOLO 任务；只想文字聊天时使用 `/chat <内容>`。

远程命令：

| 命令 | 行为 |
| ---- | ---- |
| `<普通文本>` | 启动 SOLO 桌面任务 |
| `/solo <任务>` | 显式启动 SOLO 桌面任务 |
| `/chat <内容>` | 只进入 Chat 对话 |
| `/pause`、`/resume`、`/stop` | 控制当前 SOLO 任务 |
| `/allow`、`/reject` | 确认或拒绝待确认动作 |
| `/help` | 查看命令帮助 |

### 安全模型

每个操作都经过三级风险评估：

| 级别      | 行为                             |
| --------- | -------------------------------- |
| `safe`    | 立即执行（低风险桌面动作、只读工具等）         |
| `confirm` | 等待你确认（写文件、删除/移动、系统快捷键等） |
| `blocked` | 直接拒绝（危险命令如 `rm -rf`）              |

额外护栏：最多 150 步、动作签名重复检测、截图无变化检测、恢复模式下抑制 batch；动作参数格式错误或执行失败会优先反馈给当前 agent 自修复，而不是直接抛给用户。

## 配置

openEagle 支持灵活的模型路由——Chat（文本）和 Vision-Language（视觉）任务可使用不同的提供商和模型，支持任何 OpenAI 兼容 API。

关键设置（通过应用内设置面板访问）：

| 设置项              | 说明                                          |
| ------------------- | --------------------------------------------- |
| `agent.provider`  | 文本模型提供商（`openai`、`openai-like`、`mock`） |
| `agent.modelId`   | 对话用的文本模型                                  |
| `agent.baseUrl`   | OpenAI 兼容 API 的自定义地址                      |
| `agent.vlProvider` | SOLO 视觉模型提供商（`openai`、`openai-like`）    |
| `agent.vlModelId` | SOLO 用的视觉语言模型                             |
| `agent.vlBaseUrl` | 视觉模型的 OpenAI 兼容 API 地址                    |
| `feishu.enabled` | 启用飞书远程入口                                  |
| `feishu.appId` / `feishu.appSecret` | 飞书长连接应用凭据                 |
| `feishu.allowedOpenIds` / `feishu.allowedChatIds` | 飞书用户/会话白名单     |
| `telegram.enabled` | 启用 Telegram 远程入口                            |
| `telegram.botToken` | Telegram Bot API Token                          |
| `telegram.allowedUserIds` / `telegram.allowedChatIds` | Telegram 用户/会话白名单 |
| `tools`           | 自定义工具定义                                    |
| `mcp`             | MCP 服务器连接                                    |
| `skills`          | 自定义 Skill Prompt                               |

## Roadmap

- [ ] 离开主窗口后，执行状态也能更清楚地呈现给用户
- [ ] 适配更多的模型（目前主要测试过阿里云百炼、小米 MiMo）
- [ ] 类似 OpenClaw 那样的定时任务
- [ ] 更快的响应操作
- [ ] 良好的自迭代上下文管理机制
- [ ] macOS 和 Linux 支持
- [ ] 社区插件系统
- [ ] 多显示器感知
- [ ] 会话回放与更完整的历史记录
- [ ] 语音输入

## 参与贡献

欢迎贡献！以下是参与方式：

1. **Fork** 仓库
2. **Clone** 你的 Fork：`git clone https://github.com/YOUR_USERNAME/openEagle.git`
3. **创建分支**：`git checkout -b feature/我的功能`
4. **编写代码**
5. **提交前验证**：
   ```bash
   pnpm -s tsc --noEmit                          # 前端类型检查
   uv run python -m compileall backend/app       # 后端语法检查
   cd src-tauri && cargo check                   # Rust 检查（如修改了 Rust 代码）
   ```
6. **提交**，遵循 [Conventional Commits](https://www.conventionalcommits.org/) 规范：
   ```
   feat: 添加多显示器支持
   fix: 修复超宽屏下悬浮窗定位问题
   ```
7. **Push** 并创建 Pull Request

### 需要帮助的方向

- **SOLO 可靠性** — 在不同应用和工作流中测试
- **优秀的上下文管理机制** — 类似 OpenClaw、Hermes 的上下文管理机制
- **跨平台** — macOS 和 Linux 适配
- **工具与集成** — 新的内置工具、MCP 服务器、Skills
- **UI 动效** — 更好的 UI 设计和人机交互
- **文档** — 使用指南、教程、翻译
- **测试** — 后端单元测试、前端组件测试

详细编码规范请参阅 [AGENT.md](./AGENT.md)。

## 许可证

[MIT](./LICENSE)

---

<p align="center">
  用好奇心和无数张截图打造。
</p>
