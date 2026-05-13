from __future__ import annotations

import json
import re
from typing import Any

from agno.agent import Agent
from agno.models.openai import OpenAIResponses
from agno.models.openai.like import OpenAILike

from .config import AppConfig
from .prompts import build_main_router_instructions, build_main_router_prompt
from .subagent_models import AgentRouteDecision, AgentTaskRecord


GUI_KEYWORDS = (
    "打开",
    "点击",
    "输入",
    "填写",
    "浏览器",
    "网页",
    "桌面",
    "窗口",
    "登录",
    "启动",
    "open",
    "click",
    "type",
    "browser",
    "desktop",
    "window",
)
CODING_KEYWORDS = (
    "代码",
    "实现",
    "修改",
    "修复",
    "测试",
    "构建",
    "提交",
    "readme",
    "docs",
    "implement",
    "fix",
    "test",
    "build",
    "commit",
)
RESEARCH_KEYWORDS = (
    "查询",
    "搜索",
    "资料",
    "新闻",
    "最新",
    "价格",
    "天气",
    "search",
    "research",
    "latest",
    "news",
)
WRITE_KEYWORDS = (
    "修改",
    "写入",
    "创建",
    "删除",
    "安装",
    "提交",
    "implement",
    "change",
    "write",
    "delete",
    "install",
    "commit",
)


class AgentRouter:
    def __init__(self, config: AppConfig) -> None:
        self._config = config

    async def route(
        self,
        conversation_id: str,
        content: str,
        preferred_mode: str | None = None,
        recent_tasks: list[AgentTaskRecord] | None = None,
    ) -> AgentRouteDecision:
        recent_tasks = recent_tasks or []
        if self._can_use_model_router():
            try:
                raw = await self._route_with_model(
                    conversation_id=conversation_id,
                    content=content,
                    preferred_mode=preferred_mode,
                    recent_tasks=recent_tasks,
                )
                return self.parse(raw, content, preferred_mode, recent_tasks)
            except Exception:
                return self.heuristic(content, preferred_mode, recent_tasks)
        return self.heuristic(content, preferred_mode, recent_tasks)

    def _can_use_model_router(self) -> bool:
        return self._config.agent.provider in {"openai", "openai-like"} and bool(
            self._config.agent.api_key
        )

    async def _route_with_model(
        self,
        conversation_id: str,
        content: str,
        preferred_mode: str | None,
        recent_tasks: list[AgentTaskRecord],
    ) -> str:
        agent_config = self._config.agent
        if agent_config.provider == "openai-like":
            if not agent_config.base_url:
                raise ValueError("openai-like 模式需要配置 Base URL。")
            model = OpenAILike(
                id=agent_config.model_id or "gpt-5-mini",
                api_key=agent_config.api_key,
                base_url=agent_config.base_url,
            )
        else:
            model = OpenAIResponses(
                id=agent_config.model_id or "gpt-5-mini",
                api_key=agent_config.api_key,
            )

        agent = Agent(
            model=model,
            markdown=False,
            instructions=build_main_router_instructions(),
        )
        result = await agent.arun(
            build_main_router_prompt(
                conversation_id=conversation_id,
                content=content,
                preferred_mode=preferred_mode,
                recent_tasks=recent_tasks,
            )
        )
        raw = getattr(result, "content", None)
        return raw if isinstance(raw, str) else str(result)

    @classmethod
    def parse(
        cls,
        raw_text: str,
        content: str,
        preferred_mode: str | None = None,
        recent_tasks: list[AgentTaskRecord] | None = None,
    ) -> AgentRouteDecision:
        try:
            payload = json.loads(cls._extract_json(raw_text))
            decision = AgentRouteDecision.model_validate(payload)
        except Exception:
            return cls.heuristic(content, preferred_mode, recent_tasks or [])

        if not decision.task_brief:
            decision.task_brief = content.strip()
        if not decision.task_title:
            decision.task_title = cls._title_from_content(content)
        if not decision.user_visible_summary:
            decision.user_visible_summary = cls._summary_for_route(decision)
        return decision

    @staticmethod
    def _extract_json(text: str) -> str:
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
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
        raise ValueError("router output does not contain JSON")

    @classmethod
    def heuristic(
        cls,
        content: str,
        preferred_mode: str | None = None,
        recent_tasks: list[AgentTaskRecord] | None = None,
    ) -> AgentRouteDecision:
        text = content.strip()
        lowered = text.lower()
        recent_tasks = recent_tasks or []

        control = cls._solo_control_route(lowered)
        if control:
            return control
        if preferred_mode == "solo":
            if cls._looks_like_direct_answer(lowered):
                return cls._decision("answer_directly", "general", text)
            return cls._decision("start_solo", "solo", text, requires_gui=True)
        if any(keyword in lowered or keyword in text for keyword in GUI_KEYWORDS):
            return cls._decision("start_solo", "solo", text, requires_gui=True)
        return cls._delegate_or_direct(text, lowered, recent_tasks)

    @classmethod
    def _delegate_or_direct(
        cls,
        text: str,
        lowered: str,
        recent_tasks: list[AgentTaskRecord],
    ) -> AgentRouteDecision:
        if cls._looks_like_followup(lowered) and recent_tasks:
            reusable = next(
                (
                    task
                    for task in reversed(recent_tasks)
                    if task.worker_kind != "solo" and task.state in {"completed", "idle"}
                ),
                None,
            )
            if reusable:
                return cls._decision(
                    "delegate_existing",
                    reusable.worker_kind,
                    text,
                    target_worker_id=reusable.worker_id,
                    requires_write=reusable.requires_write,
                )

        if cls._looks_like_direct_answer(lowered):
            return cls._decision("answer_directly", "general", text)

        requires_write = any(keyword in lowered or keyword in text for keyword in WRITE_KEYWORDS)
        if any(keyword in lowered or keyword in text for keyword in CODING_KEYWORDS):
            return cls._decision("delegate_new", "coding", text, requires_write=requires_write)
        if any(keyword in lowered or keyword in text for keyword in RESEARCH_KEYWORDS):
            return cls._decision("delegate_new", "research", text)
        return cls._decision("delegate_new", "general", text, requires_write=requires_write)

    @staticmethod
    def _looks_like_direct_answer(lowered: str) -> bool:
        compact = lowered.strip()
        if compact in {"hi", "hello", "你好", "在吗", "你是谁"}:
            return True
        return len(compact) <= 24 and compact.endswith(("?", "？")) and not any(
            marker in compact
            for marker in ("项目", "文件", "代码", "搜索", "查询", "打开", "修改")
        )

    @staticmethod
    def _looks_like_followup(lowered: str) -> bool:
        return any(
            keyword in lowered
            for keyword in (
                "刚才",
                "继续",
                "上一",
                "这个",
                "那个",
                "再",
                "same",
                "continue",
                "previous",
            )
        )

    @classmethod
    def _solo_control_route(cls, lowered: str) -> AgentRouteDecision | None:
        if lowered in {"/pause", "pause", "暂停"}:
            return cls._decision("control_solo", "solo", "pause", requires_gui=True)
        if lowered in {"/resume", "resume", "恢复"}:
            return cls._decision("control_solo", "solo", "resume", requires_gui=True)
        if lowered in {"/stop", "stop", "停止", "结束"}:
            return cls._decision("control_solo", "solo", "stop", requires_gui=True)
        return None

    @classmethod
    def _decision(
        cls,
        route: str,
        worker_kind: str,
        content: str,
        *,
        target_worker_id: str | None = None,
        requires_write: bool = False,
        requires_gui: bool = False,
    ) -> AgentRouteDecision:
        decision = AgentRouteDecision(
            route=route,
            worker_kind=worker_kind,
            target_worker_id=target_worker_id,
            task_title=cls._title_from_content(content),
            task_brief=content.strip(),
            success_criteria=["完成用户本轮请求，并给出简洁结果。"],
            requires_write=requires_write,
            requires_gui=requires_gui,
            user_visible_summary="",
            context_summary="",
        )
        decision.user_visible_summary = cls._summary_for_route(decision)
        return decision

    @staticmethod
    def _title_from_content(content: str) -> str:
        title = " ".join(content.strip().split())
        if not title:
            return "用户请求"
        return title[:40]

    @staticmethod
    def _summary_for_route(decision: AgentRouteDecision) -> str:
        if decision.route == "answer_directly":
            return "main agent 将直接回复。"
        if decision.route == "start_solo":
            return "main agent 将启动桌面执行。"
        if decision.route == "control_solo":
            return "main agent 将转发桌面执行控制动作。"
        if decision.route == "delegate_existing":
            return f"main agent 将复用 {decision.worker_kind} worker 继续处理。"
        if decision.route == "clarify":
            return "main agent 需要先澄清任务。"
        return f"main agent 将交给 {decision.worker_kind} worker 处理。"
