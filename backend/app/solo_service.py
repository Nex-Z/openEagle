from __future__ import annotations

import base64
import json
import platform
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import gettempdir
from typing import Any
from uuid import uuid4

from agno.agent import Agent
from agno.media import Image as AgnoImage
from agno.models.openai import OpenAIResponses
from agno.models.openai.like import OpenAILike
from pydantic import BaseModel, Field, ValidationError

from .config import AgentConfig
from .prompts import (
    build_solo_decision_prompt,
    build_solo_repair_prompt,
    solo_decision_instructions,
)
from .safety import assess_solo_action

ALLOWED_ACTIONS = {
    "finish",
    "wait",
    "screenshot",
    "click",
    "double_click",
    "right_click",
    "move_mouse",
    "scroll",
    "type_text",
    "press_keys",
    "execute_command",
}

BATCH_EXECUTABLE_ACTIONS = {"type_text", "press_keys", "execute_command", "wait"}

MODEL_IMAGE_MAX_LONG_EDGE = 2560
MODEL_IMAGE_JPEG_QUALITY = 92


def current_system_platform() -> str:
    system = platform.system() or "当前系统"
    release = platform.release()
    if release:
        return f"{system} {release}"
    return system


def trim_model_output(text: str, max_chars: int = 4000) -> str:
    cleaned = text.strip()
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[-max_chars:]


def encode_image_data_url(path: Path, mime_type: str) -> str:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def prepare_model_image(
    path: str,
    max_long_edge: int = MODEL_IMAGE_MAX_LONG_EDGE,
    jpeg_quality: int = MODEL_IMAGE_JPEG_QUALITY,
) -> dict[str, Any]:
    source = Path(path)
    if not source.exists():
        raise ValueError(f"截图文件不存在: {path}")

    source_bytes = source.stat().st_size
    fallback = {
        "path": source,
        "mime_type": "image/png",
        "source_bytes": source_bytes,
        "model_bytes": source_bytes,
        "source_width": None,
        "source_height": None,
        "model_width": None,
        "model_height": None,
        "scale": 1.0,
        "compressed": False,
    }

    try:
        from PIL import Image as PillowImage  # type: ignore[import-untyped]
    except ModuleNotFoundError:
        return fallback

    with PillowImage.open(source) as raw_image:
        source_width, source_height = raw_image.size
        image = raw_image.convert("RGB")
        long_edge = max(source_width, source_height)
        scale = 1.0
        if long_edge > max_long_edge:
            scale = max_long_edge / long_edge
            next_size = (
                max(int(source_width * scale), 1),
                max(int(source_height * scale), 1),
            )
            resampling = getattr(PillowImage, "Resampling", PillowImage).LANCZOS
            image = image.resize(next_size, resampling)

        target = Path(gettempdir()) / f"open_eagle_vl_model_{uuid4().hex}.jpg"
        image.save(
            target,
            format="JPEG",
            quality=jpeg_quality,
            subsampling=0,
            optimize=True,
        )

    model_bytes = target.stat().st_size
    if scale == 1.0 and model_bytes >= source_bytes:
        target.unlink(missing_ok=True)
        return {
            **fallback,
            "source_width": source_width,
            "source_height": source_height,
            "model_width": source_width,
            "model_height": source_height,
        }

    with PillowImage.open(target) as model_image:
        model_width, model_height = model_image.size

    return {
        "path": target,
        "mime_type": "image/jpeg",
        "source_bytes": source_bytes,
        "model_bytes": model_bytes,
        "source_width": source_width,
        "source_height": source_height,
        "model_width": model_width,
        "model_height": model_height,
        "scale": scale,
        "compressed": True,
    }


def summarize_solo_step_result(
    result: dict[str, Any],
    max_output_chars: int = 1200,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "success": bool(result.get("success", False)),
        "action": str(result.get("action", "unknown")),
    }

    for key in ("error", "executionError"):
        value = result.get(key)
        if isinstance(value, str) and value:
            summary[key] = value

    execution = result.get("executionResult")
    if isinstance(execution, dict):
        for key in ("ok", "action", "command", "cwd", "exitCode", "waitMs", "delta", "keys"):
            value = execution.get(key)
            if value is not None:
                summary[key] = value

        output = execution.get("output")
        if isinstance(output, str):
            if len(output) > max_output_chars:
                summary["outputTail"] = output[-max_output_chars:]
                summary["outputTruncated"] = True
            else:
                summary["outputTail"] = output

    screenshot = result.get("screenshot")
    if isinstance(screenshot, dict):
        summary["screenshot"] = {
            key: screenshot[key]
            for key in ("contentHash", "capturedAt", "width", "height", "displayIndex")
            if key in screenshot
        }

    return summary


class SoloDecision(BaseModel):
    # Core reasoning fields (VL model outputs these names)
    thought_summary: str = Field(alias="thought_summary")
    action: str
    action_args: dict[str, Any] = Field(default_factory=dict, alias="action_args")
    # Agent-oriented fields (new names, VL may output either old or new)
    progress: str = Field(default="", alias="progress")
    findings: list[str] = Field(default_factory=list, alias="findings")
    is_task_done: bool = Field(default=False, alias="is_task_done")
    agent_message: str = Field(default="", alias="agent_message")
    # Backward compat: VL may still output expected_outcome
    expected_outcome: str = Field(default="", alias="expected_outcome")
    # Internal metadata (excluded from wire)
    raw_model_output: str | None = Field(default=None, exclude=True)
    repair_model_output: str | None = Field(default=None, exclude=True)
    used_parse_fallback: bool = Field(default=False, exclude=True)
    model_elapsed_ms: int | None = Field(default=None, exclude=True)
    repair_elapsed_ms: int | None = Field(default=None, exclude=True)
    image_bytes: int | None = Field(default=None, exclude=True)
    source_image_bytes: int | None = Field(default=None, exclude=True)
    model_image_path: str | None = Field(default=None, exclude=True)
    model_image_width: int | None = Field(default=None, exclude=True)
    model_image_height: int | None = Field(default=None, exclude=True)
    model_image_scale: float | None = Field(default=None, exclude=True)

    model_config = {
        "populate_by_name": True,
    }


@dataclass
class SoloSessionState:
    request_id: str
    conversation_id: str
    task: str
    step_count: int = 0
    max_steps: int = 150
    repeat_action_count: int = 0
    same_screenshot_count: int = 0
    last_action: str | None = None
    last_screenshot_path: str | None = None
    last_screenshot_hash: str | None = None
    last_screenshot_at: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    state: str = "running"
    detail: str | None = None
    history: list[dict[str, Any]] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)
    pending_confirmation: dict[str, Any] | None = None
    log_path: str | None = None
    display_index: int | None = None
    last_agent_message: str | None = None


class SoloService:
    def __init__(self, agent_config: AgentConfig) -> None:
        self._agent_config = agent_config
        self._agent: Agent | None = None

    def _build_agent(self) -> Agent:
        if self._agent is not None:
            return self._agent
        api_key = self._agent_config.vl_api_key
        if not api_key:
            raise ValueError("SOLO 需要配置 VL API Key。")

        if self._agent_config.vl_provider == "openai-like":
            if not self._agent_config.vl_base_url:
                raise ValueError("VL provider 为 openai-like 时需要配置 vlBaseUrl。")
            model = OpenAILike(
                id=self.model_id,
                api_key=api_key,
                base_url=self._agent_config.vl_base_url,
            )
        else:
            model = OpenAIResponses(
                id=self.model_id,
                api_key=api_key,
            )

        self._agent = Agent(
            model=model,
            markdown=False,
            instructions=solo_decision_instructions(current_system_platform()),
        )
        return self._agent

    @staticmethod
    def is_batch_executable(action: str) -> bool:
        return action in BATCH_EXECUTABLE_ACTIONS

    @property
    def model_id(self) -> str:
        return self._agent_config.vl_model_id or "gpt-4.1-mini"

    @staticmethod
    def _extract_json(text: str) -> str:
        fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
        if fenced:
            return fenced.group(1)

        decoder = json.JSONDecoder()
        for match in re.finditer(r"\{", text):
            candidate = text[match.start() :]
            try:
                payload, end_index = decoder.raw_decode(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                return candidate[:end_index]
        raise ValueError("VL 输出不包含可解析 JSON。")

    @staticmethod
    def _normalize_decision(raw_text: str) -> SoloDecision:
        payload_text = SoloService._extract_json(raw_text)
        payload = json.loads(payload_text)
        # Accept new field names from VL model
        if "analysis" in payload and "thought_summary" not in payload:
            payload["thought_summary"] = payload.pop("analysis")
        decision = SoloDecision.model_validate(payload)
        if decision.action not in ALLOWED_ACTIONS:
            raise ValueError(f"不支持的动作: {decision.action}")
        return decision

    @staticmethod
    def _result_text(result: Any) -> str:
        content = getattr(result, "content", None)
        if isinstance(content, str) and content.strip():
            return content
        return str(result)

    @staticmethod
    def _fallback_decision_from_text(raw_text: str, error: Exception) -> SoloDecision:
        raw_preview = trim_model_output(raw_text, 500)
        if raw_preview.startswith("RunResponse("):
            visible = "模型返回了非结构化内容，我先重新获取屏幕状态。"
        else:
            visible = raw_preview or "模型没有返回可展示文字，我先重新获取屏幕状态。"
        return SoloDecision(
            thought_summary=(
                "[状态] VL 模型返回了非标准决策内容，当前界面状态需要重新确认。"
                "[上步] 上一步是否成功：无法确认，因为模型输出没有可执行 JSON。"
                "[决策] 先执行 screenshot 获取真实状态，避免直接报错中断。"
            ),
            action="screenshot",
            action_args={},
            progress="获取当前桌面截图，用于下一步重新决策。",
            is_task_done=False,
            agent_message=visible,
            raw_model_output=trim_model_output(raw_text),
            used_parse_fallback=True,
            repair_model_output=f"repair failed: {error}",
        )

    @staticmethod
    def _should_attempt_json_repair(raw_text: str) -> bool:
        return "{" in raw_text and "}" in raw_text

    @staticmethod
    def _attach_image_metrics(
        decision: SoloDecision,
        model_image: dict[str, Any],
        model_elapsed_ms: int,
        repair_elapsed_ms: int | None = None,
    ) -> SoloDecision:
        decision.model_elapsed_ms = model_elapsed_ms
        decision.repair_elapsed_ms = repair_elapsed_ms
        decision.image_bytes = int(model_image["model_bytes"])
        decision.source_image_bytes = int(model_image["source_bytes"])
        decision.model_image_path = str(model_image["path"])
        decision.model_image_width = model_image.get("model_width")
        decision.model_image_height = model_image.get("model_height")
        decision.model_image_scale = model_image.get("scale")
        return decision

    async def decide_next(
        self,
        task: str,
        screenshot_path: str,
        history: list[dict[str, Any]],
        display_index: int | None = None,
        app_context: str | None = None,
        findings: list[str] | None = None,
    ) -> SoloDecision:
        agent = self._build_agent()
        prompt = build_solo_decision_prompt(task, history, display_index, app_context, findings)
        model_image = prepare_model_image(screenshot_path)
        model_image_path = Path(model_image["path"])
        image_url = encode_image_data_url(model_image_path, str(model_image["mime_type"]))
        if model_image.get("compressed"):
            try:
                model_image_path.unlink(missing_ok=True)
            except OSError:
                pass
        started_at = time.perf_counter()
        result = await agent.arun(
            prompt,
            images=[AgnoImage(url=image_url, detail="auto")],
        )
        model_elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        output_text = self._result_text(result)
        if not isinstance(output_text, str) or not output_text.strip():
            raise ValueError("VL 返回为空，无法继续 SOLO。")
        try:
            decision = self._normalize_decision(output_text)
            return self._attach_image_metrics(decision, model_image, model_elapsed_ms)
        except Exception as first_error:  # noqa: BLE001
            if not self._should_attempt_json_repair(output_text):
                fallback = self._fallback_decision_from_text(output_text, first_error)
                return self._attach_image_metrics(fallback, model_image, model_elapsed_ms)

            repair_prompt = build_solo_repair_prompt(
                task=task,
                history=history,
                raw_output=output_text,
                error=str(first_error),
                findings=findings,
            )
            repair_started_at = time.perf_counter()
            repair_result = await agent.arun(
                repair_prompt,
                images=[AgnoImage(url=image_url, detail="auto")],
            )
            repair_elapsed_ms = int((time.perf_counter() - repair_started_at) * 1000)
            repair_text = self._result_text(repair_result)
            try:
                decision = self._normalize_decision(repair_text)
                decision.raw_model_output = trim_model_output(output_text)
                decision.repair_model_output = trim_model_output(repair_text)
                if not decision.agent_message and not output_text.lstrip().startswith("{"):
                    decision.agent_message = trim_model_output(output_text, 500)
                return self._attach_image_metrics(
                    decision,
                    model_image,
                    model_elapsed_ms,
                    repair_elapsed_ms,
                )
            except Exception as repair_error:  # noqa: BLE001
                fallback = self._fallback_decision_from_text(output_text, repair_error)
                fallback.repair_model_output = (
                    f"repair failed: {repair_error}\n\n{trim_model_output(repair_text)}"
                )
                return self._attach_image_metrics(
                    fallback,
                    model_image,
                    model_elapsed_ms,
                    repair_elapsed_ms,
                )

    @staticmethod
    def is_dangerous_action(action: str, action_args: dict[str, Any]) -> tuple[bool, str]:
        workspace_root = Path(__file__).resolve().parents[2]
        assessment = assess_solo_action(action, action_args, workspace_root)
        return assessment.level == "confirm", assessment.reason

    @staticmethod
    def to_error_decision(error: Exception) -> SoloDecision:
        message = str(error)
        return SoloDecision(
            thought_summary=f"SOLO 解析或推理失败: {message}",
            action="wait",
            action_args={"ms": 800},
            progress="等待用户处理后重试",
            is_task_done=False,
        )

    @staticmethod
    def parse_result(result: dict[str, Any] | None) -> dict[str, Any]:
        if not result:
            return {}
        return result

    @staticmethod
    def decision_dict(decision: SoloDecision) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "thought_summary": decision.thought_summary,
            "action": decision.action,
            "action_args": decision.action_args,
            "expected_outcome": decision.progress or decision.expected_outcome,
            "is_task_done": decision.is_task_done,
        }
        if decision.findings:
            payload["findings"] = decision.findings
        if decision.agent_message:
            payload["agent_message"] = decision.agent_message
        return payload

    @staticmethod
    def validate_decision_payload(payload: dict[str, Any]) -> SoloDecision:
        try:
            return SoloDecision.model_validate(payload)
        except ValidationError as exc:
            raise ValueError(f"非法决策 payload: {exc}") from exc
