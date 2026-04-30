from __future__ import annotations

import hashlib
import re

from .models import IMConversationBinding, IMMessageSource

_SAFE_ID = re.compile(r"[^A-Za-z0-9_-]+")


def safe_conversation_id(channel: str, chat_id: str) -> str:
    digest = hashlib.sha256(f"{channel}:{chat_id}".encode("utf-8")).hexdigest()[:24]
    safe_channel = _SAFE_ID.sub("_", channel).strip("_") or "im"
    return f"im_{safe_channel}_{digest}"


def build_conversation_binding(source: IMMessageSource) -> IMConversationBinding:
    chat_label = source.chat_id[-8:] if len(source.chat_id) > 8 else source.chat_id
    provider_label = "飞书" if source.channel == "feishu" else "Telegram"
    if source.chat_type == "private":
        name = source.user_name.strip() or source.user_id[-8:] or "私聊"
        title = f"{provider_label} · {name}"
    else:
        title = f"{provider_label}群聊 · {chat_label}"
    return IMConversationBinding(
        conversation_id=safe_conversation_id(source.channel, source.chat_id),
        title=title,
        source=source,
    )


def is_source_allowed(config: object, source: IMMessageSource) -> bool:
    raw_user_ids = getattr(config, "allowed_user_ids", [])
    if not raw_user_ids:
        raw_user_ids = getattr(config, "allowed_open_ids", [])
    raw_chat_ids = getattr(config, "allowed_chat_ids", [])
    allowed_user_ids = {item.strip() for item in raw_user_ids if item.strip()}
    allowed_chat_ids = {item.strip() for item in raw_chat_ids if item.strip()}
    if not allowed_user_ids and not allowed_chat_ids:
        return False
    return source.user_id in allowed_user_ids or source.chat_id in allowed_chat_ids
