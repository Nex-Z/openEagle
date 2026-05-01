from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from .models import utc_now


RouteName = Literal[
    "answer_directly",
    "delegate_new",
    "delegate_existing",
    "start_solo",
    "control_solo",
    "clarify",
]
WorkerKind = Literal["general", "coding", "research", "solo"]
WorkerState = Literal[
    "idle",
    "running",
    "waiting_confirmation",
    "completed",
    "failed",
    "cancelled",
    "expired",
]


class AgentRouteDecision(BaseModel):
    route: RouteName = "delegate_new"
    task_title: str = Field(default="", alias="task_title")
    task_brief: str = Field(default="", alias="task_brief")
    success_criteria: list[str] = Field(default_factory=list, alias="success_criteria")
    worker_kind: WorkerKind = Field(default="general", alias="worker_kind")
    target_worker_id: str | None = Field(default=None, alias="target_worker_id")
    requires_write: bool = Field(default=False, alias="requires_write")
    requires_gui: bool = Field(default=False, alias="requires_gui")
    user_visible_summary: str = Field(default="", alias="user_visible_summary")
    context_summary: str = Field(default="", alias="context_summary")

    model_config = {
        "populate_by_name": True,
    }


@dataclass
class WorkerReport:
    worker_id: str
    worker_kind: WorkerKind
    state: WorkerState
    title: str
    summary: str
    evidence: list[str] = field(default_factory=list)
    result: str = ""
    error: str | None = None
    started_at: str = field(default_factory=utc_now)
    completed_at: str | None = None

    def to_trace_result(self) -> str:
        parts = [self.summary.strip() or self.result.strip()]
        if self.evidence:
            parts.append("证据:\n" + "\n".join(f"- {item}" for item in self.evidence))
        if self.error:
            parts.append(f"错误: {self.error}")
        return "\n\n".join(part for part in parts if part)


@dataclass
class AgentTaskRecord:
    conversation_id: str
    worker_kind: WorkerKind
    title: str
    task_brief: str
    success_criteria: list[str]
    context_summary: str = ""
    requires_write: bool = False
    requires_gui: bool = False
    worker_id: str = field(default_factory=lambda: f"worker-{uuid4()}")
    state: WorkerState = "idle"
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    completed_at: str | None = None
    last_report: WorkerReport | None = None

    @property
    def scoped_conversation_id(self) -> str:
        return f"{self.conversation_id}:worker:{self.worker_id}"

    def mark(self, state: WorkerState) -> None:
        self.state = state
        self.updated_at = utc_now()
        if state in {"completed", "failed", "cancelled", "expired"}:
            self.completed_at = self.updated_at

    def to_trace_params(self) -> dict[str, Any]:
        return {
            "agentTaskId": self.worker_id,
            "workerKind": self.worker_kind,
            "title": self.title,
            "requiresWrite": self.requires_write,
            "requiresGui": self.requires_gui,
        }
