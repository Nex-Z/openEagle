from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

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


class AttachmentRef(BaseModel):
    id: str = Field(default_factory=lambda: f"att-{uuid4().hex}")
    name: str = ""
    mime_type: str = Field(default="application/octet-stream", alias="mimeType")
    size: int = 0
    kind: Literal["image", "file", "audio", "video", "unknown"] = "file"
    source: Literal["local", "remote", "generated"] = "local"
    local_path: str | None = Field(default=None, alias="localPath")
    remote_meta: dict[str, Any] = Field(default_factory=dict, alias="remoteMeta")
    status: Literal["pending", "ready", "error"] = "ready"
    error: str | None = None
    content_base64: str | None = Field(default=None, alias="contentBase64", exclude=True)

    model_config = {
        "populate_by_name": True,
    }


class ConversationHistoryMessage(BaseModel):
    id: str = ""
    request_id: str = Field(default="", alias="requestId")
    role: Literal["user", "assistant"]
    content: str
    created_at: str = Field(default="", alias="createdAt")

    model_config = {
        "populate_by_name": True,
    }


class MessagePayload(BaseModel):
    content: str
    attachments: list[AttachmentRef] = Field(default_factory=list)
    history: list[ConversationHistoryMessage] = Field(default_factory=list)


class ErrorPayload(BaseModel):
    message: str
    code: str | None = None


class TracePayload(BaseModel):
    trace: dict[str, Any]


class SoloScreenshotPayload(BaseModel):
    path: str
    width: int | None = None
    height: int | None = None
    captured_at: str | None = Field(default=None, alias="capturedAt")

    model_config = {
        "populate_by_name": True,
    }


class SoloControlPayload(BaseModel):
    action: str
    solo_request_id: str | None = Field(default=None, alias="soloRequestId")
    result: dict[str, Any] | None = None

    model_config = {
        "populate_by_name": True,
    }


class ToolConfirmationPayload(BaseModel):
    confirmation_id: str = Field(alias="confirmationId")
    decision: str

    model_config = {
        "populate_by_name": True,
    }


class SoloStatusPayload(BaseModel):
    state: str
    detail: str | None = None
    step_count: int = Field(default=0, alias="stepCount")
    max_steps: int = Field(default=100, alias="maxSteps")
    last_action: str | None = Field(default=None, alias="lastAction")
    last_screenshot_at: str | None = Field(default=None, alias="lastScreenshotAt")
    log_path: str | None = Field(default=None, alias="logPath")
    started_at: str | None = Field(default=None, alias="startedAt")
    completed_at: str | None = Field(default=None, alias="completedAt")

    model_config = {
        "populate_by_name": True,
    }


class SoloStepVisualPayload(BaseModel):
    kind: Literal["point", "scroll", "keyboard", "command", "navigation", "wait", "none"] = "none"
    x: int | None = None
    y: int | None = None
    display_index: int | None = Field(default=None, alias="displayIndex")
    coordinate_space: Literal["screen", "screenshot", "unknown"] | None = Field(
        default=None,
        alias="coordinateSpace",
    )
    screenshot_path: str | None = Field(default=None, alias="screenshotPath")
    screenshot_x: int | None = Field(default=None, alias="screenshotX")
    screenshot_y: int | None = Field(default=None, alias="screenshotY")
    screenshot_width: int | None = Field(default=None, alias="screenshotWidth")
    screenshot_height: int | None = Field(default=None, alias="screenshotHeight")
    display_text: str | None = Field(default=None, alias="displayText")
    target_label: str | None = Field(default=None, alias="targetLabel")
    safe_args_preview: dict[str, Any] | None = Field(default=None, alias="safeArgsPreview")

    model_config = {
        "populate_by_name": True,
    }


class SoloStepPayload(BaseModel):
    step_index: int = Field(alias="stepIndex")
    action: str
    action_args: dict[str, Any] = Field(default_factory=dict, alias="actionArgs")
    thought_summary: str = Field(alias="thoughtSummary")
    agent_message: str | None = Field(default=None, alias="agentMessage")
    expected_outcome: str | None = Field(default=None, alias="expectedOutcome")
    findings: list[str] = Field(default_factory=list)
    confidence: float | None = None
    screen_state: str | None = Field(default=None, alias="screenState")
    screenshot_path: str | None = Field(default=None, alias="screenshotPath")
    visual: SoloStepVisualPayload | None = None
    timestamp: str

    model_config = {
        "populate_by_name": True,
    }


class SoloPlanItemPayload(BaseModel):
    index: int
    action: str
    description: str
    status: str = "pending"


class SoloPlanStatusPayload(BaseModel):
    items: list[SoloPlanItemPayload]
    task_analysis: str = Field(default="", alias="taskAnalysis")
    alternative: str = ""
    agent_message: str = Field(default="", alias="agentMessage")
    replan_count: int = Field(default=0, alias="replanCount")

    model_config = {
        "populate_by_name": True,
    }


class SoloConfirmationPayload(BaseModel):
    step_index: int = Field(alias="stepIndex")
    risk_level: str = Field(default="confirm", alias="riskLevel")
    reason: str
    action: str
    action_args: dict[str, Any] = Field(default_factory=dict, alias="actionArgs")
    thought_summary: str = Field(alias="thoughtSummary")
    visual: SoloStepVisualPayload | None = None

    model_config = {
        "populate_by_name": True,
    }


class ScheduledTaskPayload(BaseModel):
    task: dict[str, Any]

    model_config = {
        "populate_by_name": True,
    }


class ScheduledTaskListPayload(BaseModel):
    tasks: list[dict[str, Any]]

    model_config = {
        "populate_by_name": True,
    }


class ScheduledTaskHistoryPayload(BaseModel):
    task_id: str = Field(alias="taskId")
    executions: list[dict[str, Any]]

    model_config = {
        "populate_by_name": True,
    }


def utc_now() -> str:
    return datetime.now(UTC).isoformat()
