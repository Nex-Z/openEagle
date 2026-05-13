from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field


ScheduleType = Literal["cron", "interval", "date"]
WorkerKind = Literal["general", "coding", "research", "solo"]
ExecutionStatus = Literal["running", "completed", "failed"]


class ScheduledTask(BaseModel):
    id: str = Field(default_factory=lambda: f"task-{uuid4().hex[:12]}")
    name: str
    prompt: str
    schedule_expr: str = Field(alias="scheduleExpr")
    schedule_type: ScheduleType = Field(default="cron", alias="scheduleType")
    enabled: bool = True
    worker_kind: WorkerKind = Field(default="general", alias="workerKind")
    conversation_id: str | None = Field(default=None, alias="conversationId")
    im_channel: str | None = Field(default=None, alias="imChannel")
    im_chat_id: str | None = Field(default=None, alias="imChatId")
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat(), alias="createdAt")
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat(), alias="updatedAt")
    next_run_at: str | None = Field(default=None, alias="nextRunAt")
    last_run_at: str | None = Field(default=None, alias="lastRunAt")

    model_config = {
        "populate_by_name": True,
    }


class ScheduledTaskExecution(BaseModel):
    id: str = Field(default_factory=lambda: f"exec-{uuid4().hex[:12]}")
    task_id: str = Field(alias="taskId")
    status: ExecutionStatus = "running"
    result: str | None = None
    error: str | None = None
    started_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat(), alias="startedAt")
    completed_at: str | None = Field(default=None, alias="completedAt")
    conversation_id: str | None = Field(default=None, alias="conversationId")

    model_config = {
        "populate_by_name": True,
    }
