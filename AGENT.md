# AGENT.md — openEagle 项目约定

## 项目概述

openEagle 是一个桌面端 AI 助手，采用 Tauri 2 + React 前端 + Python FastAPI 后端的三层架构。核心能力有两个：

- **Chat 模式**：传统 LLM 对话，支持工具调用（文件操作、命令执行、代码搜索）
- **SOLO 模式**：视觉桌面自动化，通过 VL（Vision-Language）模型分析截图并操作桌面

## 架构要点

### 进程与通信

```
Tauri (Rust) → 启动 Python sidecar（随机端口）
Python → stdout 输出 [AGENT_READY] WS_PORT: <port>
Rust → 解析端口，通知前端
前端 → WebSocket 连接 ws://127.0.0.1:<port>/ws
```

所有 WebSocket 消息使用 **Envelope** 模式，包含 `type`、`requestId`、`conversationId`、`payload`、`timestamp` 五个字段。前端类型定义在 `src/types/protocol.ts`，后端 Pydantic 模型在 `backend/app/models.py`。

### SOLO 执行流程（Agent Loop 架构）

```
用户输入任务 → client:start_solo
  → 截屏 → agent_loop() 观察→思考→行动循环：
      VL 分析截图 + 任务目标 + 已有发现 → 决策
      → 决策动作 → 安全检查 → 执行 → 截屏 → 下一轮循环
      → VL 判断 is_task_done → 汇总 findings → 生成最终汇报 → finish
```

**核心设计原则**：SOLO 是一个 goal-driven agent，不是 plan executor。VL 模型在每一步都自主推理：观察屏幕、提取信息、判断进度、决定下一步。没有预定义计划，agent 完全自主决策。

关键状态追踪在 `SoloSessionState`（`backend/app/solo_service.py`），包括步数、截图哈希、历史记录、findings（信息积累）等。

核心循环实现在 `agent_loop()`（`backend/app/main.py`）。

### SOLO 鲁棒性机制

**Agent Loop 自主决策**：VL 模型在每一步都自主推理（观察→思考→行动），而非机械执行预定义计划。模型输出 `thought_summary`（分析）、`progress`（进度）、`findings`（信息提取）、`is_task_done`（完成判断）。

**信息积累**：agent 在每一步可提取屏幕信息（新闻标题、搜索结果、价格等）存入 `findings`。任务完成时，findings 汇总到最终汇报中呈现给用户。

**屏幕偏离检测**：VL 决策 prompt 包含「屏幕状态校验」指令，要求模型在执行前检查截图是否有弹窗/对话框/通知等意外覆盖层。偏离时优先处理偏离。

**`is_task_done` 信号**：VL 模型在每次决策中输出 `is_task_done` 布尔值。agent_loop 在每步检查此信号，若为 true 则立即完成并生成最终汇报。

**安全三层评估**：每个动作经过 `assess_solo_action` 评估：safe 直接执行、confirm 等待用户确认、blocked 拒绝执行。

**安全护栏**：
- 最大步数限制（150 步）
- 连续重复动作检测（≥4 次暂停）
- 连续截图无变化检测（≥3 次暂停）

**异常不静默完成**：VL 调用异常时，暂停会话并报告错误，不再静默标记完成。

**完成汇报**：`_build_final_report` 汇总 agent_message + findings + 步数统计，生成结构化的最终汇报。

### 安全模型

三层风险评估：`safe` / `confirm` / `blocked`，覆盖 SOLO 动作、工具调用、命令执行。详见 `backend/app/safety.py`。

核心原则：
- 只读操作默认 safe
- 写操作/系统快捷键需要 confirm
- 危险命令（rm -rf、format 等）直接 blocked
- 所有操作限制在工作区内（workspace boundary enforcement）

## 编码约定

### 语言与命名

- UI 文本、注释、prompt、文档使用**中文**
- 代码标识符使用**英文**
- TypeScript：camelCase（含 JSON 字段，通过 `alias` 映射）
- Python：snake_case（Pydantic 用 `alias` 映射 camelCase 的 wire format）
- 类型/类名：PascalCase

### TypeScript / 前端

- 严格模式（`strict: true`），不允许 unused locals/params
- 函数式组件 + Hooks，无 class 组件
- 状态管理：无 Redux/Zustand，状态集中在 `App.tsx` + `useBackendConnection` hook，通过 props 传递
- 样式：单一全局 `styles.css`，CSS 自定义属性做主题切换（`data-theme`），不用 CSS modules
- 图标库：`lucide-react`
- Markdown 渲染：`react-markdown` + `remark-gfm`

### Python / 后端

- 所有 `.py` 文件头部加 `from __future__ import annotations`
- Pydantic v2，配置 `model_config = {"populate_by_name": True}`
- 包管理：`uv`（不要用 pip）
- 测试：`unittest`（位于 `backend/tests/`）

### 文件编码

- 所有文件必须 **UTF-8 无 BOM**
- Windows 环境写文件前确认编码正确，禁止使用 GBK/ANSI

## WebSocket 协议扩展规范

新增消息类型时需要同步修改三个位置：

1. **`src/types/protocol.ts`** — 添加 TypeScript 类型
2. **`backend/app/models.py`** — 添加 Pydantic payload 模型
3. **`backend/app/main.py`** — 添加消息处理/发送逻辑

消息 type 命名规范：
- 客户端 → 服务端：`client:<action_name>`
- 服务端 → 客户端：`server:<event_name>`

## SOLO Agent 扩展规范

### 新增动作

1. 在 `backend/app/solo_executor.py` 添加执行逻辑（`execute_action`）
2. 在 `backend/app/safety.py` 的 `assess_solo_action` 中添加风险评估
3. 在 `backend/app/prompts.py` 的 action 枚举描述中添加说明

### Prompt 修改原则

- 所有 prompt 在 `backend/app/prompts.py` 中集中管理
- SOLO decision prompt 要求模型输出结构化 JSON，字段定义在 prompt 中说明
- 修改 prompt 后需要验证 JSON 解析兼容性（`SoloService._normalize_decision`）
- `build_solo_decision_prompt` 包含屏幕状态校验指令和 findings 参数
- `solo_decision_instructions` 定义 agent 的身份、输出格式、决策优先级和完成判定标准

## 工具扩展规范

新增 Chat 工具需要：
1. 在 `backend/app/default_tools.py` 的 Toolkit 类中添加方法
2. 方法的 docstring 就是工具描述，模型会直接读取
3. 在 `backend/app/safety.py` 中添加对应的风险评估规则
4. 工具名使用 snake_case，与方法名一致

## 前端组件结构

```
components/
  chat/           # 对话工作区（消息列表 + 输入框）
  inspector/      # 右侧执行面板（SOLO 状态、traces、assets）
  layout/         # 布局壳（AppShell、NavigationSidebar）
  settings/       # 设置抽屉
```

组件通过 props 接收数据，不直接读取全局状态。新增组件放在对应子目录下。

## 构建与验证

```powershell
# 前端类型检查
pnpm -s tsc --noEmit

# 前端构建
pnpm -s build

# 后端语法检查
uv run python -m compileall backend/app

# Rust 检查（修改 src-tauri 时）
cd src-tauri && cargo check
```

提交前至少完成前端 tsc + 后端 compileall。

## 提交规范

```
feat: <中文摘要>
fix: <中文摘要>
refactor: <中文摘要>
```
