from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from .solo_actions import SAFE_DEFAULT_TOOL_SOLO_ACTIONS

RiskLevel = Literal["safe", "confirm", "blocked"]

SAFE_SOLO_ACTIONS = {
    "finish",
    "wait",
    "screenshot",
    "click",
    "double_click",
    "right_click",
    "move_mouse",
    "scroll",
    "type_text",
    "open_url",
} | SAFE_DEFAULT_TOOL_SOLO_ACTIONS
DANGEROUS_KEYS = {"ctrl", "alt", "meta", "win", "f4", "delete", "backspace", "enter"}
SAFE_COMMAND_PATTERNS = [
    r"^rg(\.exe)?(\s|$)",
    r"^git\s+(status|diff|log|show|branch|rev-parse|ls-files|grep)(\s|$)",
    r"^(dir|ls|pwd)(\s|$)",
    r"^(where|where\.exe)(\s|$)",
    r"^(get-childitem|gci|get-content|gc|select-string)(\s|$)",
    r"^wmic\s+logicaldisk\s+get\s+",
]
BLOCKED_COMMAND_PATTERNS = [
    r"\brm\s+-rf\b",
    r"\bremove-item\b",
    r"\bdel\s+",
    r"\berase\s+",
    r"\brmdir\b",
    r"\brd\s+/s\b",
    r"(?<![/\w.-])format(?:\.com|\.exe)?(?=\s|$)",
    r"\bshutdown\b",
    r"\brestart-computer\b",
    r"\bstop-computer\b",
    r"\bdiskpart\b",
    r"\breg\s+delete\b",
    r"\btakeown\b",
    r"\bicacls\b",
    r"\bgit\s+reset\s+--hard\b",
    r"\bgit\s+clean\b",
    r"\bgit\s+checkout\s+--\b",
]
COMMAND_WRITE_OPERATORS = (">", ">>", "2>", "*>")
REPAIRABLE_SOLO_BLOCK_REASONS = (
    "press_keys 缺少有效按键列表",
    "press_keys 缺少有效按键",
    "open_url 只允许",
    "命令为空或未提供",
    "cwd 无效",
    "不支持的动作",
)
HARD_SOLO_BLOCK_REASONS = (
    "明确高危操作",
    "路径超出工作区范围",
)


@dataclass(frozen=True)
class RiskAssessment:
    level: RiskLevel
    reason: str
    overridable: bool = False


class BlockedActionError(ValueError):
    pass


def normalize_key(token: str) -> str:
    mapping = {
        "control": "ctrl",
        "cmd": "win",
        "command": "win",
        "meta": "win",
        "return": "enter",
        "escape": "esc",
    }
    lowered = token.strip().lower()
    return mapping.get(lowered, lowered)


def resolve_workspace_path(workspace_root: Path, path: str = ".") -> Path:
    target = (workspace_root / path).resolve()
    try:
        target.relative_to(workspace_root.resolve())
    except ValueError as exc:
        raise BlockedActionError("路径超出工作区范围，不允许访问。") from exc
    return target


def classify_command_risk(command: str) -> RiskAssessment:
    normalized = " ".join(command.strip().split())
    lowered = normalized.lower()
    if not normalized:
        return RiskAssessment("blocked", "命令为空或未提供。")
    if "\x00" in normalized:
        return RiskAssessment("blocked", "命令包含非法字符。")

    for pattern in BLOCKED_COMMAND_PATTERNS:
        if re.search(pattern, lowered):
            return RiskAssessment("blocked", "命令包含明确高危操作，已阻断。", overridable=True)

    if any(operator in lowered for operator in COMMAND_WRITE_OPERATORS):
        return RiskAssessment("confirm", "命令包含输出重定向，可能写入文件。")
    if any(separator in lowered for separator in ("&&", "||", ";", "|")):
        return RiskAssessment("confirm", "命令包含组合或管道操作，需要确认。")

    for pattern in SAFE_COMMAND_PATTERNS:
        if re.search(pattern, lowered):
            return RiskAssessment("safe", "只读命令，可自动执行。")

    return RiskAssessment("confirm", "未知或可能修改环境的命令，需要确认。")


def assess_command_action(
    name: str,
    params: dict[str, Any],
    workspace_root: Path,
) -> RiskAssessment:
    command = str(params.get("command", "")).strip()
    command_risk = classify_command_risk(command)
    if command_risk.level == "blocked":
        return command_risk

    cwd = str(params.get("cwd", "."))
    try:
        target = resolve_workspace_path(workspace_root, cwd)
    except BlockedActionError as exc:
        return RiskAssessment("blocked", str(exc))
    if not target.exists() or not target.is_dir():
        return RiskAssessment("blocked", "cwd 无效，必须是工作区内目录。")

    if name == "configured_tool" and command_risk.level == "confirm":
        return RiskAssessment("confirm", "自定义固定命令需要确认。")
    if name == "configured_tool" and command_risk.level == "safe":
        return RiskAssessment("safe", "只读自定义固定命令，可自动执行。")
    if name == "execute_command" and command_risk.level == "safe":
        return RiskAssessment("safe", "只读系统命令，可自动执行。")
    if name == "execute_command":
        return RiskAssessment("confirm", command_risk.reason)
    return command_risk


def assess_solo_action(action: str, action_args: dict[str, Any], workspace_root: Path) -> RiskAssessment:
    if action == "open_url":
        url = str(action_args.get("url", "")).strip()
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return RiskAssessment("blocked", "open_url 只允许 http/https URL。")
        return RiskAssessment("safe", "打开网页 URL，可自动执行。")

    if action in SAFE_SOLO_ACTIONS:
        return RiskAssessment("safe", "常规桌面动作，可自动执行。")

    if action == "press_keys":
        keys = action_args.get("keys")
        if not isinstance(keys, list) or not keys:
            return RiskAssessment("blocked", "press_keys 缺少有效按键列表。")
        normalized = {normalize_key(str(item)) for item in keys if str(item).strip()}
        if not normalized:
            return RiskAssessment("blocked", "press_keys 缺少有效按键。")
        if normalized & DANGEROUS_KEYS:
            return RiskAssessment("confirm", "包含系统级或可能提交/删除的快捷键。")
        return RiskAssessment("safe", "普通按键动作，可自动执行。")

    if action == "execute_command":
        return assess_command_action("execute_command", action_args, workspace_root)

    return RiskAssessment("blocked", f"不支持的动作: {action}")


def is_repairable_solo_block(action: str, reason: str) -> bool:
    if any(marker in reason for marker in HARD_SOLO_BLOCK_REASONS):
        return False
    if any(marker in reason for marker in REPAIRABLE_SOLO_BLOCK_REASONS):
        return True
    return action in {"press_keys", "open_url"} and "缺少" in reason


def assess_tool_action(name: str, params: dict[str, Any], workspace_root: Path) -> RiskAssessment:
    if name == "write_text_file":
        path = str(params.get("path", "")).strip()
        if not path:
            return RiskAssessment("blocked", "写文件缺少 path。")
        try:
            resolve_workspace_path(workspace_root, path)
        except BlockedActionError as exc:
            return RiskAssessment("blocked", str(exc))
        return RiskAssessment("confirm", "将写入工作区文件。")

    if name == "create_directory":
        path = str(params.get("path", "")).strip()
        if not path:
            return RiskAssessment("blocked", "创建目录缺少 path。")
        try:
            target = resolve_workspace_path(workspace_root, path)
        except BlockedActionError as exc:
            return RiskAssessment("blocked", str(exc))
        if target == workspace_root.resolve():
            return RiskAssessment("blocked", "不能把工作区根目录作为创建目标。")
        return RiskAssessment("confirm", "将在工作区内创建目录。")

    if name in {"copy_path", "move_path"}:
        source_path = str(params.get("source", "")).strip()
        destination_path = str(params.get("destination", "")).strip()
        if not source_path or not destination_path:
            return RiskAssessment("blocked", "复制或移动缺少 source 或 destination。")
        try:
            source = resolve_workspace_path(workspace_root, source_path)
            destination = resolve_workspace_path(workspace_root, destination_path)
        except BlockedActionError as exc:
            return RiskAssessment("blocked", str(exc))
        root = workspace_root.resolve()
        if source == root:
            return RiskAssessment("blocked", "不能复制或移动整个工作区根目录。")
        if not source.exists():
            return RiskAssessment("blocked", "source 不存在。")
        if source == destination:
            return RiskAssessment("blocked", "source 和 destination 不能相同。")
        if destination == root:
            return RiskAssessment("blocked", "destination 不能是工作区根目录。")
        try:
            destination.relative_to(source)
        except ValueError:
            pass
        else:
            return RiskAssessment("blocked", "destination 不能位于 source 内部。")
        reason = "将在工作区内复制路径。" if name == "copy_path" else "将在工作区内移动路径。"
        return RiskAssessment("confirm", reason)

    if name == "delete_path":
        path = str(params.get("path", "")).strip()
        if not path:
            return RiskAssessment("blocked", "删除路径缺少 path。")
        try:
            target = resolve_workspace_path(workspace_root, path)
        except BlockedActionError as exc:
            return RiskAssessment("blocked", str(exc))
        if target == workspace_root.resolve():
            return RiskAssessment("blocked", "不能删除工作区根目录。")
        if not target.exists():
            return RiskAssessment("blocked", "目标路径不存在。")
        return RiskAssessment("confirm", "将在工作区内删除路径。")

    if name == "replace_text_in_file":
        path = str(params.get("path", "")).strip()
        old_text = str(params.get("old_text", ""))
        try:
            expected_occurrences = int(params.get("expected_occurrences", 1))
        except (TypeError, ValueError):
            return RiskAssessment("blocked", "expected_occurrences 必须是整数。")
        if not path:
            return RiskAssessment("blocked", "替换文本缺少 path。")
        if not old_text:
            return RiskAssessment("blocked", "替换文本缺少 old_text。")
        if expected_occurrences < 1:
            return RiskAssessment("blocked", "expected_occurrences 必须 >= 1。")
        try:
            target = resolve_workspace_path(workspace_root, path)
        except BlockedActionError as exc:
            return RiskAssessment("blocked", str(exc))
        if not target.exists() or not target.is_file():
            return RiskAssessment("blocked", "目标文件不存在或不是文件。")
        return RiskAssessment("confirm", "将在工作区内替换文本。")

    if name == "apply_text_edits":
        path = str(params.get("path", "")).strip()
        edits = params.get("edits")
        if not path:
            return RiskAssessment("blocked", "多段编辑缺少 path。")
        if not isinstance(edits, list) or not edits:
            return RiskAssessment("blocked", "edits 必须是非空列表。")
        for index, edit in enumerate(edits, start=1):
            if not isinstance(edit, dict):
                return RiskAssessment("blocked", f"第 {index} 个 edit 必须是对象。")
            if not str(edit.get("old_text", "")):
                return RiskAssessment("blocked", f"第 {index} 个 edit 缺少 old_text。")
            try:
                expected_occurrences = int(edit.get("expected_occurrences", 1))
            except (TypeError, ValueError):
                return RiskAssessment("blocked", f"第 {index} 个 edit 的 expected_occurrences 必须是整数。")
            if expected_occurrences < 1:
                return RiskAssessment("blocked", f"第 {index} 个 edit 的 expected_occurrences 必须 >= 1。")
        try:
            target = resolve_workspace_path(workspace_root, path)
        except BlockedActionError as exc:
            return RiskAssessment("blocked", str(exc))
        if not target.exists() or not target.is_file():
            return RiskAssessment("blocked", "目标文件不存在或不是文件。")
        return RiskAssessment("confirm", "将在工作区内应用多段文本编辑。")

    if name in {"run_command", "configured_tool"}:
        return assess_command_action(name, params, workspace_root)

    return RiskAssessment("safe", "只读或低风险工具。")
