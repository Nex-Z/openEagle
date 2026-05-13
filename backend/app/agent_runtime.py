from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from agno.agent import Agent
from agno.models.openai import OpenAIResponses
from agno.models.openai.like import OpenAILike

from .agent_router import AgentRouter
from .attachments import AttachmentError, AttachmentStore, append_attachment_context
from .config import AgentConfig, AppConfig
from .confirmations import ToolConfirmationStore
from .models import AttachmentRef, StatusPayload, utc_now
from .prompts import build_direct_answer_instructions, build_direct_answer_prompt
from .providers.base import ReplyToolConfirmation, ReplyTrace
from .solo_worker_adapter import SoloWorkerAdapter
from .subagent_manager import SubAgentManager
from .subagent_models import AgentRouteDecision, WorkerReport


SendEvent = Callable[[str, str, str, dict[str, Any]], Awaitable[None]]
StartSolo = Callable[[str, str, str], Awaitable[str]]
SoloControl = Callable[[str, str, str], Awaitable[str]]
ConfigGetter = Callable[[], AppConfig]


class AgentRuntime:
    def __init__(
        self,
        *,
        config_getter: ConfigGetter,
        confirmation_store: ToolConfirmationStore,
        attachment_store: AttachmentStore | None = None,
        confirmed_tool_results: dict[str, str],
        send_event: SendEvent,
        start_solo: StartSolo,
        solo_control: SoloControl,
    ) -> None:
        self._config_getter = config_getter
        self._confirmation_store = confirmation_store
        self._attachment_store = attachment_store or AttachmentStore(Path(__file__).resolve().parents[2])
        self._confirmed_tool_results = confirmed_tool_results
        self._send_event = send_event
        self._solo_adapter = SoloWorkerAdapter(start_solo, solo_control)
        self._subagents = SubAgentManager()

    async def handle_user_message(
        self,
        conversation_id: str,
        request_id: str,
        content: str,
        attachments: list[AttachmentRef] | None = None,
        preferred_mode: str | None = None,
    ) -> str:
        try:
            prepared_attachments = self._attachment_store.prepare_user_attachments(
                conversation_id,
                attachments or [],
            )
        except AttachmentError as exc:
            reply = f"附件处理失败: {exc}"
            await self._send_event(
                "server:message",
                request_id,
                conversation_id,
                {"content": reply},
            )
            return reply

        if prepared_attachments:
            await self._send_event(
                "server:attachments_ready",
                request_id,
                conversation_id,
                {"attachments": self._attachment_store.public_dicts(prepared_attachments)},
            )

        enhanced_content = append_attachment_context(content, prepared_attachments)
        prev_result = self._confirmed_tool_results.pop(conversation_id, None)
        if prev_result:
            enhanced_content = f"{prev_result}\n\n用户新消息：{enhanced_content}"

        await self._send_status(request_id, conversation_id, "thinking", "main agent 正在调度任务")
        config = self._config_getter()
        router = AgentRouter(config)
        route_started_at = utc_now()
        await self._send_trace(
            request_id,
            conversation_id,
            {
                "id": f"{request_id}-main-router",
                "kind": "agent",
                "name": "main-router",
                "status": "started",
                "summary": "main agent 正在判断本轮请求的执行方式。",
                "params": {"preferredMode": preferred_mode or "auto"},
                "startedAt": route_started_at,
            },
        )
        decision = await router.route(
            conversation_id=conversation_id,
            content=enhanced_content,
            preferred_mode=preferred_mode,
            recent_tasks=self._subagents.recent_tasks(conversation_id),
        )
        if prepared_attachments and decision.route == "answer_directly":
            decision.route = "delegate_new"
            decision.worker_kind = "general"
            decision.task_brief = enhanced_content
            decision.task_title = decision.task_title or "处理附件"
            decision.user_visible_summary = "已转交 general worker 处理本轮附件。"
        await self._send_trace(
            request_id,
            conversation_id,
            {
                "id": f"{request_id}-main-router",
                "kind": "agent",
                "name": "main-router",
                "status": "completed",
                "summary": decision.user_visible_summary,
                "params": self._route_params(decision),
                "result": decision.model_dump_json(by_alias=True, exclude_none=True),
                "startedAt": route_started_at,
                "completedAt": utc_now(),
            },
        )

        try:
            if decision.route == "answer_directly":
                reply = await self._direct_answer(
                    conversation_id=conversation_id,
                    content=content,
                    config=config,
                )
            elif decision.route == "clarify":
                reply = decision.user_visible_summary or "我需要先确认一下你的具体目标。"
            elif decision.route == "start_solo":
                if prepared_attachments:
                    decision.task_brief = append_attachment_context(
                        decision.task_brief or decision.task_title,
                        prepared_attachments,
                    )
                reply = await self._handle_start_solo(conversation_id, request_id, decision)
            elif decision.route == "control_solo":
                action = self._solo_action_from_decision(decision)
                reply = await self.control_solo(conversation_id, request_id, action)
            else:
                reply = await self._delegate_to_worker(
                    conversation_id=conversation_id,
                    request_id=request_id,
                    decision=decision,
                    config=config,
                    attachments=prepared_attachments,
                )
        finally:
            await self._send_status(request_id, conversation_id, "idle", "回复完成")

        reply_attachments = self._attachment_store.peek_reply_attachments(
            conversation_id,
            request_id,
        )
        payload: dict[str, Any] = {"content": reply}
        if reply_attachments:
            payload["attachments"] = self._attachment_store.public_dicts(reply_attachments)
        await self._send_event("server:message", request_id, conversation_id, payload)
        if not conversation_id.startswith("im_"):
            self._attachment_store.pop_reply_attachments(conversation_id, request_id)
        return reply

    async def control_solo(
        self,
        conversation_id: str,
        request_id: str,
        action: str,
    ) -> str:
        if action == "pause":
            return await self._solo_adapter.pause(conversation_id, request_id)
        if action == "resume":
            return await self._solo_adapter.resume(conversation_id, request_id)
        if action == "stop":
            return await self._solo_adapter.stop(conversation_id, request_id)
        if action in {"confirm_allow", "confirm_reject"}:
            return await self._solo_adapter.confirm(
                conversation_id,
                request_id,
                "allow" if action == "confirm_allow" else "reject",
            )
        return f"不支持的桌面执行控制动作: {action}"

    async def handle_confirmation(
        self,
        conversation_id: str,
        request_id: str,
        decision: str,
    ) -> str:
        action = "confirm_allow" if decision == "allow" else "confirm_reject"
        return await self.control_solo(conversation_id, request_id, action)

    async def _handle_start_solo(
        self,
        conversation_id: str,
        request_id: str,
        decision: AgentRouteDecision,
    ) -> str:
        started_at = utc_now()
        await self._send_trace(
            request_id,
            conversation_id,
            {
                "id": f"{request_id}-solo-worker",
                "kind": "agent",
                "name": "solo-worker",
                "status": "started",
                "summary": "main agent 已将任务交给桌面执行 worker。",
                "params": {"workerKind": "solo", "route": decision.route},
                "startedAt": started_at,
            },
        )
        reply = await self._solo_adapter.start(
            conversation_id,
            request_id,
            decision.task_brief or decision.task_title,
        )
        await self._send_trace(
            request_id,
            conversation_id,
            {
                "id": f"{request_id}-solo-worker",
                "kind": "agent",
                "name": "solo-worker",
                "status": "completed",
                "summary": "桌面执行 worker 已接收任务。",
                "params": {"workerKind": "solo", "route": decision.route},
                "result": reply,
                "startedAt": started_at,
                "completedAt": utc_now(),
            },
        )
        return reply

    async def _delegate_to_worker(
        self,
        *,
        conversation_id: str,
        request_id: str,
        decision: AgentRouteDecision,
        config: AppConfig,
        attachments: list[AttachmentRef],
    ) -> str:
        task = self._subagents.create_or_reuse(conversation_id, decision)
        started_at = utc_now()
        await self._send_trace(
            request_id,
            conversation_id,
            {
                "id": f"{request_id}-{task.worker_id}",
                "kind": "agent",
                "name": f"{task.worker_kind}-worker",
                "status": "started",
                "summary": f"{task.worker_kind} worker 开始处理：{task.title}",
                "params": task.to_trace_params(),
                "startedAt": started_at,
            },
        )

        report: WorkerReport | None = None
        async for event in self._subagents.run_worker(
            task=task,
            config=config,
            confirmation_store=self._confirmation_store,
            request_id=request_id,
            attachment_store=self._attachment_store,
            attachments=attachments,
        ):
            if isinstance(event, ReplyTrace):
                await self._send_reply_trace(request_id, conversation_id, event)
                continue
            if isinstance(event, ReplyToolConfirmation):
                pending = self._confirmation_store.get(event.confirmation_id)
                if pending:
                    await self._send_event(
                        "server:tool_confirmation_required",
                        request_id,
                        conversation_id,
                        {"confirmation": pending.to_payload()},
                    )
                continue
            if isinstance(event, WorkerReport):
                report = event

        if report is None:
            report = WorkerReport(
                worker_id=task.worker_id,
                worker_kind=task.worker_kind,
                state="failed",
                title=task.title,
                summary="worker 没有返回结果。",
                result="",
                error="missing worker report",
                started_at=started_at,
                completed_at=utc_now(),
            )

        await self._send_trace(
            request_id,
            conversation_id,
            {
                "id": f"{request_id}-{task.worker_id}",
                "kind": "agent",
                "name": f"{task.worker_kind}-worker",
                "status": "completed" if report.state == "completed" else "error",
                "summary": report.summary,
                "params": task.to_trace_params(),
                "result": report.to_trace_result(),
                "startedAt": started_at,
                "completedAt": report.completed_at or utc_now(),
            },
        )

        if report.state == "completed":
            return report.result or report.summary
        return f"{task.worker_kind} worker 执行失败：{report.error or report.summary}"

    async def _send_reply_trace(
        self,
        request_id: str,
        conversation_id: str,
        event: ReplyTrace,
    ) -> None:
        await self._send_trace(
            request_id,
            conversation_id,
            {
                "id": event.trace_id,
                "kind": event.kind,
                "name": event.name,
                "status": event.status,
                "summary": event.summary,
                "params": event.params,
                "result": event.result,
                "startedAt": event.started_at,
                "completedAt": event.completed_at,
            },
        )

    async def _send_status(
        self,
        request_id: str,
        conversation_id: str,
        stage: str,
        detail: str,
    ) -> None:
        await self._send_event(
            "server:status",
            request_id,
            conversation_id,
            StatusPayload(stage=stage, detail=detail).model_dump(),
        )

    async def _send_trace(
        self,
        request_id: str,
        conversation_id: str,
        trace: dict[str, Any],
    ) -> None:
        await self._send_event("server:trace", request_id, conversation_id, {"trace": trace})

    @staticmethod
    def _route_params(decision: AgentRouteDecision) -> dict[str, Any]:
        return {
            "route": decision.route,
            "workerKind": decision.worker_kind,
            "targetWorkerId": decision.target_worker_id,
            "requiresWrite": decision.requires_write,
            "requiresGui": decision.requires_gui,
        }

    @staticmethod
    def _solo_action_from_decision(decision: AgentRouteDecision) -> str:
        text = (decision.task_brief or decision.task_title).strip().lower()
        if "resume" in text or "恢复" in text:
            return "resume"
        if "stop" in text or "停止" in text or "结束" in text:
            return "stop"
        return "pause"

    async def _direct_answer(
        self,
        *,
        conversation_id: str,
        content: str,
        config: AppConfig,
    ) -> str:
        if self._can_use_direct_answer_model(config.agent):
            try:
                return await self._direct_answer_with_model(conversation_id, content, config)
            except Exception:
                pass
        return self._fallback_direct_answer(content)

    @staticmethod
    def _can_use_direct_answer_model(agent_config: AgentConfig) -> bool:
        return agent_config.provider in {"openai", "openai-like"} and bool(agent_config.api_key)

    async def _direct_answer_with_model(
        self,
        conversation_id: str,
        content: str,
        config: AppConfig,
    ) -> str:
        agent_config = config.agent
        model_id = agent_config.model_id or "gpt-5-mini"
        if agent_config.provider == "openai-like":
            if not agent_config.base_url:
                raise ValueError("openai-like 模式需要配置 Base URL。")
            model = OpenAILike(
                id=model_id,
                api_key=agent_config.api_key,
                base_url=agent_config.base_url,
            )
        else:
            model = OpenAIResponses(
                id=model_id,
                api_key=agent_config.api_key,
            )

        agent = Agent(
            model=model,
            markdown=False,
            instructions=build_direct_answer_instructions(),
        )
        result = await agent.arun(build_direct_answer_prompt(content))
        answer = getattr(result, "content", None)
        if isinstance(answer, str) and answer.strip():
            return answer.strip()
        return str(result).strip()

    @staticmethod
    def _fallback_direct_answer(content: str) -> str:
        stripped = content.strip()
        if stripped in {"你好", "hi", "hello"}:
            return "我在。你可以直接和我聊，也可以告诉我想处理什么。"
        if stripped in {"你是谁", "who are you"}:
            return "我是 openEagle 的 main agent，可以直接和你对话，也可以在需要时调度 worker 帮你处理任务。"
        return "我明白。你可以继续说，我会按你的意图判断是直接聊，还是交给合适的执行者处理。"
