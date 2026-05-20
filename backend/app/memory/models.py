from __future__ import annotations

from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from ..models import utc_now


MemoryNoteStatus = Literal["active", "archived"]


class MemoryProfilePayload(BaseModel):
    content: str = ""
    updated_at: str = Field(default_factory=utc_now, alias="updatedAt")
    manual_updated_at: str | None = Field(default=None, alias="manualUpdatedAt")

    model_config = {"populate_by_name": True}


class MemoryNotePayload(BaseModel):
    id: str = Field(default_factory=lambda: f"note-{uuid4().hex}")
    text: str
    tags: list[str] = Field(default_factory=list)
    source: str = "manual"
    confidence: float = 1.0
    status: MemoryNoteStatus = "active"
    created_at: str = Field(default_factory=utc_now, alias="createdAt")
    updated_at: str = Field(default_factory=utc_now, alias="updatedAt")

    model_config = {"populate_by_name": True}


class AgentSoulPayload(BaseModel):
    core: str = ""
    side_notes: str = Field(default="", alias="sideNotes")
    updated_at: str = Field(default_factory=utc_now, alias="updatedAt")
    side_notes_updated_at: str | None = Field(default=None, alias="sideNotesUpdatedAt")

    model_config = {"populate_by_name": True}


class MemoryAuditPayload(BaseModel):
    id: str = Field(default_factory=lambda: f"audit-{uuid4().hex}")
    action: str
    target_kind: str = Field(alias="targetKind")
    target_id: str = Field(default="", alias="targetId")
    summary: str = ""
    source: str = "system"
    created_at: str = Field(default_factory=utc_now, alias="createdAt")

    model_config = {"populate_by_name": True}


class MemoryEventPayload(BaseModel):
    id: str = Field(default_factory=lambda: f"event-{uuid4().hex}")
    source: str
    conversation_id: str = Field(default="", alias="conversationId")
    request_id: str = Field(default="", alias="requestId")
    summary: str = ""
    content: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=utc_now, alias="createdAt")

    model_config = {"populate_by_name": True}


class MemoryStatePayload(BaseModel):
    profile: MemoryProfilePayload = Field(default_factory=MemoryProfilePayload)
    notes: list[MemoryNotePayload] = Field(default_factory=list)
    agent_soul: AgentSoulPayload = Field(default_factory=AgentSoulPayload, alias="agentSoul")
    audit: list[MemoryAuditPayload] = Field(default_factory=list)
    events: list[MemoryEventPayload] = Field(default_factory=list)

    model_config = {"populate_by_name": True}
