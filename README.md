# openEagle

openEagle 是一个通过视觉界面帮助你做事的桌面 Agent。

当前仓库已完成基础工程搭建：

- 前端：`Tauri 2 + React + TypeScript`
- 后端：`Python + FastAPI + WebSocket`
- 桌面集成：Tauri Rust 层负责拉起 Python sidecar，并通过 stdout 完成动态端口握手

## 当前能力

- ChatGPT Web 风格的基础双栏布局
- 左侧历史会话与设置入口
- 主聊天面板、输入框、连接状态提示
- Python 后端随机端口启动，并输出 `[AGENT_READY] WS_PORT: <port>`
- Tauri 监听握手日志，将端口传递给前端
- 前端自动连接 `ws://127.0.0.1:<port>/ws`
- mock provider 占位链路已打通，真实模型链路使用 `Agno`
- 支持 `openai` 与 `openai-like` 两类模型配置
- 飞书机器人配置页已预留字段，暂未接入 webhook / 事件处理

## 项目结构

```text
.
|-- src/                    # React 前端
|-- src-tauri/              # Tauri 2 Rust 壳与 sidecar 生命周期管理
|-- backend/                # Python FastAPI/WebSocket 服务
|-- docs/                   # 架构说明文档
```

## 本地开发

### 1. 安装前端依赖

推荐使用 `pnpm`：

```powershell
corepack enable
corepack prepare pnpm@10.7.0 --activate
pnpm install
```

### 2. 使用 uv 准备 Python 环境

```powershell
uv sync --project .\backend
```

### 3. 启动桌面应用

```powershell
pnpm tauri:dev
```

开发模式下，Tauri 会优先通过 `uv run python -m app.main` 启动本地 Python 后端。

## Python 后端

后端提供：

- `GET /health`
- `/ws` WebSocket 对话入口

消息协议统一包含以下字段：

- `type`
- `requestId`
- `conversationId`
- `payload`
- `timestamp`

目前已实现：

- `client:send_message`
- `client:update_settings`
- `server:message`
- `server:status`
- `server:error`

## 打包分发

打包思路参考 `docs/架构设计理念.md`。

Python sidecar 预留了构建脚本：

```powershell
.\backend\scripts\build-sidecar.ps1
```

该脚本会用 `uv` 安装构建依赖，并通过 `PyInstaller` 将后端打包到 `src-tauri/binaries/` 下，供 Tauri 生产构建使用。

## 通信方式

1. 通过主页面对话框输入
2. 通过接入飞书机器人（当前仅预留设置入口）
