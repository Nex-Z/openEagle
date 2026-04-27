from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

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
}
DANGEROUS_KEYS = {"ctrl", "alt", "meta", "win", "f4", "delete", "backspace", "enter"}


@dataclass(frozen=True)
class RiskAssessment:
    level: RiskLevel
    reason: str


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


def assess_solo_action(action: str, action_args: dict[str, Any], workspace_root: Path) -> RiskAssessment:
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
        command = str(action_args.get("command", "")).strip()
        if not command:
            return RiskAssessment("blocked", "命令为空或未提供。")
        cwd = str(action_args.get("cwd", "."))
        try:
            target = resolve_workspace_path(workspace_root, cwd)
        except BlockedActionError as exc:
            return RiskAssessment("blocked", str(exc))
        if not target.exists() or not target.is_dir():
            return RiskAssessment("blocked", "cwd 无效，必须是工作区内目录。")
        return RiskAssessment("confirm", "将执行系统命令。")

    return RiskAssessment("blocked", f"不支持的动作: {action}")


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

    if name == "run_command":
        command = str(params.get("command", "")).strip()
        if not command:
            return RiskAssessment("blocked", "命令为空或未提供。")
        cwd = str(params.get("cwd", "."))
        try:
            target = resolve_workspace_path(workspace_root, cwd)
        except BlockedActionError as exc:
            return RiskAssessment("blocked", str(exc))
        if not target.exists() or not target.is_dir():
            return RiskAssessment("blocked", "cwd 无效，必须是工作区内目录。")
        return RiskAssessment("confirm", "将执行工作区命令。")

    return RiskAssessment("safe", "只读或低风险工具。")
