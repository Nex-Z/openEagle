# openEagle

openEagle 是一个桌面 Agent 应用，支持普通对话与 SOLO 视觉自动化任务。

技术栈：
- 前端：`Tauri 2 + React + TypeScript`
- 后端：`Python + FastAPI + WebSocket`
- 桌面壳：Rust 负责 sidecar 生命周期、本地能力桥接（截图、输入、文件等）

## 核心功能

- **Chat / SOLO 双模式对话**：Chat 模式支持工具调用（文件操作、命令执行、代码搜索）；SOLO 模式通过 VL 模型分析截图自动操作桌面
- **SOLO 规划执行**：VL 模型先制定动作计划，再逐步执行；批量动作（打字、按键、命令）直接执行，视觉动作（点击、滚动）经过 VL 定位
- **SOLO 最终评估**：任务结束前 VL 模型会审视最终截图，确认任务完成并给出结果反馈，不满足则自动重新规划
- **安全模型**：三级风险评估（safe / confirm / blocked），覆盖 SOLO 动作、工具调用、命令执行
- **工具系统**：Tool / MCP / Skill 斜杠面板（`/`）与能力注入
- **执行面板**：实时显示 SOLO 状态、计划清单（todo-list）、时间线、确认决策
- **多模型接入**：`openai` / `openai-like` / `mock`

## 目录结构

```text
.
|-- src/                    # React 前端
|-- src-tauri/              # Tauri Rust 壳
|-- backend/                # Python FastAPI / WebSocket 服务
|-- docs/                   # 文档（架构、开发指南）
|-- AGENT.md                # Agent 编码约定与扩展规范
```

## 快速开始

### 1) 安装前端依赖

```powershell
corepack enable
corepack prepare pnpm@10.7.0 --activate
pnpm install
```

### 2) 准备 Python 环境

```powershell
uv sync --project .\backend
```

### 3) 启动桌面应用

```powershell
pnpm tauri:dev
```

说明：开发模式下 Tauri 会拉起 Python 后端并通过握手日志动态获取端口。

## 常用命令

```powershell
pnpm dev
pnpm build
pnpm tauri:dev
pnpm tauri:build
```

后端检查：

```powershell
backend\.venv\Scripts\python.exe -m compileall backend\app
```

## WebSocket 协议（节选）

公共字段：`type`、`requestId`、`conversationId`、`payload`、`timestamp`

常见消息类型：
- 客户端 → 服务端：`client:send_message`、`client:update_settings`、`client:start_solo`、`client:solo_control`、`client:list_solo_displays`、`client:tool_confirmation`
- 服务端 → 客户端：`server:message`、`server:message_delta`、`server:status`、`server:trace`、`server:solo_status`、`server:solo_step`、`server:solo_plan`、`server:solo_confirmation_required`、`server:tool_confirmation_required`、`server:solo_displays`、`server:error`

## 文档

- 架构说明：`docs/架构设计理念.md`
- 开发指南：`docs/开发指南.md`
- Agent 编码约定：`AGENT.md`

## 打包

后端 sidecar 打包脚本：

```powershell
.\backend\scripts\build-sidecar.ps1
```

该脚本通过 `PyInstaller` 构建后端可执行文件并输出到 `src-tauri/binaries/`。
