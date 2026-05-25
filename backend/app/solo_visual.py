from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit, urlunsplit


POINT_ACTIONS = {"click", "double_click", "right_click", "move_mouse"}
KEYBOARD_ACTIONS = {"type_text", "press_keys"}
COMMAND_ACTIONS = {
    "execute_command",
    "run_configured_tool",
    "call_mcp_tool",
    "get_file_info",
    "list_directory",
    "read_text_file",
    "search_files",
    "search_text",
    "web_search",
}
MAX_PUBLIC_TEXT = 120


def build_solo_step_visual(
    action: str,
    action_args: dict[str, Any],
    *,
    display_text: str | None = None,
    screenshot_path: str | None = None,
    capture_region: dict[str, Any] | None = None,
    display_index: int | None = None,
) -> dict[str, Any]:
    normalized_action = action.strip().lower()
    kind = _visual_kind(normalized_action)
    visual: dict[str, Any] = {"kind": kind}

    compact_text = _compact_public_text(display_text)
    if compact_text:
        visual["displayText"] = compact_text

    target_label = _target_label(action_args)
    if target_label:
        visual["targetLabel"] = target_label

    safe_preview = build_safe_args_preview(normalized_action, action_args)
    if safe_preview:
        visual["safeArgsPreview"] = safe_preview

    if screenshot_path:
        visual["screenshotPath"] = screenshot_path

    if kind == "point":
        point = resolve_visual_point(action_args, capture_region)
        if point is not None:
            x, y, screenshot_x, screenshot_y, width, height = point
            visual.update(
                {
                    "x": x,
                    "y": y,
                    "coordinateSpace": "screen",
                    "screenshotX": screenshot_x,
                    "screenshotY": screenshot_y,
                    "screenshotWidth": width,
                    "screenshotHeight": height,
                }
            )
            resolved_display = _int_or_none((capture_region or {}).get("displayIndex")) or display_index
            if resolved_display is not None:
                visual["displayIndex"] = resolved_display

    return visual


def should_delay_for_visual(action: str, action_args: dict[str, Any]) -> bool:
    normalized_action = action.strip().lower()
    return normalized_action in POINT_ACTIONS and action_args.get("x") is not None and action_args.get("y") is not None


def resolve_visual_point(
    action_args: dict[str, Any],
    capture_region: dict[str, Any] | None,
) -> tuple[int, int, int, int, int, int] | None:
    if capture_region is None:
        return None
    raw_x = action_args.get("x")
    raw_y = action_args.get("y")
    if raw_x is None or raw_y is None:
        return None

    width = _positive_int(capture_region.get("width"))
    height = _positive_int(capture_region.get("height"))
    if width is None or height is None:
        return None

    left = _int_or_none(capture_region.get("left")) or 0
    top = _int_or_none(capture_region.get("top")) or 0
    try:
        x = _normalize_point(float(raw_x), width, left)
        y = _normalize_point(float(raw_y), height, top)
    except (TypeError, ValueError):
        return None

    screenshot_x = x - left
    screenshot_y = y - top
    if not (0 <= screenshot_x <= width and 0 <= screenshot_y <= height):
        return None
    return x, y, screenshot_x, screenshot_y, width, height


def build_safe_args_preview(action: str, action_args: dict[str, Any]) -> dict[str, Any]:
    if action in POINT_ACTIONS:
        return _copy_keys(action_args, ("x", "y"))
    if action == "scroll":
        return _copy_keys(action_args, ("delta", "amount"))
    if action == "wait":
        return _copy_keys(action_args, ("ms",))
    if action == "type_text":
        text = str(action_args.get("text") or "")
        return {"text": f"[hidden {len(text)} chars]"} if text else {}
    if action == "press_keys":
        keys = action_args.get("keys")
        return {"keys": [str(item) for item in keys]} if isinstance(keys, list) else {}
    if action == "open_url":
        url = _safe_url_preview(str(action_args.get("url") or ""))
        return {"url": url} if url else {}
    if action == "execute_command":
        command = _redact_public_text(str(action_args.get("command") or ""))
        cwd = _safe_path_preview(action_args.get("cwd"))
        result: dict[str, Any] = {}
        if command:
            result["command"] = _compact_public_text(command)
        if cwd:
            result["cwd"] = cwd
        return result
    if action in {"run_configured_tool", "call_mcp_tool"}:
        result = _copy_keys(action_args, ("tool_id", "toolId", "tool_name", "toolName", "server_id", "serverId"))
        args = action_args.get("arguments")
        if isinstance(args, dict) and args:
            result["arguments"] = _summarize_dict(args)
        return result
    if action in COMMAND_ACTIONS:
        return _summarize_dict(action_args)
    return {}


def _visual_kind(action: str) -> str:
    if action in POINT_ACTIONS:
        return "point"
    if action == "scroll":
        return "scroll"
    if action in KEYBOARD_ACTIONS:
        return "keyboard"
    if action == "open_url":
        return "navigation"
    if action == "wait":
        return "wait"
    if action in COMMAND_ACTIONS:
        return "command"
    return "none"


def _normalize_point(value: float, bound: int, offset: int = 0) -> int:
    if 0 <= value <= 1:
        return int(value * bound) + offset
    raw = int(value)
    if offset != 0 and 0 <= raw <= bound:
        return raw + offset
    return raw


def _target_label(action_args: dict[str, Any]) -> str | None:
    for key in ("targetLabel", "target_label", "target", "element", "label"):
        value = _compact_public_text(action_args.get(key))
        if value:
            return value
    return None


def _copy_keys(action_args: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in keys:
        value = action_args.get(key)
        if value is not None:
            result[key] = _safe_scalar_preview(value)
    return result


def _summarize_dict(value: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in list(value.items())[:8]:
        result[str(key)] = _safe_scalar_preview(item)
    if len(value) > 8:
        result["..."] = f"{len(value) - 8} more"
    return result


def _safe_scalar_preview(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int | float):
        return value
    if isinstance(value, list):
        return [_safe_scalar_preview(item) for item in value[:8]]
    if isinstance(value, dict):
        return _summarize_dict(value)
    text = str(value)
    if _looks_like_path(text):
        return _safe_path_preview(text)
    return _compact_public_text(_redact_public_text(text))


def _compact_public_text(value: Any, max_length: int = MAX_PUBLIC_TEXT) -> str:
    text = str(value or "")
    text = re.sub(r"\s+", " ", text.replace("\r", " ").replace("\n", " ")).strip()
    if len(text) <= max_length:
        return text
    return f"{text[: max_length - 1].rstrip()}..."


def _redact_public_text(text: str) -> str:
    redacted = re.sub(
        r"(?i)\b(api[_-]?key|token|secret|password|authorization)(\s*[:=]\s*)([^\s&]+)",
        r"\1\2[hidden]",
        text,
    )
    redacted = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[hidden]", redacted)
    redacted = re.sub(r"([A-Za-z]:\\[^\s\"']+|/[^\s\"']+)", "[path]", redacted)
    return redacted


def _safe_path_preview(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    normalized = text.replace("\\", "/")
    name = PurePosixPath(normalized).name
    return f".../{name}" if name else "[path]"


def _safe_url_preview(value: str) -> str:
    text = value.strip()
    if not text:
        return ""
    try:
        parsed = urlsplit(text)
    except ValueError:
        return _compact_public_text(_redact_public_text(text))
    if not parsed.scheme or not parsed.netloc:
        return _compact_public_text(_redact_public_text(text))
    return _compact_public_text(urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")))


def _looks_like_path(text: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:[\\/]", text) or text.startswith("/") or "\\" in text)


def _positive_int(value: Any) -> int | None:
    number = _int_or_none(value)
    if number is None or number <= 0:
        return None
    return number


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
