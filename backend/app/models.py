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


class SoloStartPayload(BaseModel):
    content: str
    screenshot: SoloScreenshotPayload | None = None


class SoloControlPayload(BaseModel):
    action: str
    solo_request_id: str | None = Field(default=None, alias="soloRequestId")
    result: dict[str, Any] | None = None

    model_config = {
        "populate_by_name": True,
    }


class SoloStatusPayload(BaseModel):
    state: str
    detail: str | None = None
    step_count: int = Field(default=0, alias="stepCount")
    max_steps: int = Field(default=25, alias="maxSteps")
    last_action: str | None = Field(default=None, alias="lastAction")
    last_screenshot_at: str | None = Field(default=None, alias="lastScreenshotAt")
    started_at: str | None = Field(default=None, alias="startedAt")
    completed_at: str | None = Field(default=None, alias="completedAt")

    model_config = {
        "populate_by_name": True,
    }


class SoloStepPayload(BaseModel):
    step_index: int = Field(alias="stepIndex")
    action: str
    action_args: dict[str, Any] = Field(default_factory=dict, alias="actionArgs")
    thought_summary: str = Field(alias="thoughtSummary")
    expected_outcome: str | None = Field(default=None, alias="expectedOutcome")
    screenshot_path: str | None = Field(default=None, alias="screenshotPath")
    timestamp: str

    model_config = {
        "populate_by_name": True,
    }


class SoloConfirmationPayload(BaseModel):
    step_index: int = Field(alias="stepIndex")
    reason: str
    action: str
    action_args: dict[str, Any] = Field(default_factory=dict, alias="actionArgs")
    thought_summary: str = Field(alias="thoughtSummary")

    model_config = {
        "populate_by_name": True,
    }


def utc_now() -> str:
    return datetime.now(UTC).isoformat()
