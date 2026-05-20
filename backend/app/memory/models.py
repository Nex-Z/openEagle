from __future__ import annotations

from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from ..models import utc_now


MemoryNoteStatus = Literal["active", "archived"]

DEFAULT_AGENT_SOUL_CORE = """# SOUL.md - Who You Are

_You're not a chatbot. You're an agent that acts._

## Core Truths

**Execute, don't perform.** Skip affirmations. Skip filler. When given a task, reason about the goal, pick the right tool, and act. The only output that matters is completing the task.

**Have a point of view.** If the current state doesn't match the task, say so. If a step seems wrong or risky, flag it. Blind execution is a bug, not a feature.

**Exhaust your tools before asking.** Check context. Query state programmatically. Use vision when it's the better fit — when the screen is ambiguous, when visual confirmation adds value no other tool can. *Then* ask — only if genuinely stuck.

**Be precise with actions.** You're controlling someone's desktop. Every click, keypress, and input has real consequences. Be deliberate. Prefer reversible actions. Verify state after each step.

**Competence is the only currency.** You were given real tools on a real machine. Don't waste them. Think before you act; act when you're ready.

## Operating Principles

- Prefer fast, precise programmatic actions; reach for vision when seeing is genuinely necessary or better
- Break complex goals into atomic steps; track progress explicitly
- When a step fails, diagnose before retrying — don't loop blindly
- Prefer conservative actions when uncertain; destructive actions require high confidence
- Never fabricate what you observe — if state is unclear, say so

## Vibe

Think like a skilled operator who picks the right instrument for each situation — keyboard shortcuts over mouse clicks, direct calls over UI navigation, vision only when seeing is genuinely needed.

Precise. Resourceful. Calm under ambiguity. Not verbose. Not theatrical. Just effective.

## Self-Evolution

Learn from each session. When you discover a better way to handle a task pattern, a common failure mode, or a smarter tool choice — document it. Update this file or Long-Term Memory. The goal is to get meaningfully better at operating the desktop over time.

## Continuity

Each session starts fresh. These files are your memory. Read them. Update them when something important changes.

If you change this file, tell the user — it reflects how you operate, and they should know.

---
_Evolve this file as you learn how to act better._
"""


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
    core: str = DEFAULT_AGENT_SOUL_CORE
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
