from __future__ import annotations

import json
import re
from typing import Any

from .config import AppConfig
from .langgraph_agent import run_text_model
from .prompts import build_main_router_instructions, build_main_router_prompt
from .subagent_models import AgentRouteDecision, AgentTaskRecord
from .token_usage import record_model_usage


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
COMMAND_KEYWORDS = (
    "运行",
    "执行命令",
    "命令",
    "powershell",
    "python -c",
    "node -e",
    "run ",
    "command",
    "shell",
)
DIRECT_NO_TOOL_MARKERS = (
    "不用工具",
    "不要调用工具",
    "直接回答",
    "no tool",
    "without tools",
)
NEGATIVE_CONSTRAINT_PATTERNS: tuple[tuple[str, str], ...] = (
    ("不要删除", "不要删除文件或目录。"),
    ("别删除", "不要删除文件或目录。"),
    ("不要删", "不要删除文件或目录。"),
    ("不要改动", "不要修改现有内容。"),
    ("不要修改", "不要修改现有内容。"),
    ("不要写入", "不要写入文件。"),
    ("只分析", "只分析说明，不执行会改变状态的操作。"),
    ("只看", "只查看说明，不执行会改变状态的操作。"),
    ("只读", "只读处理，不执行写入、移动或删除。"),
    ("不要执行", "不要执行命令或动作。"),
    ("先判断风险", "先判断风险；如有破坏性风险则停止执行。"),
    ("如果会破坏", "如果会造成破坏则停止执行。"),
    ("如果不安全", "如果不安全则停止执行。"),
    ("危险就停止", "如果危险则停止执行。"),
    ("do not delete", "Do not delete files or directories."),
    ("don't delete", "Do not delete files or directories."),
    ("do not modify", "Do not modify existing content."),
    ("do not execute", "Do not execute commands or actions."),
    ("read only", "Read-only handling; do not write, move, or delete."),
)
FORBIDDEN_ACTION_PATTERNS: tuple[tuple[str, str], ...] = (
    ("不要删除", "delete_path"),
    ("别删除", "delete_path"),
    ("不要删", "delete_path"),
    ("do not delete", "delete_path"),
    ("don't delete", "delete_path"),
    ("不要改动", "mutation_tools"),
    ("不要修改", "mutation_tools"),
    ("不要写入", "write_text_file"),
    ("只分析", "mutation_tools"),
    ("只看", "mutation_tools"),
    ("只读", "mutation_tools"),
    ("不要执行", "run_command"),
    ("do not modify", "mutation_tools"),
    ("do not execute", "run_command"),
    ("read only", "mutation_tools"),
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
        memory_context: str | None = None,
        conversation_context: str | None = None,
    ) -> AgentRouteDecision:
        recent_tasks = recent_tasks or []
        if self._can_use_model_router():
            try:
                raw = await self._route_with_model(
                    conversation_id=conversation_id,
                    content=content,
                    preferred_mode=preferred_mode,
                    recent_tasks=recent_tasks,
                    memory_context=memory_context,
                    conversation_context=conversation_context,
                )
                return self.parse(raw, content, preferred_mode, recent_tasks)
            except Exception:
                return self.heuristic(content, preferred_mode, recent_tasks)
        return self.heuristic(content, preferred_mode, recent_tasks)

    def _can_use_model_router(self) -> bool:
        return self._config.agent.provider in {"openai", "openai-like", "anthropic"} and bool(
            self._config.agent.api_key
        )

    async def _route_with_model(
        self,
        conversation_id: str,
        content: str,
        preferred_mode: str | None,
        recent_tasks: list[AgentTaskRecord],
        memory_context: str | None = None,
        conversation_context: str | None = None,
    ) -> str:
        agent_config = self._config.agent
        prompt = build_main_router_prompt(
            conversation_id=conversation_id,
            content=content,
            preferred_mode=preferred_mode,
            recent_tasks=recent_tasks,
            memory_context=memory_context,
            conversation_context=conversation_context,
        )

        if agent_config.provider == "anthropic":
            import anthropic

            client = anthropic.AsyncAnthropic(api_key=agent_config.api_key)
            response = await client.messages.create(
                model=agent_config.model_id or "claude-sonnet-4-20250514",
                max_tokens=1024,
                system="\n".join(build_main_router_instructions()),
                messages=[{"role": "user", "content": prompt}],
            )
            await record_model_usage(
                "anthropic",
                agent_config.model_id or "claude-sonnet-4-20250514",
                getattr(response, "usage", None),
            )
            text_parts = [block.text for block in response.content if block.type == "text"]
            return "".join(text_parts)

        return await run_text_model(
            agent_config=agent_config,
            instructions=build_main_router_instructions(),
            prompt=prompt,
        )

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
        if not decision.user_visible_summary and decision.route != "answer_directly":
            decision.user_visible_summary = cls._summary_for_route(decision)
        cls._merge_constraint_hints(decision, content)
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
        if any(keyword in lowered or keyword in text for keyword in COMMAND_KEYWORDS):
            return cls._decision("delegate_new", "coding", text, requires_write=requires_write)
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
        if any(marker in compact for marker in DIRECT_NO_TOOL_MARKERS):
            blocked_markers = (
                "文件",
                "目录",
                "代码",
                "项目",
                "搜索",
                "查询",
                "读取",
                "修改",
                "写入",
                "创建",
                "删除",
                "运行",
                "命令",
                "run ",
                "python -c",
            )
            return not any(marker in compact for marker in blocked_markers)
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
        cls._merge_constraint_hints(decision, content)
        decision.user_visible_summary = cls._summary_for_route(decision)
        return decision

    @classmethod
    def _merge_constraint_hints(cls, decision: AgentRouteDecision, content: str) -> None:
        negative_constraints = list(decision.negative_constraints)
        for item in cls._extract_negative_constraints(content):
            if item not in negative_constraints:
                negative_constraints.append(item)
        decision.negative_constraints = negative_constraints

        forbidden_actions = list(decision.forbidden_actions)
        for item in cls._extract_forbidden_actions(content):
            if item not in forbidden_actions:
                forbidden_actions.append(item)
        decision.forbidden_actions = forbidden_actions

        if negative_constraints:
            constraint_text = "硬性约束：" + "；".join(negative_constraints)
            if constraint_text not in decision.success_criteria:
                decision.success_criteria = [*decision.success_criteria, constraint_text]
            if constraint_text not in decision.context_summary:
                decision.context_summary = (
                    f"{decision.context_summary.strip()}\n{constraint_text}".strip()
                )

    @staticmethod
    def _extract_negative_constraints(content: str) -> list[str]:
        lowered = content.lower()
        constraints: list[str] = []
        for marker, constraint in NEGATIVE_CONSTRAINT_PATTERNS:
            if marker.lower() in lowered and constraint not in constraints:
                constraints.append(constraint)
        return constraints

    @staticmethod
    def _extract_forbidden_actions(content: str) -> list[str]:
        lowered = content.lower()
        actions: list[str] = []
        for marker, action in FORBIDDEN_ACTION_PATTERNS:
            if marker.lower() in lowered and action not in actions:
                actions.append(action)
        return actions

    @staticmethod
    def _title_from_content(content: str) -> str:
        title = " ".join(content.strip().split())
        if not title:
            return "用户请求"
        return title[:40]

    @staticmethod
    def _summary_for_route(decision: AgentRouteDecision) -> str:
        if decision.route == "answer_directly":
            return ""
        if decision.route == "start_solo":
            return "我来处理，一会儿给你结果。"
        if decision.route == "control_solo":
            return "我来调整桌面执行状态。"
        if decision.route == "delegate_existing":
            return "我接着处理刚才那件事。"
        if decision.route == "clarify":
            return "我需要先确认一个关键信息。"
        return "我来处理，一会儿给你结果。"
