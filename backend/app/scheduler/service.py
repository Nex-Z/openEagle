from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from ..config import AppConfig
from ..models import utc_now
from ..providers.base import ReplyChunk
from ..subagent_models import AgentRouteDecision, WorkerReport
from .models import ScheduledTask, ScheduledTaskExecution
from .store import (
    complete_execution,
    create_execution,
    fail_execution,
    get_task,
    list_tasks,
    update_task_last_run,
    update_task_next_run,
)

SendEvent = Callable[[str, str, str, dict[str, Any]], Awaitable[None]]
ConfigGetter = Callable[[], AppConfig]
MemoryContextGetter = Callable[[str | None], str]


class SchedulerService:
    def __init__(
        self,
        *,
        config_getter: ConfigGetter,
        send_event: SendEvent | None = None,
        memory_context_getter: MemoryContextGetter | None = None,
    ) -> None:
        from ..subagent_manager import SubAgentManager

        self._config_getter = config_getter
        self._send_event = send_event
        self._memory_context_getter = memory_context_getter
        self._scheduler = AsyncIOScheduler()
        self._subagents = SubAgentManager()
        self._running = False

    def start(self) -> None:
        if self._running:
            return
        self._scheduler.start()
        self._running = True
        self.reload_tasks()

    def shutdown(self) -> None:
        if not self._running:
            return
        self._scheduler.shutdown(wait=False)
        self._running = False

    def add_task(self, task: ScheduledTask) -> None:
        job_id = self._job_id(task.id)
        self.remove_task(task.id)
        if not task.enabled:
            return
        try:
            trigger = CronTrigger.from_crontab(task.schedule_expr)
            self._scheduler.add_job(
                self._execute_task,
                trigger=trigger,
                id=job_id,
                args=[task.id],
                replace_existing=True,
            )
            next_run = self._scheduler.get_job(job_id)
            if next_run and next_run.next_run_time:
                update_task_next_run(task.id, next_run.next_run_time.isoformat())
            else:
                update_task_next_run(task.id, None)
        except Exception as exc:
            update_task_next_run(task.id, None)
            raise ValueError(f"无效调度表达式: {task.schedule_expr}") from exc

    def remove_task(self, task_id: str) -> None:
        job_id = self._job_id(task_id)
        try:
            self._scheduler.remove_job(job_id)
        except Exception:
            pass
        update_task_next_run(task_id, None)

    def update_task(self, task: ScheduledTask) -> None:
        self.add_task(task)

    def reload_tasks(self) -> None:
        for task in list_tasks():
            if not task.enabled:
                update_task_next_run(task.id, None)
                continue
            try:
                self.add_task(task)
            except ValueError:
                continue

    @staticmethod
    def _job_id(task_id: str) -> str:
        return f"scheduled-task-{task_id}"

    async def _execute_task(self, task_id: str) -> None:
        task = get_task(task_id)
        if task is None or not task.enabled:
            return

        execution = ScheduledTaskExecution(
            task_id=task_id,
            conversation_id=f"scheduled-{task_id}-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}",
        )
        create_execution(execution)
        update_task_last_run(task_id, utc_now())

        try:
            config = self._config_getter()
            decision = AgentRouteDecision(
                route="delegate_new",
                worker_kind=task.worker_kind,
                task_title=task.name,
                task_brief=task.prompt,
                success_criteria=["完成定时任务并给出简洁结果。"],
            )
            agent_task = self._subagents.create_or_reuse(
                execution.conversation_id or task_id, decision
            )

            result_parts: list[str] = []
            async for event in self._subagents.run_worker(
                task=agent_task,
                config=config,
                confirmation_store=_AutoConfirmStore(),
                request_id=execution.id,
                memory_context=(
                    self._memory_context_getter(task.prompt)
                    if self._memory_context_getter
                    else None
                ),
            ):
                if isinstance(event, ReplyChunk):
                    result_parts.append(event.content)
                elif isinstance(event, WorkerReport):
                    result = event.result or event.summary or ""
                    if not result and result_parts:
                        result = "".join(result_parts)
                    if not result:
                        result = "任务执行完成，但未返回结果。"
                    complete_execution(execution.id, result)
                    await self._deliver_result(task, result)
                    return

            result = "".join(result_parts) if result_parts else "任务执行完成，但未返回结果。"
            complete_execution(execution.id, result)
            await self._deliver_result(task, result)
        except Exception as exc:
            fail_execution(execution.id, str(exc))
            await self._deliver_result(task, error=str(exc))

    async def _deliver_result(
        self, task: ScheduledTask, result: str | None = None, error: str | None = None
    ) -> None:
        text = result or f"任务执行失败: {error}"

        if self._send_event and task.conversation_id:
            try:
                await self._send_event(
                    "server:scheduled_task_executed",
                    f"scheduled-{task.id}",
                    task.conversation_id,
                    {
                        "taskId": task.id,
                        "taskName": task.name,
                        "result": text,
                        "error": error,
                    },
                )
            except Exception:
                pass


class _AutoConfirmStore:
    """一个假的 confirmation store，自动允许所有工具操作。"""

    def get(self, confirmation_id: str) -> Any:
        return None

    def pop(self, confirmation_id: str) -> Any:
        return None
