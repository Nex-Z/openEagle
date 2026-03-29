from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agno.agent import Agent
from agno.media import Image
from agno.models.openai import OpenAIResponses
from agno.models.openai.like import OpenAILike
from pydantic import BaseModel, Field, ValidationError

from .config import AgentConfig

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


class SoloDecision(BaseModel):
    thought_summary: str = Field(alias="thought_summary")
    action: str
    action_args: dict[str, Any] = Field(default_factory=dict, alias="action_args")
    expected_outcome: str = Field(default="", alias="expected_outcome")
    is_task_done: bool = Field(default=False, alias="is_task_done")

    model_config = {
        "populate_by_name": True,
    }


@dataclass
class SoloSessionState:
    request_id: str
    conversation_id: str
    task: str
    step_count: int = 0
    max_steps: int = 25
    repeat_action_count: int = 0
    same_screenshot_count: int = 0
    last_action: str | None = None
    last_screenshot_path: str | None = None
    last_screenshot_at: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    state: str = "running"
    detail: str | None = None
    history: list[dict[str, Any]] = field(default_factory=list)
    pending_confirmation: dict[str, Any] | None = None


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
            instructions=[
                "你是桌面自动化决策模型。",
                "必须仅输出 JSON，禁止输出任何额外文本。",
                "JSON 字段必须为 thought_summary, action, action_args, expected_outcome, is_task_done。",
                "action 仅可取: finish, wait, screenshot, click, double_click, right_click, move_mouse, scroll, type_text, press_keys, execute_command。",
                "如果任务已完成，action=finish 且 is_task_done=true。",
                "仅在确实需要系统命令时才使用 execute_command，且必须提供 action_args.command。",
            ],
        )
        return self._agent

    @property
    def model_id(self) -> str:
        return self._agent_config.vl_model_id or "gpt-4.1-mini"

    @staticmethod
    def _to_data_url(path: str) -> str:
        target = Path(path)
        if not target.exists():
            raise ValueError(f"截图文件不存在: {path}")
        binary = target.read_bytes()
        encoded = base64.b64encode(binary).decode("ascii")
        return f"data:image/png;base64,{encoded}"

    @staticmethod
    def _extract_json(text: str) -> str:
        fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
        if fenced:
            return fenced.group(1)
        direct = re.search(r"\{.*\}", text, re.DOTALL)
        if direct:
            return direct.group(0)
        raise ValueError("VL 输出不包含可解析 JSON。")

    @staticmethod
    def _normalize_decision(raw_text: str) -> SoloDecision:
        payload_text = SoloService._extract_json(raw_text)
        payload = json.loads(payload_text)
        decision = SoloDecision.model_validate(payload)
        if decision.action not in ALLOWED_ACTIONS:
            raise ValueError(f"不支持的动作: {decision.action}")
        return decision

    async def decide_next(
        self,
        task: str,
        screenshot_path: str,
        history: list[dict[str, Any]],
    ) -> SoloDecision:
        agent = self._build_agent()
        history_text = json.dumps(history[-8:], ensure_ascii=False)
        prompt = (
            f"用户任务: {task}\n\n"
            f"最近步骤历史: {history_text}\n\n"
            "请基于当前截图给出下一步动作。仅返回 JSON。"
        )
        result = await agent.arun(
            prompt,
            images=[Image(url=self._to_data_url(screenshot_path))],
        )
        content = getattr(result, "content", None)
        output_text = content if isinstance(content, str) and content.strip() else str(result)
        if not isinstance(output_text, str) or not output_text.strip():
            raise ValueError("VL 返回为空，无法继续 SOLO。")
        return self._normalize_decision(output_text)

    @staticmethod
    def is_dangerous_action(action: str, action_args: dict[str, Any]) -> tuple[bool, str]:
        if action == "execute_command":
            command = str(action_args.get("command", "")).strip()
            if not command:
                return True, "命令为空或未提供"
            return True, "将执行系统命令"
        if action != "press_keys":
            return False, ""
        keys = action_args.get("keys")
        if not isinstance(keys, list):
            return False, ""
        lowered = [str(item).lower() for item in keys]
        if any(item in lowered for item in ("ctrl", "alt", "meta", "win")):
            return True, "包含系统级组合键"
        if any(item in lowered for item in ("f4", "delete", "backspace", "enter")):
            return True, "可能触发不可逆提交或关闭/删除"
        return False, ""

    @staticmethod
    def to_error_decision(error: Exception) -> SoloDecision:
        message = str(error)
        return SoloDecision(
            thought_summary=f"SOLO 解析或推理失败: {message}",
            action="wait",
            action_args={"ms": 800},
            expected_outcome="等待用户处理后重试",
            is_task_done=False,
        )

    @staticmethod
    def parse_result(result: dict[str, Any] | None) -> dict[str, Any]:
        if not result:
            return {}
        return result

    @staticmethod
    def decision_dict(decision: SoloDecision) -> dict[str, Any]:
        return {
            "thought_summary": decision.thought_summary,
            "action": decision.action,
            "action_args": decision.action_args,
            "expected_outcome": decision.expected_outcome,
            "is_task_done": decision.is_task_done,
        }

    @staticmethod
    def validate_decision_payload(payload: dict[str, Any]) -> SoloDecision:
        try:
            return SoloDecision.model_validate(payload)
        except ValidationError as exc:
            raise ValueError(f"非法决策 payload: {exc}") from exc
