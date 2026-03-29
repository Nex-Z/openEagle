# openEagle

openEagle 是一个桌面 Agent 应用，支持普通对话与 SOLO 视觉自动化任务。

技术栈：
- 前端：`Tauri 2 + React + TypeScript`
- 后端：`Python + FastAPI + WebSocket`
- 桌面壳：Rust 负责 sidecar 生命周期、本地能力桥接（截图、输入、文件等）

## 核心功能

- Chat / SOLO 双模式对话
- Tool/MCP/Skill 斜杠面板（`/`）与能力注入
- SOLO 实时状态、步骤流、危险动作确认、自动暂停保护
- SOLO 工具调用按单条 item 顺序展示，支持展开查看入参与结果
- SOLO 新消息自动滚动到底
- 设置页显示器选择（带实时截图预览），SOLO 截图按该配置生效
- 多模型接入：`openai` / `openai-like` / `mock`

## 目录结构

```text
.
|-- src/                    # React 前端
|-- src-tauri/              # Tauri Rust 壳
|-- backend/                # Python FastAPI / WebSocket 服务
|-- docs/                   # 文档（架构、开发指南）
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

公共字段：
- `type`
- `requestId`
- `conversationId`
- `payload`
- `timestamp`

常见消息类型：
- `client:send_message`
- `client:update_settings`
- `client:start_solo`
- `client:solo_control`
- `client:list_solo_displays`
- `server:message` / `server:message_delta`
- `server:trace`
- `server:solo_status` / `server:solo_step` / `server:solo_confirmation_required`
- `server:solo_displays`
- `server:error`

## 文档

- 架构说明：`docs/架构设计理念.md`
- 开发指南：`docs/开发指南.md`

## 打包

后端 sidecar 打包脚本：

```powershell
.\backend\scripts\build-sidecar.ps1
```

该脚本通过 `PyInstaller` 构建后端可执行文件并输出到 `src-tauri/binaries/`。
