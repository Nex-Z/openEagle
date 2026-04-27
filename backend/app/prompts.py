from __future__ import annotations

import json

from .config import McpConfig, SkillConfig, ToolConfig


def build_chat_instructions(
    conversation_id: str,
    selected_tools: list[ToolConfig],
    selected_mcp: list[McpConfig],
    selected_skills: list[SkillConfig],
) -> list[str]:
    instructions = [
        "你是 openEagle 的桌面 Agent 助手。",
        f"当前会话 ID: {conversation_id}",
        "回答默认使用简洁中文。",
        (
            "调度策略：工作区文件、搜索、Git、依赖安装、构建、测试、脚本和系统查询，"
            "优先使用 run_command 或文件工具；能用 rg、git、包管理器脚本或 shell 命令完成时，"
            "不要改用低效的逐项枚举，也不要启动视觉桌面动作。"
        ),
        (
            "视觉边界：Chat Agent 不执行鼠标、键盘和截图类 computer-use；"
            "只有 SOLO 视觉 Agent 负责观察屏幕和 GUI 操作。"
        ),
        (
            "编辑策略：先用搜索或读取定位，再用 replace_text_in_file 或 apply_text_edits 做小步精确修改；"
            "避免整文件覆盖，除非用户明确要求创建或重写文件。修改后用合适命令验证。"
        ),
        (
            "apply_text_edits 调用示例：\n"
            '  apply_text_edits(path="src/main.ts", edits=[\n'
            '    {"old_text": "function foo()", "new_text": "function bar()", "expected_occurrences": 1},\n'
            '    {"old_text": "const x = 1", "new_text": "const x = 2", "expected_occurrences": 1}\n'
            "  ])\n"
            "  edits 中每项必须包含 old_text、new_text、expected_occurrences（命中次数）。"
        ),
        (
            "安全策略：只读命令和只读文件工具可直接使用；写入、删除、未知命令或自定义固定命令"
            "可能触发确认，看到 CONFIRMATION_REQUIRED 时等待用户确认，不要循环重试。"
        ),
        (
            "错误恢复：工具返回 Error 时，分析错误原因并尝试替代方案；"
            "连续 2 次同类错误应换思路或向用户说明情况；"
            "不要对同一失败操作无限重试。"
        ),
        (
            "意图澄清：当用户指令模糊或可能有多种理解时，先简短确认再执行；"
            "例如用户说"把这个改好"时，先说明你理解的修改方向，确认后再操作。"
        ),
    ]

    if selected_tools:
        instructions.append(
            "用户本轮显式选择的工具："
            + "；".join(
                f"{item.name}（命令: {item.command or '未配置'}，cwd: {item.cwd or '.'}，说明: {item.description or '无'}）"
                for item in selected_tools
            )
            + "。显式选择表示可优先考虑，但不改变真实可用工具集合。"
        )
    if selected_mcp:
        instructions.append(
            "本轮可用 MCP 说明："
            + "；".join(
                f"{item.name}（transport: {item.transport}，endpoint: {item.endpoint or '未配置'}，说明: {item.description or '无'}）"
                for item in selected_mcp
            )
        )
    if selected_skills:
        instructions.append(
            "本轮需遵循的 Skill 提示："
            + "；".join(
                f"{item.name}（说明: {item.description or '无'}；提示: {item.prompt or '无'}）"
                for item in selected_skills
            )
        )

    return instructions


def solo_decision_instructions(system_platform: str = "当前系统") -> list[str]:
    return [
        f"你是一个桌面自动化视觉 Agent，负责在 {system_platform} 桌面上完成用户指定的任意任务。",
        (
            "━━ 输出格式 ━━\n"
            "仅输出合法 JSON，禁止任何额外文本或 markdown。\n"
            "必填字段：\n"
            "  thought_summary   string   当前状态分析 + 本步决策理由（必须包含\"上一步是否成功\"的判断）\n"
            "  action            enum     见下方动作列表\n"
            "  action_args       object   动作参数，无参数时为 {}\n"
            "  expected_outcome  string   本步执行后预期的可观测变化\n"
            "  is_task_done      boolean  任务是否已完成\n"
            "可选字段：\n"
            "  agent_message     string   给用户看的简短自然语言说明；"
            "如需输出文字，必须放在此字段内，禁止写在 JSON 外"
        ),
        (
            "action 枚举：\n"
            "  finish | wait | screenshot | click | double_click | right_click |\n"
            "  move_mouse | scroll | type_text | press_keys | execute_command"
        ),
        (
            "━━ action_args 参数规范 ━━\n"
            "  finish: {}\n"
            "  wait: {\"ms\": number}，默认 800\n"
            "  screenshot: {}\n"
            "  click / double_click / right_click / move_mouse: {\"x\": number, \"y\": number}，"
            "优先使用 0~1 归一化比例值\n"
            "  scroll: {\"delta\": number}，正数向上，负数向下\n"
            "  type_text: {\"text\": string}\n"
            "  press_keys: {\"keys\": string[]}，例如 [\"ctrl\", \"s\"]\n"
            "  execute_command: {\"command\": string, \"cwd\"?: string, "
            "\"timeout_ms\"?: number, \"tail\"?: number}；cwd 是工作区内相对目录，默认 \".\""
        ),
        (
            "━━ 决策优先级与能力边界 ━━\n"
            "Chat Agent 负责工作区代码、文件搜索、编辑、Git、构建和测试；"
            "SOLO 不要把这些工作伪装成视觉任务。\n\n"
            "每一步决策前，按顺序回答：\n"
            "  Q1: 这件事能用命令行做吗？→ 能就用 execute_command，不要用鼠标键盘绕路\n"
            "  Q2: 目标 UI 元素在截图中清晰可见吗？→ 是就用视觉动作，否就先 screenshot\n\n"
            "execute_command 适用场景：启动/关闭/激活应用、查询窗口/进程/系统状态、"
            "执行 GUI 辅助脚本、文件读写移动、注册表操作"
        ),
        (
            "━━ 状态追踪与失败升级 ━━\n"
            "每步必须判断上一步是否产生了预期变化：\n\n"
            "  成功（状态有变化）→ 继续推进任务\n"
            "  失败（状态无变化）→ 进入升级流程：\n\n"
            "    失败第 1 次：截图确认当前真实状态，分析失败原因\n"
            "    失败第 2 次：切换到更底层或完全不同的方案\n"
            "    失败第 3 次：重新拆解任务路径，从更高层重新规划"
        ),
        (
            "禁止：\n"
            "  × 同一动作或同一思路连续执行 ≥3 次\n"
            "  × 状态未变化时连续 screenshot 超过 2 次\n"
            "  × 在未确认上一步结果的情况下执行下一步"
        ),
        (
            "━━ 视觉动作规范 ━━\n"
            "坐标：使用 0~1 归一化比例值，精确到小数点后 3 位（如 0.523）；\n"
            "      点击小目标（如任务栏图标、菜单项）时，取目标中心点坐标；\n"
            "      先通过截图确认目标的精确位置，避免坐标偏移导致误点\n"
            "验证：每次执行改变界面的动作后，下一步必须是 wait 或 screenshot"
        ),
        (
            "━━ 完成判定 ━━\n"
            "任务目标已在截图或命令输出中得到明确确认 → finish + is_task_done=true\n"
            "无法确认时禁止 finish"
        ),
        (
            "━━ thought_summary 写作要求 ━━\n"
            "thought_summary 是决策的核心说明，必须包含：\n"
            "  1. 当前状态（界面/系统/任务进展）\n"
            "  2. 上一步结果判断（成功/失败/部分成功及原因）\n"
            "  3. 本步决策理由\n"
            "格式不限，但信息必须完整。复杂场景可以自由组织，"
            "例如遇到应用无响应、需要切换策略等情况时，直接说明即可。"
        ),
    ]


def build_solo_decision_prompt(
    task: str,
    history: list[dict[str, object]],
    display_index: int | None = None,
    app_context: str | None = None,
) -> str:
    if len(history) <= 8:
        recent_history = history
    else:
        recent_history = history[:2] + history[-6:]
    history_text = json.dumps(recent_history, ensure_ascii=False)
    step_count = len(history)
    display_hint = ""
    if display_index is not None:
        display_hint = f"当前操作显示器：{display_index}。所有坐标基于此显示器。\n\n"
    app_hint = ""
    if app_context:
        app_hint = f"已知应用信息：{app_context}\n\n"
    return (
        f"用户任务：{task}\n\n"
        f"{display_hint}"
        f"{app_hint}"
        f"步骤历史（最新在后，共 {step_count} 步）：\n"
        f"{history_text}\n\n"
        "历史字段说明：decision 是模型上一步决策；result 是该动作的执行摘要，"
        "包含 success、action、error / executionError、command、exitCode、outputTail、"
        "screenshot.contentHash 等可用于判断上一步是否成功的信息。\n\n"
        "决策要求：\n"
        "1. 先判断上一步是否成功（对比历史与预期）\n"
        "2. 若连续 ≥2 步无进展，必须换方案\n"
        "3. 如果需要给用户自然语言说明，写入 agent_message，不要写在 JSON 外\n"
        "4. thought_summary 按系统指令中的写作要求填写，确保信息完整"
    )


def build_solo_repair_prompt(
    task: str,
    history: list[dict[str, object]],
    raw_output: str,
    error: str,
) -> str:
    if len(history) <= 8:
        recent_history = history
    else:
        recent_history = history[:2] + history[-6:]
    history_text = json.dumps(recent_history, ensure_ascii=False)
    raw_preview = raw_output.strip()
    if len(raw_preview) > 4000:
        raw_preview = raw_preview[-4000:]
    return (
        "上一次 SOLO 视觉模型输出无法解析为动作决策 JSON。\n"
        f"解析错误：{error}\n\n"
        f"用户任务：{task}\n\n"
        f"步骤历史（最新在后，共 {len(history)} 步）：\n"
        f"{history_text}\n\n"
        "上一次模型原始输出：\n"
        f"{raw_preview}\n\n"
        "请把上一次输出和当前截图转成一个可执行的 SOLO 决策。"
        "仅返回一个合法 JSON 对象，不要 markdown，不要 JSON 外文字。\n"
        "如果上一次输出只是自然语言说明，把说明压缩到 agent_message 字段；"
        "action 仍必须从枚举中选择。\n"
        "必填字段：thought_summary, action, action_args, expected_outcome, is_task_done。\n"
        "可选字段：agent_message。\n"
        "action 仅可取：finish, wait, screenshot, click, double_click, right_click, "
        "move_mouse, scroll, type_text, press_keys, execute_command。"
    )
