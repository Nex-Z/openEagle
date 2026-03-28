from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field


class Envelope(BaseModel):
    type: str
    request_id: str = Field(alias="requestId")
    conversation_id: str = Field(alias="conversationId")
    payload: dict[str, Any]
    timestamp: str

    model_config = {
        "populate_by_name": True,
    }


class StatusPayload(BaseModel):
    stage: str
    detail: str | None = None


class MessagePayload(BaseModel):
    content: str


class ErrorPayload(BaseModel):
    message: str
    code: str | None = None


def utc_now() -> str:
    return datetime.now(UTC).isoformat()
