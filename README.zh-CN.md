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

openEagle 填补了"理解你想做什么"和"在你的电脑上实际完成"之间的鸿沟。你只需要和一个 main agent 对话；它可以直接回答、调用工具、委派 worker，或在需要 GUI 操作时调度桌面执行 worker 去观看屏幕并操控鼠标、键盘和应用程序——同时通过三级安全模型让你始终保持控制。

现在不再需要手动切换模式。main agent 会根据意图决定自然对话、工具型 worker 任务，还是调度桌面执行 worker 完成 GUI 导航、填写表单、跨应用多步骤操作等任务。

> 已尝试的场景：
>  - 直接操作浏览器收集信息
>  - 操作客户端软件（播放暂停、音乐）
>  - 操作word，编写文档
>  - ...

## 快速上手（5 分钟）

### 环境准备

- [Node.js](https://nodejs.org/)（需启用 corepack）
- [Python](https://python.org/) >= 3.12
- [uv](https://docs.astral.sh/uv/)（Python 包管理器）

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
pnpm electron:dev
```

搞定。`pnpm electron:dev` 是一条命令启动器：它会编译 Electron 主进程、启动 Vite dev server、拉起 Electron，然后由 Electron 自动启动 Python 后端。打包版本会通过 Python sidecar 启动，无需手动启动服务器。

### 打包与发布

```bash
pnpm package:windows
```

该命令会先构建 Python sidecar，再在 `release/` 下生成带版本号的 Windows 安装包，例如 `openEagle-0.1.0-win-x64.exe`。GitHub Actions 里的 `Package Desktop` workflow 会构建 Windows、macOS、Linux 三个平台产物。手动触发时填写 `release_tag`（例如 `v0.1.0`）会创建或更新对应 GitHub Release；留空则只生成可下载的 workflow artifacts。推送 `v*` tag 时也会自动把产物上传到对应 Release。

### 底层流程

```
Dev launcher  →  编译 Electron，启动 Vite，再拉起桌面壳
Electron      →  开发模式启动 uv/Python，打包版本启动 Python sidecar（随机端口）
Python        →  向 stdout 输出 [AGENT_READY] WS_PORT: <端口>
Electron      →  解析端口，通知前端
Frontend      →  通过 WebSocket 连接 ws://127.0.0.1:<端口>/ws
```

## 架构

```
┌──────────────────────────────────────────────────────┐
│                Electron 壳层 (Node.js)                  │
│  进程生命周期 · 截图 · 输入注入 · 悬浮窗 · 通知        │
├──────────────────────────────────────────────────────┤
│              Python 后端 (FastAPI)                     │
│  Main/Sub-Agent Runtime · 桌面执行编排 · 工具执行      │
│  安全评估 · Prompt 引擎 · 多模型路由                    │
├──────────────────────────────────────────────────────┤
│             React 前端 (TypeScript)                    │
│  对话界面 · 桌面执行悬浮窗 · 执行面板 · 设置            │
│  深色/浅色主题 · 响应式布局                             │
└──────────────────────────────────────────────────────┘
         ↕ WebSocket（Envelope 协议）
```

### 技术栈

| 层级       | 技术                                                   |
| ---------- | ------------------------------------------------------ |
| 桌面壳     | Electron 42, Node.js                                         |
| 前端       | React 18, TypeScript, Vite                             |
| 后端       | Python 3.12+, FastAPI, WebSocket                       |
| LLM        | OpenAI 兼容 API（可配置）                              |
| 视觉模型   | VL 模型，通过 OpenAI 兼容接口接入                      |
| 自动化     | mss（截图）, pyautogui（输入）                         |
| Agent 框架 | LangGraph                                              |
| 搜索       | Tavily Search API（用户自带 API Key）                  |
| 定时调度   | APScheduler + SQLite                                  |
| 远程 IM    | 飞书长连接、Telegram Bot 长轮询、微信 ClawBot 扫码绑定 |

### Main/Sub-Agent Runtime

所有用户消息都会先由 MainAgent 处理。MainAgent 负责面对用户、维护有边界的最近对话窗口，理解“继续”“你搜搜看”“刚才那个”这类承接表达；简单请求直接回答，只有任务需要专门执行、桌面操作或异步处理时才委派 worker。worker 使用内部 scoped conversation id 执行任务，前台会话只保留摘要、证据和最终结果，减少上下文污染。

MainAgent 的内部决策步骤采用原则驱动，而不是关键词驱动：根据任务所需能力选择 worker，把非即时的时间意图视为持久化任务，只在缺失信息会导致不可撤销且完全错误的结果时才追问。直接回答写入独立的 `answer` 字段；面向用户的调度说明使用第一人称口语，让 MainAgent 更像协作助理，而不是系统日志。

在委派工作开始前，MainAgent 可以通过 `server:agent_progress` 先发一句自然的进展说明，比如“我先搜一下”。worker 执行、工具调用和对用户有意义的记忆操作都会通过 `server:trace` 事件回到客户端，前端可以像展示工具调用一样展示这些动作。MainAgent 自己的内部决策 JSON 不进入 Run calls UI。

当用户要求稍后或周期性执行某件事时，MainAgent 会创建持久化定时任务，而不是立即把任务做掉。定时任务存储在 `.open-eagle/scheduler.db`，使用 cron 表达式调度，执行时仍走同一套 worker 类型，并可将执行结果回传到原会话。

### 记忆与上下文整理

openEagle 现在内置 Hermes 风格的单用户长期记忆，数据保存在 `.open-eagle/memory.db`。记忆分为用户画像、用户笔记、Soul 和原始记忆事件：原始事件尽量保留回合与压缩快照，prompt 注入走 V2 检索层，只放有界用户画像摘要、压缩后的 Soul 摘要、Agent 旁注，以及与当前请求相关的活跃用户笔记。完整记忆仍可通过系统默认工具（`get_memory_state`、`save_memory_note`、`update_memory_note`、`delete_memory_note`、`save_user_profile`、`save_soul_core`、`save_agent_side_notes`）按需读取和维护，所以“记住/记录一下”类请求会写入 memory 数据库，而不是在项目根目录创建文件。设置页的 Memory 区域只展示活跃用户笔记，删除笔记会归档并从列表隐藏，同时保留审计记录。

会话轮次会按 conversation ID 持久化到 `.open-eagle/memory.db`，因此桌面客户端或远程 IM 在断联、重连和重启后都能继续原上下文。默认完整保留最近 30 轮，可通过 `context.conversationTurnLimit` 调整；更早轮次会合并为归档摘要。达到 token 阈值后，后端会保留 system 和最近 N 条消息，只处理中间消息；工具消息先占位或移除，避免把大段工具输出交给摘要模型。远程 IM 会话会在达到静默时长后后台整理旧上下文，不再把下一轮强制视为新窗口。

内置 worker 类型：

| Worker     | 用途                                   |
| ---------- | -------------------------------------- |
| `general`  | 通用解释、轻量工具、只读检查           |
| `coding`   | 代码修改、文档、构建、测试、写文件任务 |
| `research` | 搜索、查询、信息整理                   |
| `solo`     | 视觉桌面执行的内部 worker id           |

只读 worker 可以保守并发；代码/写入任务与桌面执行串行执行，避免文件和桌面操作互相打架。

### 桌面执行 — 工作原理

桌面执行 worker 是一个**目标驱动的 Agent**，而非按固定脚本执行的任务器：

1. **观察** — 截取当前桌面屏幕
2. **思考** — VL 模型分析截图 + 任务目标 + 历史记录
3. **行动** — 执行一个操作（点击、输入、滚动等）
4. **循环** — 重复直到任务完成或手动停止

模型在每一步自主决策。没有预编程脚本，没有脆弱的元素选择器。纯视觉理解 + 推理。

桌面执行 worker 的稳定性策略保持温和：用 action signature 判断真正重复的动作，用 no-op / uncertain / failed 区分执行结果，只在恢复模式下降级 batch。`open_url`、点击、双击、`press_keys` 这类可能触发页面加载的动作会做短暂的动作后截图稳定采样，避免下一轮模型看到过早的 loading 帧。这样可以减少重复点击和卡屏，同时不把通用任务框成固定脚本。

### 远程 IM 控制

在 **Settings -> IM** 中启用飞书、Telegram 或微信后，openEagle 可以从远程会话接收任务。

- 飞书需要填写应用的 `App ID` 和 `App Secret`。白名单支持单个用户的 `open_id`，也支持私聊或群聊的 `chat_id`。如果不知道该填什么，可以先给机器人发一条消息，再从 IM 状态面板里的“最近拦截 open_id / chat_id”复制。
- Telegram 需要填写 Bot Token。白名单支持 `user_id` 或 `chat_id`。
- 微信使用 `wechat-clawbot`。在微信卡片中点击“扫码绑定”，用微信扫码后会自动保存 `accountId`，再启用微信入口即可开始长轮询。点击“解绑”会停止轮询，并清理 openEagle 专用的本地 ClawBot 账号凭据。
- 白名单为空时默认拦截所有远程消息。
- 远程普通文本会先交给 main agent。main agent 可以自然回复、调用工具，或在任务需要 GUI 操作时调度桌面执行。
- 远程会话静默达到 `context.imIdleCleanupMinutes` 后，会在后台摘要较早上下文，同时保留最近完整轮次。
- 只有明确希望优先走桌面执行时，才使用 `/solo <任务>`。

远程命令：

| 命令                         | 行为                 |
| ---------------------------- | -------------------- |
| `<普通文本>`                 | 发送给 main agent    |
| `/solo <任务>`               | 显式请求桌面执行     |
| `/pause`、`/resume`、`/stop` | 控制当前桌面执行任务 |
| `/allow`、`/reject`          | 确认或拒绝待确认动作 |
| `/help`                      | 查看命令帮助         |

### 安全模型

每个操作都经过三级风险评估：

| 级别      | 行为                                          |
| --------- | --------------------------------------------- |
| `safe`    | 立即执行（低风险桌面动作、只读工具等）        |
| `confirm` | 等待你确认（写文件、删除/移动、系统快捷键等） |
| `blocked` | 直接拒绝（危险命令如 `rm -rf`）               |

额外护栏：最多 150 步、动作签名重复检测、截图无变化检测、恢复模式下抑制 batch；动作参数格式错误或执行失败会优先反馈给当前 agent 自修复，而不是直接抛给用户。

## 配置

openEagle 支持灵活的模型路由——main agent 文本对话和 Vision-Language 桌面执行可使用不同的提供商和模型，支持任何 OpenAI 兼容 API。

关键设置（通过应用内设置面板访问）：

| 设置项                                                | 说明                                              |
| ----------------------------------------------------- | ------------------------------------------------- |
| `agent.provider`                                      | 文本模型提供商（`openai`、`openai-like`、`mock`） |
| `agent.modelId`                                       | main agent 与直接对话用的文本模型                 |
| `agent.baseUrl`                                       | OpenAI 兼容 API 的自定义地址                      |
| `agent.vlProvider`                                    | 桌面执行视觉模型提供商（`openai`、`openai-like`） |
| `agent.vlModelId`                                     | 桌面执行用的视觉语言模型                          |
| `agent.vlBaseUrl`                                     | 视觉模型的 OpenAI 兼容 API 地址                   |
| `webSearch.provider`                                  | 内置联网搜索提供商（`tavily` 或 `disabled`）      |
| `webSearch.apiKey`                                    | Tavily API Key，也可使用 `TAVILY_API_KEY` 环境变量 |
| `webSearch.searchDepth` / `webSearch.maxResults`      | 搜索深度与默认结果数量                            |
| `feishu.enabled`                                      | 启用飞书远程入口                                  |
| `feishu.appId` / `feishu.appSecret`                   | 飞书长连接应用凭据                                |
| `feishu.allowedOpenIds` / `feishu.allowedChatIds`     | 飞书用户/会话白名单                               |
| `telegram.enabled`                                    | 启用 Telegram 远程入口                            |
| `telegram.botToken`                                   | Telegram Bot API Token                            |
| `telegram.allowedUserIds` / `telegram.allowedChatIds` | Telegram 用户/会话白名单                          |
| `wechat.enabled`                                      | 启用微信 ClawBot 远程入口                         |
| `wechat.accountId`                                    | 微信扫码绑定后保存的 ClawBot 账号                 |
| `wechat.baseUrl` / `wechat.botType`                   | 可选 ClawBot API 地址与 Bot Type                  |
| `wechat.allowedUserIds` / `wechat.allowedChatIds`     | 微信用户/会话白名单                               |
| `context.maxInputTokens`                              | 触发上下文整理的估算输入 token 阈值               |
| `context.conversationTurnLimit`                       | 每个会话持久保留的完整轮数（默认 30）             |
| `context.preserveRecentMessages`                      | 上下文整理时完整保留的最近消息数                  |
| `context.toolMessageMode`                             | 中段工具消息处理方式：占位或移除                  |
| `context.aiSummaryEnabled`                            | 是否使用文本模型摘要中段上下文                    |
| `context.snapshotOnCompaction`                        | 压缩前是否写入长期记忆快照                        |
| `tools`                                               | 自定义命令工具（[详见下方](#tool--自定义命令工具)） |
| `mcp`                                                 | MCP 服务器连接，保存于 `.open-eagle/mcp.json`（[详见下方](#mcp--连接外部服务)） |
| `skills`                                              | 自定义 Skill 行为指令，保存于 `.open-eagle/skills/`（[详见下方](#skill--注入私域经验)） |

模型、联网搜索凭据、IM、上下文和界面偏好仍保存在本机 `.open-eagle/settings.json`。MCP 与 Skill 定义改为文件态存储，方便用户审阅、复制到其他机器，或单独纳入版本管理。Tavily API Key 不会写入 `.open-eagle/mcp.json`；旧版 `settings.json` 中已有的 `mcp` / `skills` 数组会在启动时自动迁移为文件。

## 工具、MCP 与 Skill

openEagle 的能力不只是内置的。你可以通过三种机制扩展 Agent 的行为，从"能做什么"到"怎么做"全面定制。

### Tool — 自定义命令工具

Tool 是最基础的扩展：给 Agent 一个可执行的 shell 命令，它就能在对话或桌面执行中调用。

在 **Settings -> Tools** 中添加，每个 Tool 定义：

| 字段 | 说明 |
|------|------|
| `name` | 工具名称，Agent 会根据名称和描述判断何时调用 |
| `command` | Shell 命令模板，支持 `{placeholder}` 占位符，Agent 会自动填充参数 |
| `description` | 告诉 Agent 这个工具做什么、什么时候该用 |
| `cwd` | 工作目录（可选） |
| `timeout_ms` | 超时时间，默认 30 秒 |

**示例**：定义一个 `run_tests` 工具，命令为 `uv run python -m pytest {test_path} -v`，描述为"运行指定路径的 pytest 测试"。Agent 在需要跑测试时会自动选择它。

### MCP — 连接外部服务

openEagle 支持 [Model Context Protocol (MCP)](https://modelcontextprotocol.io/)，可以接入任何 MCP 服务器提供的工具。这意味着你可以复用整个 MCP 生态的能力，而不需要自己写集成代码。

在 **Settings -> MCP** 中配置，支持三种传输方式：

| 传输方式 | 说明 | 适用场景 |
|----------|------|----------|
| `stdio` | 启动本地进程通信 | 本地 MCP 服务器 |
| `sse` | Server-Sent Events | 远程 MCP 服务器 |
| `http` | Streamable HTTP | 远程 MCP 服务器 |

配置示例（stdio）：`id: "my-mcp"`, `name: "我的 MCP 服务"`, `transport: "stdio"`, `endpoint: "npx -y @some/mcp-server"`

MCP 工具在桌面执行和对话中均可使用。默认权限模式下，MCP 调用需要用户确认。

MCP 定义会持久化到 `.open-eagle/mcp.json`：

```json
{
  "version": 1,
  "servers": [
    {
      "id": "my-mcp",
      "name": "我的 MCP 服务",
      "transport": "stdio",
      "endpoint": "npx -y @some/mcp-server",
      "description": "提供项目专用工具。",
      "enabled": true
    }
  ]
}
```

openEagle 也可以读取常见的 Claude 风格 `mcpServers` JSON，并规范化为应用内的 MCP 列表。

### Skill — 注入私域经验

Skill 是 openEagle 最独特的扩展机制。它不执行代码，而是**向 Agent 注入一段行为指令**——告诉 Agent "遇到这类任务时，按这个经验来做，不要走通用的路子"。

这解决了一个核心问题：**通用模型会走大众路线，但你的工作流有私域经验**。比如：

- 你公司的部署流程不是标准的 `docker compose up`，而是一套自定义脚本
- 你习惯用特定的文件组织方式、命名规范、commit 风格
- 某个操作的正确顺序和网上搜到的不一样

Skill 让你把这些经验"教"给 Agent，它在相关场景下会自动遵循。

在 **Settings -> Skills** 中添加：

| 字段 | 说明 |
|------|------|
| `name` | Skill 名称 |
| `description` | 一句话描述这个 Skill 适用于什么场景 |
| `prompt` | 完整的行为指令——越具体越好 |

**示例**：

```
name: "项目部署"
description: "部署本项目到测试环境时的正确流程"
prompt: |
  部署到测试环境时，不要用 docker compose。
  正确流程：
  1. 运行 scripts/build.sh
  2. 运行 scripts/deploy-test.sh
  3. 检查 health check: curl http://test.internal:8080/health
  4. 如果失败，回滚: scripts/rollback.sh
```

**使用方式**：

- **自动激活**：启用的 Skill 会在桌面执行时自动注入系统指令，Agent 会根据 Skill 描述自动判断是否适用
- **手动指定**：在对话中输入 `/skill <名称>` 显式选择某个 Skill 用于当前轮次

> Skill 的本质是**经验的结构化传递**。你不需要写代码，只需要把"正确做法"写清楚，Agent 就会照做。这比让模型自己猜或者搜索通用方案可靠得多。

Skill 会以可迁移的目录形式保存：

```
.open-eagle/
  skills/
    project-deploy/
      skill.json
      SKILL.md
```

`skill.json` 保存 `id`、`name`、`description`、`enabled` 等元数据；`SKILL.md` 保存完整行为指令，因此其他文件态 agent 的 Skill 可以较少修改地复制进 openEagle。如果 `SKILL.md` 带有包含 `name` 或 `description` 的 front matter，而 `skill.json` 不存在，openEagle 会读取这些信息。用户在设置中删除 Skill 时，目录会归档到 `.open-eagle/deleted-skills/`，不会直接永久删除。

### 三者的关系

```
                ┌─────────────────────────────────┐
                │           Agent 决策             │
                │  "我该怎么完成这个任务？"          │
                └──────────┬──────────────────────┘
                           │
            ┌──────────────┼──────────────────┐
            ▼              ▼                  ▼
     ┌──────────┐   ┌──────────┐      ┌──────────┐
     │   Tool   │   │   MCP    │      │  Skill   │
     │ 执行命令  │   │ 外部能力  │      │ 行为指令  │
     │          │   │          │      │          │
     │ "做什么"  │   │ "用什么"  │      │ "怎么做"  │
     └──────────┘   └──────────┘      └──────────┘
```

- **Tool** 决定 Agent 能调用什么命令
- **MCP** 决定 Agent 能使用什么外部能力
- **Skill** 决定 Agent 在特定场景下怎么做

三者可以组合使用。比如你有一个 MCP 服务器提供了数据库查询能力，一个 Skill 定义了你们团队的查询规范，一个 Tool 封装了常用的分析脚本——Agent 会在合适的场景自动组合它们。

## Roadmap

- [x] 离开主窗口后，执行状态也能更清楚地呈现给用户（结构化悬浮窗 HUD）
- [ ] 更快的响应操作（prompt caching 等）
- [ ] macOS 和 Linux 支持
- [ ] 社区插件系统
- [ ] 会话回放与更完整的历史记录
- [ ] 语音输入
- [x] Hermes 风格长期记忆：用户画像、用户笔记、Soul、原始事件与系统默认记忆工具
- [x] 可配置上下文整理：token 阈值、最近消息保留、工具预清理、AI 中段摘要与 IM 静默窗口
- [x] 定时任务：持久化存储、worker 执行、UI 管理与执行历史
- [x] MainAgent 主导的派工、承接上下文、自然进展消息、worker/工具/记忆可见 trace 与助理式直接回复
- [x] 多显示器感知（已支持 display 选择与运行时切换）
- [x] 模型适配：OpenAI 兼容 API + Anthropic Claude 原生支持

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
   pnpm exec tsc -p tsconfig.electron.json --noEmit  # Electron 主进程检查
   ```
6. **提交**，遵循 [Conventional Commits](https://www.conventionalcommits.org/) 规范：
   ```
   feat: 添加多显示器支持
   fix: 修复超宽屏下悬浮窗定位问题
   ```
7. **Push** 并创建 Pull Request

### 需要帮助的方向

- **桌面执行可靠性** — 在不同应用和工作流中测试
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
