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
            "安全策略：只读命令和只读文件工具可直接使用；写入、删除、未知命令或自定义固定命令"
            "可能触发确认，看到 CONFIRMATION_REQUIRED 时等待用户确认，不要循环重试。"
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


def solo_decision_instructions() -> list[str]:
    return [
        "你是 openEagle SOLO 视觉 Agent 的桌面自动化决策模型。",
        "必须仅输出 JSON，禁止输出任何额外文本。",
        "JSON 字段必须为 thought_summary, action, action_args, expected_outcome, is_task_done。",
        "action 仅可取: finish, wait, screenshot, click, double_click, right_click, move_mouse, scroll, type_text, press_keys, execute_command。",
        (
            "视觉边界：SOLO 负责观察屏幕、点击 GUI、滚动、输入和验证视觉状态；"
            "如果任务能通过命令行完成检查、启动、文件、Git、脚本或系统查询，优先使用 execute_command，"
            "不要用鼠标键盘绕远路。"
        ),
        "只有当当前截图中的 UI 状态本身是任务目标，或没有可靠命令行路径时，才使用 click/type_text/press_keys 等视觉动作。",
        "鼠标坐标可以使用像素值，也可以使用 0~1 的归一化比例坐标；优先选择当前截图中清晰可见的目标。",
        "避免连续重复同一动作；执行会改变界面的动作后，下一步应通过截图或等待来验证结果。",
        "如果任务已完成，action=finish 且 is_task_done=true；不能确认完成时不要 finish。",
        "使用 execute_command 时必须提供 action_args.command，可选提供 cwd、timeout_ms、tail。",
    ]


def build_solo_decision_prompt(
    task: str,
    history: list[dict[str, object]],
) -> str:
    history_text = json.dumps(history[-8:], ensure_ascii=False)
    return (
        f"用户任务: {task}\n\n"
        f"最近步骤历史: {history_text}\n\n"
        "请基于当前截图和历史决定下一步动作。"
        "优先判断是否可用 execute_command 高效完成；只有真正需要视觉交互时才使用鼠标键盘动作。"
        "仅返回 JSON。"
    )
