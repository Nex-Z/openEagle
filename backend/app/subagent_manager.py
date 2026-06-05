from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from .agent_service import build_agent_service
from .attachments import AttachmentStore
from .config import AppConfig
from .confirmations import ToolConfirmationStore
from .memory import MemoryService
from .models import AttachmentRef
from .providers.base import ProviderStreamEvent, ReplyChunk, ReplyTrace
from .prompts import MEMORY_STORAGE_POLICY, current_datetime_instruction
from .subagent_models import AgentRouteDecision, AgentTaskRecord, WorkerReport


MAX_WORKER_SELF_REPAIR_ATTEMPTS = 2
ERROR_RESULT_MARKERS = (
    "Error:",
    "错误",
    "[TIMEOUT]",
    "Traceback",
    "Exception",
)

ContextSnapshotCallback = Callable[
    [str, str, str, dict[str, Any]],
    Awaitable[None],
]
UNRECOVERED_FAILURE_MARKERS = (
    "执行失败",
    "失败",
    "无法",
    "出错",
    "error",
    "exception",
    "traceback",
    "not found",
    "no such",
    "缺少",
)


class SubAgentManager:
    def __init__(self) -> None:
        self._tasks_by_conversation: dict[str, list[AgentTaskRecord]] = defaultdict(list)
        self._read_semaphore = asyncio.Semaphore(3)
        self._write_lock = asyncio.Lock()

    def recent_tasks(self, conversation_id: str) -> list[AgentTaskRecord]:
        return list(self._tasks_by_conversation.get(conversation_id, []))

    def get_task(self, conversation_id: str, worker_id: str) -> AgentTaskRecord | None:
        for task in self._tasks_by_conversation.get(conversation_id, []):
            if task.worker_id == worker_id:
                return task
        return None

    def create_or_reuse(
        self,
        conversation_id: str,
        decision: AgentRouteDecision,
    ) -> AgentTaskRecord:
        if decision.route == "delegate_existing" and decision.target_worker_id:
            existing = self.get_task(conversation_id, decision.target_worker_id)
            if existing is not None and existing.worker_kind != "solo":
                existing.task_brief = decision.task_brief or existing.task_brief
                existing.success_criteria = decision.success_criteria or existing.success_criteria
                existing.context_summary = decision.context_summary or existing.context_summary
                existing.requires_write = existing.requires_write or decision.requires_write
                existing.requires_gui = existing.requires_gui or decision.requires_gui
                existing.mark("idle")
                return existing

        task = AgentTaskRecord(
            conversation_id=conversation_id,
            worker_kind=decision.worker_kind,
            title=decision.task_title or decision.task_brief or "用户请求",
            task_brief=decision.task_brief,
            success_criteria=decision.success_criteria or ["完成用户本轮请求。"],
            context_summary=decision.context_summary,
            requires_write=decision.requires_write,
            requires_gui=decision.requires_gui,
        )
        self._tasks_by_conversation[conversation_id].append(task)
        return task

    async def run_worker(
        self,
        *,
        task: AgentTaskRecord,
        config: AppConfig,
        confirmation_store: ToolConfirmationStore,
        request_id: str,
        attachment_store: AttachmentStore | None = None,
        attachments: list[AttachmentRef] | None = None,
        memory_context: str | None = None,
        memory_service: MemoryService | None = None,
        context_snapshot: ContextSnapshotCallback | None = None,
    ) -> AsyncIterator[ProviderStreamEvent | WorkerReport]:
        if task.requires_write or task.worker_kind == "coding":
            async with self._write_lock:
                async for event in self._run_worker_unlocked(
                    task=task,
                    config=config,
                    confirmation_store=confirmation_store,
                    attachment_store=attachment_store,
                    request_id=request_id,
                    attachments=attachments,
                    memory_context=memory_context,
                    memory_service=memory_service,
                    context_snapshot=context_snapshot,
                ):
                    yield event
            return

        async with self._read_semaphore:
            async for event in self._run_worker_unlocked(
                task=task,
                config=config,
                confirmation_store=confirmation_store,
                attachment_store=attachment_store,
                request_id=request_id,
                attachments=attachments,
                memory_context=memory_context,
                memory_service=memory_service,
                context_snapshot=context_snapshot,
            ):
                yield event

    async def _run_worker_unlocked(
        self,
        *,
        task: AgentTaskRecord,
        config: AppConfig,
        confirmation_store: ToolConfirmationStore,
        request_id: str,
        attachment_store: AttachmentStore | None = None,
        attachments: list[AttachmentRef] | None = None,
        memory_context: str | None = None,
        memory_service: MemoryService | None = None,
        context_snapshot: ContextSnapshotCallback | None = None,
    ) -> AsyncIterator[ProviderStreamEvent | WorkerReport]:
        task.mark("running")
        final_error: str | None = None
        agent_service = build_agent_service(
            config,
            confirmation_store=confirmation_store,
            attachment_store=attachment_store,
            request_id=request_id,
            conversation_id=task.conversation_id,
            context_snapshot=context_snapshot,
            memory_service=memory_service,
        )
        prompt = self._build_worker_prompt(task, memory_context=memory_context)
        try:
            for attempt in range(MAX_WORKER_SELF_REPAIR_ATTEMPTS + 1):
                chunks: list[str] = []
                feedback_items: list[str] = []
                try:
                    async for event in agent_service.stream_reply(
                        task.scoped_conversation_id,
                        prompt,
                        attachments=attachments,
                    ):
                        if isinstance(event, ReplyChunk):
                            chunks.append(event.content)
                            continue
                        if isinstance(event, ReplyTrace):
                            event.params = self._augment_trace_params(event.params, task)
                            if self._trace_needs_agent_feedback(event):
                                feedback_items.append(self._trace_feedback_text(event))
                        yield event
                except Exception as exc:  # noqa: BLE001
                    final_error = str(exc)
                    if attempt < MAX_WORKER_SELF_REPAIR_ATTEMPTS and self._is_recoverable_worker_exception(exc):
                        prompt = self._build_worker_retry_prompt(
                            task,
                            errors=[str(exc)],
                            previous_output="",
                            attempt=attempt + 1,
                            memory_context=memory_context,
                        )
                        yield self._repair_trace(task, str(exc), attempt + 1)
                        continue
                    raise

                content = "".join(chunks).strip()
                if (
                    attempt < MAX_WORKER_SELF_REPAIR_ATTEMPTS
                    and self._should_retry_worker_output(content, feedback_items)
                ):
                    final_error = "\n".join(feedback_items) or "worker 输出为空或未能修复错误。"
                    prompt = self._build_worker_retry_prompt(
                        task,
                        errors=feedback_items,
                        previous_output=content,
                        attempt=attempt + 1,
                        memory_context=memory_context,
                    )
                    yield self._repair_trace(task, final_error, attempt + 1)
                    continue

                task.mark("completed")
                report = WorkerReport(
                    worker_id=task.worker_id,
                    worker_kind=task.worker_kind,
                    state="completed",
                    title=task.title,
                    summary=self._summarize_result(content),
                    evidence=self._extract_evidence(content),
                    result=content,
                    started_at=task.created_at,
                    completed_at=task.completed_at,
                )
                task.last_report = report
                yield report
                return

            raise RuntimeError(final_error or "worker 自修复后仍未返回结果。")
        except Exception as exc:  # noqa: BLE001
            task.mark("failed")
            report = WorkerReport(
                worker_id=task.worker_id,
                worker_kind=task.worker_kind,
                state="failed",
                title=task.title,
                summary="worker 执行失败。",
                result="",
                error=str(exc),
                started_at=task.created_at,
                completed_at=task.completed_at,
            )
            task.last_report = report
            yield report

    @staticmethod
    def _augment_trace_params(
        params: dict[str, Any] | None,
        task: AgentTaskRecord,
    ) -> dict[str, Any]:
        merged = dict(params or {})
        merged.setdefault("agentTaskId", task.worker_id)
        merged.setdefault("workerKind", task.worker_kind)
        return merged

    @staticmethod
    def _build_worker_prompt(task: AgentTaskRecord, memory_context: str | None = None) -> str:
        criteria = "\n".join(f"- {item}" for item in task.success_criteria)
        context = task.context_summary.strip()
        context_block = f"\n必要上下文:\n{context}\n" if context else ""
        memory_block = f"\n长期记忆:\n{memory_context}\n" if memory_context else ""
        return (
            f"你是 openEagle 的 {task.worker_kind} worker。"
            "请只处理 main agent 委派给你的任务，不要自行扩展到无关事项。\n\n"
            f"{current_datetime_instruction()}\n\n"
            f"{MEMORY_STORAGE_POLICY}\n\n"
            f"任务: {task.task_brief}\n"
            f"{context_block}"
            f"{memory_block}"
            f"完成标准:\n{criteria}\n\n"
            "普通问答和不需要当前状态的解释，直接回答，不要调用工具。"
            "需要工具时先少量验证关键事实，避免展开成无关的批量搜索或命令。"
            "完成工具调用后必须给出面向用户的最终结论。\n\n"
            "遇到工具错误、参数错误或执行失败时，先把错误当成 observation 自己修正并重试，"
            "不要把第一轮错误直接交给用户。完成后直接给出面向用户的结果摘要。"
        )

    @staticmethod
    def _build_worker_retry_prompt(
        task: AgentTaskRecord,
        errors: list[str],
        previous_output: str,
        attempt: int,
        memory_context: str | None = None,
    ) -> str:
        error_text = "\n".join(f"- {item}" for item in errors if item.strip())
        previous = previous_output.strip()
        previous_block = f"\n你上一轮的输出:\n{previous[:1600]}\n" if previous else ""
        criteria = "\n".join(f"- {item}" for item in task.success_criteria)
        memory_block = f"\n长期记忆:\n{memory_context}\n" if memory_context else ""
        return (
            f"这是第 {attempt} 次自动修复反馈。不要把下面的错误直接交给用户，"
            "把它当成工具/执行 observation，自己修正参数、路径、命令或方案后重新尝试。\n\n"
            f"{current_datetime_instruction()}\n\n"
            f"{MEMORY_STORAGE_POLICY}\n\n"
            f"原任务: {task.task_brief}\n"
            f"{memory_block}"
            f"完成标准:\n{criteria}\n\n"
            f"上一轮错误:\n{error_text or '- worker 输出为空或没有推进任务。'}\n"
            f"{previous_block}\n"
            "请重新决策并继续执行。只有在已经尝试替代方案后仍不可恢复，才用最终回复说明原因。"
        )

    @staticmethod
    def _repair_trace(task: AgentTaskRecord, reason: str, attempt: int) -> ReplyTrace:
        return ReplyTrace(
            trace_id=f"{task.worker_id}-self-repair-{attempt}",
            kind="agent",
            name=f"{task.worker_kind}-worker-self-repair",
            status="completed",
            summary="worker 捕获到执行错误，已反馈给当前 agent 自动重试。",
            params={
                "agentTaskId": task.worker_id,
                "workerKind": task.worker_kind,
                "attempt": attempt,
            },
            result=reason,
            started_at=task.updated_at,
            completed_at=task.updated_at,
        )

    @classmethod
    def _trace_needs_agent_feedback(cls, trace: ReplyTrace) -> bool:
        if trace.status == "error":
            return True
        result = str(trace.result or "").strip()
        if result.startswith("CONFIRMATION_REQUIRED"):
            return False
        return any(marker in result for marker in ERROR_RESULT_MARKERS)

    @staticmethod
    def _trace_feedback_text(trace: ReplyTrace) -> str:
        result = str(trace.result or "").strip()
        params = trace.params or {}
        return (
            f"{trace.name} 调用失败或返回错误。"
            f"params={params}; result={result or trace.summary or 'unknown error'}"
        )

    @classmethod
    def _should_retry_worker_output(cls, content: str, feedback_items: list[str]) -> bool:
        if not content.strip():
            return True
        lowered = content.lower()
        if feedback_items and any(marker in lowered for marker in UNRECOVERED_FAILURE_MARKERS):
            return True
        return False

    @staticmethod
    def _is_recoverable_worker_exception(exc: Exception) -> bool:
        text = str(exc).lower()
        non_recoverable = ("api key", "base url", "需要配置", "not configured")
        return not any(marker in text for marker in non_recoverable)

    @staticmethod
    def _summarize_result(content: str) -> str:
        if not content:
            return "worker 没有返回正文。"
        lines = [line.strip() for line in content.splitlines() if line.strip()]
        if not lines:
            return content[:300]
        summary = "\n".join(lines[:6])
        return summary[:1200]

    @staticmethod
    def _extract_evidence(content: str) -> list[str]:
        evidence: list[str] = []
        for line in content.splitlines():
            stripped = line.strip(" -\t")
            lowered = stripped.lower()
            if not stripped:
                continue
            if any(marker in lowered for marker in ("test", "测试", "验证", "已完成", "completed")):
                evidence.append(stripped[:300])
            if len(evidence) >= 5:
                break
        return evidence
