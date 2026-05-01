from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from .agent_router import AgentRouter
from .config import AppConfig
from .confirmations import ToolConfirmationStore
from .models import StatusPayload, utc_now
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
        confirmed_tool_results: dict[str, str],
        send_event: SendEvent,
        start_solo: StartSolo,
        solo_control: SoloControl,
    ) -> None:
        self._config_getter = config_getter
        self._confirmation_store = confirmation_store
        self._confirmed_tool_results = confirmed_tool_results
        self._send_event = send_event
        self._solo_adapter = SoloWorkerAdapter(start_solo, solo_control)
        self._subagents = SubAgentManager()

    async def handle_user_message(
        self,
        conversation_id: str,
        request_id: str,
        content: str,
        preferred_mode: str | None = None,
    ) -> str:
        enhanced_content = content
        prev_result = self._confirmed_tool_results.pop(conversation_id, None)
        if prev_result:
            enhanced_content = f"{prev_result}\n\n用户新消息：{content}"

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
                reply = self._direct_answer(content)
            elif decision.route == "clarify":
                reply = decision.user_visible_summary or "我需要先确认一下你的具体目标。"
            elif decision.route == "start_solo":
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
                )
        finally:
            await self._send_status(request_id, conversation_id, "idle", "回复完成")

        await self._send_event("server:message", request_id, conversation_id, {"content": reply})
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
        return f"不支持的 SOLO 控制动作: {action}"

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
                "summary": "main agent 已将任务交给 SOLO 视觉 worker。",
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
                "summary": "SOLO 视觉 worker 已接收任务。",
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

    @staticmethod
    def _direct_answer(content: str) -> str:
        stripped = content.strip()
        if stripped in {"你好", "hi", "hello"}:
            return "你好，我在。"
        if stripped in {"你是谁", "who are you"}:
            return "我是 openEagle 的 main agent，负责理解你的请求并调度合适的 worker 或 SOLO 来完成。"
        return "收到。这类简单问题我可以直接处理；如果需要执行任务，我会交给合适的 worker。"
