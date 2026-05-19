from __future__ import annotations

import json
import math
from dataclasses import dataclass
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from .config import ContextConfig

TOOL_RESULT_PLACEHOLDER = "[tool result compacted by context cleanup]"
TOOL_INPUT_PLACEHOLDER = {"_context_cleanup": "tool input compacted"}
MIDDLE_TEXT_PLACEHOLDER = "\n[...middle context truncated...]"


@dataclass(frozen=True)
class ContextCleanupResult:
    messages: list[dict[str, Any]]
    changed: bool
    original_tokens: int
    compacted_tokens: int
    method: str = "none"
    summary: str | None = None


ContextSummarizer = Callable[[str], Awaitable[str]]
ContextSnapshot = Callable[[str, dict[str, Any]], Awaitable[None]]


def estimate_text_tokens(text: str) -> int:
    if not text:
        return 0
    cjk = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    other = len(text) - cjk
    return max(1, cjk + math.ceil(other / 4))


def estimate_messages_tokens(
    messages: list[dict[str, Any]],
    *,
    system_prompt: str | None = None,
) -> int:
    total = estimate_text_tokens(system_prompt or "")
    for message in messages:
        total += estimate_text_tokens(_stringify(message))
    return total


def compact_messages_for_prompt(
    messages: list[dict[str, Any]],
    config: ContextConfig,
    *,
    system_prompt: str | None = None,
    force: bool = False,
) -> ContextCleanupResult:
    original_tokens = estimate_messages_tokens(messages, system_prompt=system_prompt)
    if not config.enabled and not force:
        return ContextCleanupResult(
            messages=messages,
            changed=False,
            original_tokens=original_tokens,
            compacted_tokens=original_tokens,
        )
    if not force and original_tokens <= max(0, config.max_input_tokens):
        return ContextCleanupResult(
            messages=messages,
            changed=False,
            original_tokens=original_tokens,
            compacted_tokens=original_tokens,
        )

    protected_indices = _protected_message_indices(messages, config)
    compacted: list[dict[str, Any]] = []
    changed = False

    for index, message in enumerate(messages):
        if index in protected_indices:
            compacted.append(message)
            continue

        next_message = _compact_middle_message(message, config)
        if next_message is None:
            changed = True
            continue
        if next_message is not message:
            changed = True
        compacted.append(next_message)

    compacted_tokens = estimate_messages_tokens(compacted, system_prompt=system_prompt)
    return ContextCleanupResult(
        messages=compacted,
        changed=changed,
        original_tokens=original_tokens,
        compacted_tokens=compacted_tokens,
        method="rule" if changed else "none",
    )


async def compact_messages_for_prompt_with_ai(
    messages: list[dict[str, Any]],
    config: ContextConfig,
    *,
    system_prompt: str | None = None,
    force: bool = False,
    summarizer: ContextSummarizer | None = None,
    snapshot: ContextSnapshot | None = None,
) -> ContextCleanupResult:
    original_tokens = estimate_messages_tokens(messages, system_prompt=system_prompt)
    if not config.enabled and not force:
        return ContextCleanupResult(
            messages=messages,
            changed=False,
            original_tokens=original_tokens,
            compacted_tokens=original_tokens,
        )
    if not force and original_tokens <= max(0, config.max_input_tokens):
        return ContextCleanupResult(
            messages=messages,
            changed=False,
            original_tokens=original_tokens,
            compacted_tokens=original_tokens,
        )

    protected_indices = _protected_message_indices(messages, config)
    middle_messages = [
        message for index, message in enumerate(messages) if index not in protected_indices
    ]
    if not middle_messages:
        return ContextCleanupResult(
            messages=messages,
            changed=False,
            original_tokens=original_tokens,
            compacted_tokens=original_tokens,
        )

    summary_source = _render_messages_for_summary(
        _preclean_middle_messages_for_summary(middle_messages, config),
        config,
    )
    if config.snapshot_on_compaction and snapshot is not None:
        try:
            await snapshot(
                summary_source,
                {
                    "reason": "context_compaction",
                    "originalTokens": original_tokens,
                    "messageCount": len(messages),
                    "middleMessageCount": len(middle_messages),
                },
            )
        except Exception:
            pass

    if config.ai_summary_enabled and summarizer is not None and summary_source.strip():
        try:
            raw_summary = await summarizer(
                _build_summary_prompt(summary_source, config.summary_char_limit)
            )
            summary = _truncate_text(raw_summary.strip(), config.summary_char_limit)
        except Exception:
            summary = ""
        if summary:
            compacted = _replace_middle_with_summary(messages, protected_indices, summary)
            compacted_tokens = estimate_messages_tokens(compacted, system_prompt=system_prompt)
            return ContextCleanupResult(
                messages=compacted,
                changed=True,
                original_tokens=original_tokens,
                compacted_tokens=compacted_tokens,
                method="ai_summary",
                summary=summary,
            )

    return compact_messages_for_prompt(
        messages,
        config,
        system_prompt=system_prompt,
        force=True,
    )


def should_cleanup_for_idle(
    last_activity_at: datetime | None,
    config: ContextConfig,
    *,
    now: datetime | None = None,
) -> bool:
    if not config.enabled or last_activity_at is None:
        return False
    idle_minutes = max(0, config.im_idle_cleanup_minutes)
    if idle_minutes <= 0:
        return False
    current = now or datetime.now(UTC)
    if last_activity_at.tzinfo is None:
        last_activity_at = last_activity_at.replace(tzinfo=UTC)
    return (current - last_activity_at).total_seconds() >= idle_minutes * 60


def _protected_message_indices(
    messages: list[dict[str, Any]],
    config: ContextConfig,
) -> set[int]:
    recent_count = max(0, config.preserve_recent_messages)
    recent_start = max(0, len(messages) - recent_count) if recent_count else len(messages)
    protected = {
        index
        for index, message in enumerate(messages)
        if index >= recent_start or message.get("role") == "system"
    }

    changed = True
    while changed:
        changed = False
        for index, message in enumerate(messages):
            if index not in protected:
                continue
            if _message_has_tool_result(message) and index > 0 and index - 1 not in protected:
                if _message_has_tool_use(messages[index - 1]):
                    protected.add(index - 1)
                    changed = True
            if (
                _message_has_tool_use(message)
                and index + 1 < len(messages)
                and index + 1 not in protected
            ):
                if _message_has_tool_result(messages[index + 1]):
                    protected.add(index + 1)
                    changed = True
    return protected


def _preclean_middle_messages_for_summary(
    messages: list[dict[str, Any]],
    config: ContextConfig,
) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    for message in messages:
        next_message = _preclean_middle_message_for_summary(message, config)
        if next_message is not None:
            cleaned.append(next_message)
    return cleaned


def _preclean_middle_message_for_summary(
    message: dict[str, Any],
    config: ContextConfig,
) -> dict[str, Any] | None:
    if _is_standalone_tool_message(message):
        if config.tool_message_mode == "remove":
            return None
        return {
            **message,
            "content": _compact_tool_result_content(message.get("content"), config),
        }

    content = message.get("content")
    if _contains_structured_tool_content(content):
        return {
            **message,
            "content": _compact_content_blocks(content, config),
        }
    return message


def _render_messages_for_summary(
    messages: list[dict[str, Any]],
    config: ContextConfig,
) -> str:
    rows: list[str] = []
    for index, message in enumerate(messages, start=1):
        role = str(message.get("role") or "unknown")
        rows.append(f"{index}. role={role}\n{_content_text(message.get('content'))}")
    text = "\n\n".join(rows)
    source_limit = max(4_000, min(48_000, max(0, config.max_input_tokens) * 4))
    return _truncate_text(text, source_limit)


def _build_summary_prompt(summary_source: str, summary_char_limit: int) -> str:
    return (
        "你是 openEagle 的上下文压缩器。下面是即将从 prompt 中移除的中间对话片段，"
        "工具输入和工具结果已经预先压缩，请不要尝试还原原始工具输出。\n"
        "请用中文写一段可继续注入对话的摘要，保留：用户明确目标、重要约束、已经做过的决定、"
        "关键文件/路径/命令、仍未解决的问题、对后续回复有影响的工具观察。"
        "丢弃冗余寒暄、重复日志、无关细节和大段工具原文。"
        "不要编造不存在的信息。\n\n"
        f"摘要长度上限约 {max(200, summary_char_limit)} 字。\n\n"
        "中间对话片段:\n"
        f"{summary_source}"
    )


def _replace_middle_with_summary(
    messages: list[dict[str, Any]],
    protected_indices: set[int],
    summary: str,
) -> list[dict[str, Any]]:
    compacted: list[dict[str, Any]] = []
    inserted = False
    summary_message = {
        "role": "user",
        "content": "【已压缩的中段上下文摘要】\n" + summary,
    }
    for index, message in enumerate(messages):
        if index in protected_indices:
            compacted.append(message)
            continue
        if not inserted:
            compacted.append(summary_message)
            inserted = True
    return compacted


def _compact_middle_message(
    message: dict[str, Any],
    config: ContextConfig,
) -> dict[str, Any] | None:
    if _is_standalone_tool_message(message):
        if config.tool_message_mode == "remove":
            return None
        return {
            **message,
            "content": _compact_tool_result_content(message.get("content"), config),
        }

    content = message.get("content")
    if _contains_structured_tool_content(content):
        return {
            **message,
            "content": _compact_content_blocks(content, config),
        }

    next_content = _compact_plain_content(content, config.middle_message_char_limit)
    if next_content is content:
        return message
    return {
        **message,
        "content": next_content,
    }


def _is_standalone_tool_message(message: dict[str, Any]) -> bool:
    return str(message.get("role") or "") == "tool"


def _contains_structured_tool_content(content: Any) -> bool:
    blocks = content if isinstance(content, list) else [content]
    return any(_block_type(block) in {"tool_result", "tool_use"} for block in blocks)


def _message_has_tool_result(message: dict[str, Any]) -> bool:
    if str(message.get("role") or "") == "tool":
        return True
    content = message.get("content")
    blocks = content if isinstance(content, list) else [content]
    return any(_block_type(block) == "tool_result" for block in blocks)


def _message_has_tool_use(message: dict[str, Any]) -> bool:
    content = message.get("content")
    blocks = content if isinstance(content, list) else [content]
    return any(_block_type(block) == "tool_use" for block in blocks)


def _compact_content_blocks(content: Any, config: ContextConfig) -> Any:
    if not isinstance(content, list):
        return content
    return [_compact_block(block, config) for block in content]


def _compact_block(block: Any, config: ContextConfig) -> Any:
    block_type = _block_type(block)
    if block_type == "tool_result":
        result: dict[str, Any] = {
            "type": "tool_result",
            "content": _compact_tool_result_content(_block_value(block, "content"), config),
        }
        tool_use_id = _block_value(block, "tool_use_id")
        if tool_use_id is not None:
            result["tool_use_id"] = tool_use_id
        is_error = _block_value(block, "is_error")
        if is_error is not None:
            result["is_error"] = is_error
        return result

    if block_type == "tool_use":
        result = {
            "type": "tool_use",
            "id": _block_value(block, "id"),
            "name": _block_value(block, "name"),
            "input": TOOL_INPUT_PLACEHOLDER,
        }
        return {key: value for key, value in result.items() if value is not None}

    if block_type == "text":
        text = _block_value(block, "text")
        if isinstance(text, str):
            compacted_text = _truncate_text(text, config.middle_message_char_limit)
            if compacted_text != text:
                if isinstance(block, dict):
                    return {**block, "text": compacted_text}
                return {"type": "text", "text": compacted_text}
    return block


def _compact_plain_content(content: Any, char_limit: int) -> Any:
    if isinstance(content, str):
        return _truncate_text(content, char_limit)
    if isinstance(content, list):
        changed = False
        next_blocks = []
        for block in content:
            if _block_type(block) == "text":
                text = _block_value(block, "text")
                if isinstance(text, str):
                    compacted_text = _truncate_text(text, char_limit)
                    changed = changed or compacted_text != text
                    if isinstance(block, dict):
                        next_blocks.append({**block, "text": compacted_text})
                    else:
                        next_blocks.append({"type": "text", "text": compacted_text})
                    continue
            next_blocks.append(block)
        return next_blocks if changed else content
    return content


def _compact_tool_result_content(content: Any, config: ContextConfig) -> str:
    limit = max(0, config.tool_result_char_limit)
    if limit <= 0:
        return TOOL_RESULT_PLACEHOLDER
    text = content if isinstance(content, str) else _stringify(content)
    preview = _truncate_text(text, limit)
    return f"{TOOL_RESULT_PLACEHOLDER}\n{preview}" if preview else TOOL_RESULT_PLACEHOLDER


def _truncate_text(text: str, char_limit: int) -> str:
    limit = max(0, char_limit)
    if not text or len(text) <= limit:
        return text
    if limit <= len(MIDDLE_TEXT_PLACEHOLDER):
        return MIDDLE_TEXT_PLACEHOLDER.strip()
    keep = max(0, limit - len(MIDDLE_TEXT_PLACEHOLDER))
    return text[:keep].rstrip() + MIDDLE_TEXT_PLACEHOLDER


def _block_type(block: Any) -> str | None:
    if isinstance(block, dict):
        value = block.get("type")
    else:
        value = getattr(block, "type", None)
    return str(value) if value is not None else None


def _block_value(block: Any, key: str) -> Any:
    if isinstance(block, dict):
        return block.get(key)
    return getattr(block, key, None)


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            block_type = _block_type(block)
            if block_type == "text":
                text = _block_value(block, "text")
                parts.append(str(text or ""))
            else:
                parts.append(_stringify(block))
        return "\n".join(part for part in parts if part)
    return _stringify(content)


def _stringify(value: Any) -> str:
    try:
        return json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True)
    except Exception:
        return str(value)


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return _jsonable(model_dump())
        except Exception:
            pass
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return _jsonable(to_dict())
        except Exception:
            pass
    return str(value)
