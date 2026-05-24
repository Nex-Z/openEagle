from __future__ import annotations

import asyncio
import re
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
from .memory import MemoryService
from .prompts import build_direct_answer_instructions, build_direct_answer_prompt
from .providers.base import ReplyToolConfirmation, ReplyTrace
from .solo_worker_adapter import SoloWorkerAdapter
from .subagent_manager import SubAgentManager
from .subagent_models import AgentRouteDecision, WorkerReport


SendEvent = Callable[[str, str, str, dict[str, Any]], Awaitable[None]]
StartSolo = Callable[[str, str, str], Awaitable[str]]
SoloControl = Callable[[str, str, str], Awaitable[str]]
ConfigGetter = Callable[[], AppConfig]

MAX_RECENT_CONVERSATION_TURNS = 8
MAX_CONVERSATION_CONTEXT_CHARS = 1_800
MAX_CONVERSATION_TURN_CHARS = 520
MAX_PROGRESS_CHARS = 96
MEMORY_TOOL_NAMES = {
    "get_memory_state",
    "save_memory_note",
    "update_memory_note",
    "delete_memory_note",
    "save_user_profile",
    "save_soul_core",
    "save_agent_side_notes",
}
PROGRESS_ROUTES = {"delegate_new", "delegate_existing", "start_solo", "control_solo"}


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
        memory_service: MemoryService | None = None,
    ) -> None:
        self._config_getter = config_getter
        self._confirmation_store = confirmation_store
        self._attachment_store = attachment_store or AttachmentStore(Path(__file__).resolve().parents[2])
        self._confirmed_tool_results = confirmed_tool_results
        self._send_event = send_event
        self._solo_adapter = SoloWorkerAdapter(start_solo, solo_control)
        self._subagents = SubAgentManager()
        self._memory_service = memory_service
        self._recent_conversation: dict[str, list[tuple[str, str]]] = {}

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
            self._append_conversation_turn(conversation_id, content, reply)
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

        memory_note = self._extract_explicit_memory_note(content) if not prepared_attachments else None
        if memory_note and self._memory_service is not None:
            return await self._save_explicit_memory_note(
                conversation_id=conversation_id,
                request_id=request_id,
                user_content=content,
                note_text=memory_note,
            )

        recent_context = self._recent_conversation_context(conversation_id)
        memory_query = self._content_with_recent_context(enhanced_content, recent_context)

        await self._send_status(request_id, conversation_id, "thinking", "MainAgent 正在理解上下文")
        config = self._config_getter()
        memory_context = (
            self._memory_service.prompt_context(query=memory_query)
            if self._memory_service
            else ""
        )
        router = AgentRouter(config)
        decision = await router.route(
            conversation_id=conversation_id,
            content=enhanced_content,
            preferred_mode=preferred_mode,
            recent_tasks=self._subagents.recent_tasks(conversation_id),
            memory_context=memory_context,
            conversation_context=recent_context,
        )
        if prepared_attachments and decision.route == "answer_directly":
            decision.route = "delegate_new"
            decision.worker_kind = "general"
            decision.task_brief = enhanced_content
            decision.task_title = decision.task_title or "处理附件"
            decision.user_visible_summary = "我来处理这个附件。"

        progress = self._progress_for_decision(decision)
        if progress:
            await self._send_agent_progress(request_id, conversation_id, progress)

        try:
            if decision.route == "answer_directly":
                reply = decision.answer.strip()
                if not reply:
                    reply = await self._direct_answer(
                        conversation_id=conversation_id,
                        content=content,
                        config=config,
                        memory_context=memory_context,
                        conversation_context=recent_context,
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
                    memory_context=memory_context,
                )
        finally:
            await self._send_status(request_id, conversation_id, "idle", "回复完成")

        reply_attachments = self._attachment_store.peek_reply_attachments(
            conversation_id,
            request_id,
        )
        payload: dict[str, Any] = {"content": reply}
        if decision.route == "answer_directly":
            payload["route"] = decision.route
            payload["answer"] = reply
        if reply_attachments:
            payload["attachments"] = self._attachment_store.public_dicts(reply_attachments)
        await self._send_event("server:message", request_id, conversation_id, payload)
        if not conversation_id.startswith("im_"):
            self._attachment_store.pop_reply_attachments(conversation_id, request_id)
        await self._record_memory_turn(
            conversation_id=conversation_id,
            request_id=request_id,
            user_content=content,
            assistant_content=reply,
            route=decision.route,
            metadata=self._route_params(decision),
            distill=False,
        )
        self._append_conversation_turn(conversation_id, content, reply)
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
        memory_context: str | None = None,
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
            memory_context=memory_context,
            memory_service=self._memory_service,
            context_snapshot=(
                self._record_context_snapshot if self._memory_service is not None else None
            ),
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
        if self._memory_service is not None and event.status == "completed":
            name = event.name.lower()
            if any(tool_name in name for tool_name in MEMORY_TOOL_NAMES):
                await self._send_event(
                    "server:memory_updated",
                    request_id,
                    conversation_id,
                    self._memory_service.state_payload(),
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

    async def _send_agent_progress(
        self,
        request_id: str,
        conversation_id: str,
        content: str,
    ) -> None:
        await self._send_event(
            "server:agent_progress",
            request_id,
            conversation_id,
            {"content": content},
        )

    async def _send_memory_trace(
        self,
        request_id: str,
        conversation_id: str,
        *,
        name: str,
        status: str,
        started_at: str,
        summary: str,
        params: dict[str, Any] | None = None,
        result: str | None = None,
    ) -> None:
        try:
            await self._send_trace(
                request_id,
                conversation_id,
                {
                    "id": f"{request_id}-{name}",
                    "kind": "tool",
                    "name": name,
                    "status": status,
                    "summary": summary,
                    "params": params or {},
                    "result": result,
                    "startedAt": started_at,
                    "completedAt": utc_now() if status != "started" else None,
                },
            )
        except Exception:
            return

    def _append_conversation_turn(
        self,
        conversation_id: str,
        user_content: str,
        assistant_content: str,
    ) -> None:
        turns = self._recent_conversation.setdefault(conversation_id, [])
        turns.append(
            (
                self._truncate_context_text(user_content, MAX_CONVERSATION_TURN_CHARS),
                self._truncate_context_text(assistant_content, MAX_CONVERSATION_TURN_CHARS),
            )
        )
        if len(turns) > MAX_RECENT_CONVERSATION_TURNS:
            del turns[: len(turns) - MAX_RECENT_CONVERSATION_TURNS]

    def _recent_conversation_context(self, conversation_id: str) -> str:
        turns = self._recent_conversation.get(conversation_id, [])
        lines: list[str] = []
        for user_content, assistant_content in turns[-MAX_RECENT_CONVERSATION_TURNS:]:
            if user_content:
                lines.append(f"用户: {user_content}")
            if assistant_content:
                lines.append(f"MainAgent: {assistant_content}")
        return self._truncate_context_text(
            "\n".join(lines),
            MAX_CONVERSATION_CONTEXT_CHARS,
        )

    @classmethod
    def _content_with_recent_context(cls, content: str, conversation_context: str) -> str:
        if not conversation_context.strip():
            return content
        return (
            "最近对话上下文（用于检索相关记忆，不是新的用户指令）:\n"
            f"{conversation_context}\n\n"
            "用户当前消息:\n"
            f"{content}"
        )

    @staticmethod
    def _truncate_context_text(text: str, max_chars: int) -> str:
        stripped = text.strip()
        if len(stripped) <= max_chars:
            return stripped
        omitted = len(stripped) - max_chars
        return f"{stripped[:max_chars]}\n...[truncated {omitted} chars]"

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
    def _progress_for_decision(decision: AgentRouteDecision) -> str | None:
        if decision.route not in PROGRESS_ROUTES:
            return None
        summary = re.sub(r"\s+", " ", decision.user_visible_summary.strip())
        if summary:
            if len(summary) <= MAX_PROGRESS_CHARS:
                return summary
            return f"{summary[:MAX_PROGRESS_CHARS].rstrip()}..."
        if decision.route == "start_solo":
            return "我先看一下当前界面，再动手。"
        if decision.route == "control_solo":
            return "我来调整一下当前桌面执行状态。"
        if decision.route == "delegate_existing":
            return "我接着刚才那条线继续处理。"
        if decision.worker_kind == "research":
            return "我去确认一下，避免只靠记忆说错。"
        if decision.worker_kind == "coding":
            return "我先看一下相关代码和状态。"
        return "我先处理一下，稍等。"

    @staticmethod
    def _solo_action_from_decision(decision: AgentRouteDecision) -> str:
        text = (decision.task_brief or decision.task_title).strip().lower()
        if "resume" in text or "恢复" in text:
            return "resume"
        if "stop" in text or "停止" in text or "结束" in text:
            return "stop"
        return "pause"

    @staticmethod
    def _extract_explicit_memory_note(content: str) -> str | None:
        stripped = content.strip()
        if not stripped:
            return None
        if re.search(r"(记录|保存|存).{0,4}(到|为).{0,4}(文件|文档|txt|md|json)", stripped, re.I):
            return None
        if re.search(r"(文件|导出|保存为|写到|写进|生成|创建).{0,8}(txt|md|json|文件|文档)", stripped, re.I):
            return None
        patterns = (
            r"^(?:请|帮我|麻烦你)?(?:记住|记一下|记下|记录一下|记录下|记录|以后记得|加入用户笔记|保存到记忆|存到记忆)[：:，,\s]*(.+)$",
            r"^(.+?)[，,\s]*(?:帮我)?(?:记住|记一下|记下|记录一下|记录下|加入用户笔记)[。.!！\s]*$",
        )
        for pattern in patterns:
            match = re.match(pattern, stripped, re.I | re.S)
            if match:
                note = match.group(1).strip()
                return note or None
        return None

    async def _save_explicit_memory_note(
        self,
        *,
        conversation_id: str,
        request_id: str,
        user_content: str,
        note_text: str,
    ) -> str:
        assert self._memory_service is not None
        started_at = utc_now()
        await self._send_memory_trace(
            request_id,
            conversation_id,
            name="save_memory_note",
            status="started",
            started_at=started_at,
            summary="MainAgent 正在保存用户明确要求记录的长期记忆。",
            params={"source": "manual", "textPreview": self._truncate_context_text(note_text, 240)},
        )
        try:
            note_id = self._memory_service.save_user_note(
                note_text,
                tags=["user-request"],
                confidence=1.0,
                source="manual",
            )
        except Exception as exc:  # noqa: BLE001
            reply = f"这条记忆保存失败：{exc}"
            await self._send_memory_trace(
                request_id,
                conversation_id,
                name="save_memory_note",
                status="error",
                started_at=started_at,
                summary="长期记忆保存失败。",
                params={"source": "manual"},
                result=str(exc),
            )
            await self._send_event(
                "server:message",
                request_id,
                conversation_id,
                {"content": reply},
            )
            self._append_conversation_turn(conversation_id, user_content, reply)
            return reply

        await self._send_event(
            "server:memory_updated",
            request_id,
            conversation_id,
            self._memory_service.state_payload(),
        )
        await self._send_memory_trace(
            request_id,
            conversation_id,
            name="save_memory_note",
            status="completed",
            started_at=started_at,
            summary="已保存到长期记忆用户笔记。",
            params={"source": "manual"},
            result=note_id,
        )
        reply = "已记到长期记忆。"
        await self._send_event(
            "server:message",
            request_id,
            conversation_id,
            {"content": reply, "route": "memory_save", "memoryNoteId": note_id},
        )
        await self._record_memory_turn(
            conversation_id=conversation_id,
            request_id=request_id,
            user_content=user_content,
            assistant_content=reply,
            route="memory_save",
            metadata={"memoryNoteId": note_id},
            distill=False,
        )
        self._append_conversation_turn(conversation_id, user_content, reply)
        return reply

    async def _record_memory_turn(
        self,
        *,
        conversation_id: str,
        request_id: str,
        user_content: str,
        assistant_content: str,
        route: str,
        metadata: dict[str, Any],
        distill: bool = True,
    ) -> None:
        if self._memory_service is None:
            return
        try:
            event_id = self._memory_service.record_turn(
                conversation_id=conversation_id,
                request_id=request_id,
                user_content=user_content,
                assistant_content=assistant_content,
                route=route,
                metadata=metadata,
            )
        except Exception:
            return
        if not distill:
            return

        async def _distill() -> None:
            try:
                changed = await self._memory_service.distill_event(event_id)
            except Exception:
                return
            if changed:
                try:
                    await self._send_memory_trace(
                        request_id,
                        conversation_id,
                        name="memory.distill_event",
                        status="completed",
                        started_at=utc_now(),
                        summary="已从本轮对话蒸馏更新长期记忆。",
                        params={"eventId": event_id},
                    )
                    await self._send_event(
                        "server:memory_updated",
                        request_id,
                        conversation_id,
                        self._memory_service.state_payload(),
                    )
                except Exception:
                    return

        asyncio.create_task(_distill())

    async def _record_context_snapshot(
        self,
        conversation_id: str,
        request_id: str,
        content: str,
        payload: dict[str, Any],
    ) -> None:
        if self._memory_service is None:
            return
        try:
            event_id = self._memory_service.ingest_snapshot(
                conversation_id=conversation_id,
                request_id=request_id,
                source="context_compaction",
                content=content,
                payload=payload,
            )
        except Exception:
            return
        await self._send_memory_trace(
            request_id,
            conversation_id,
            name="memory.ingest_snapshot",
            status="completed",
            started_at=utc_now(),
            summary="已在上下文压缩前保存记忆快照。",
            params={"source": "context_compaction"},
            result=event_id,
        )

        async def _distill() -> None:
            try:
                changed = await self._memory_service.distill_event(event_id)
            except Exception:
                return
            if changed:
                try:
                    await self._send_memory_trace(
                        request_id,
                        conversation_id,
                        name="memory.distill_event",
                        status="completed",
                        started_at=utc_now(),
                        summary="已从上下文快照蒸馏更新长期记忆。",
                        params={"eventId": event_id},
                    )
                    await self._send_event(
                        "server:memory_updated",
                        request_id,
                        conversation_id,
                        self._memory_service.state_payload(),
                    )
                except Exception:
                    return

        asyncio.create_task(_distill())

    async def _direct_answer(
        self,
        *,
        conversation_id: str,
        content: str,
        config: AppConfig,
        memory_context: str | None = None,
        conversation_context: str | None = None,
    ) -> str:
        if self._can_use_direct_answer_model(config.agent):
            try:
                try:
                    return await self._direct_answer_with_model(
                        conversation_id,
                        content,
                        config,
                        memory_context=memory_context,
                        conversation_context=conversation_context,
                    )
                except TypeError as exc:
                    if "conversation_context" in str(exc):
                        return await self._direct_answer_with_model(
                            conversation_id,
                            content,
                            config,
                            memory_context=memory_context,
                        )
                    if "memory_context" not in str(exc):
                        raise
                    return await self._direct_answer_with_model(conversation_id, content, config)
            except Exception:
                pass
        return self._fallback_direct_answer(content)

    @staticmethod
    def _can_use_direct_answer_model(agent_config: AgentConfig) -> bool:
        return agent_config.provider in {"openai", "openai-like", "anthropic"} and bool(agent_config.api_key)

    async def _direct_answer_with_model(
        self,
        conversation_id: str,
        content: str,
        config: AppConfig,
        memory_context: str | None = None,
        conversation_context: str | None = None,
    ) -> str:
        agent_config = config.agent
        prompt = build_direct_answer_prompt(
            content,
            memory_context=memory_context,
            conversation_context=conversation_context,
        )

        if agent_config.provider == "anthropic":
            import anthropic

            client = anthropic.AsyncAnthropic(api_key=agent_config.api_key)
            response = await client.messages.create(
                model=agent_config.model_id or "claude-sonnet-4-20250514",
                max_tokens=4096,
                system="\n".join(build_direct_answer_instructions()),
                messages=[{"role": "user", "content": prompt}],
            )
            text_parts = [block.text for block in response.content if block.type == "text"]
            answer = "".join(text_parts).strip()
            if answer:
                return answer
            return str(response)

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
        result = await agent.arun(prompt)
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
