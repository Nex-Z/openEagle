from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from ..models import AttachmentRef


IMChannel = Literal["feishu", "telegram"]
IMChatType = Literal["private", "group"]


@dataclass(frozen=True)
class IMMessageSource:
    channel: IMChannel
    chat_id: str
    chat_type: IMChatType
    user_id: str
    user_name: str = ""
    message_id: str = ""


@dataclass(frozen=True)
class IMEvent:
    source: IMMessageSource
    text: str
    attachments: list[AttachmentRef] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IMOutboundMessage:
    source: IMMessageSource
    text: str
    attachments: list[AttachmentRef] = field(default_factory=list)


@dataclass(frozen=True)
class IMConversationBinding:
    conversation_id: str
    title: str
    source: IMMessageSource


@dataclass(frozen=True)
class IMStatus:
    provider: IMChannel
    state: Literal["disabled", "starting", "connected", "error"]
    detail: str = ""
    last_blocked_open_id: str | None = None
    last_blocked_chat_id: str | None = None
