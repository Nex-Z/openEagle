from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from ..config import AppConfig
from ..models import utc_now
from ..providers.base import ReplyChunk
from ..subagent_models import AgentRouteDecision, WorkerReport
from ..token_usage import token_usage_scope
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
SendRemote = Callable[[str, str, str], Awaitable[None]]
ConfigGetter = Callable[[], AppConfig]
MemoryContextGetter = Callable[[str | None], str]


def _log(message: str) -> None:
    line = f"[SCHEDULER] {utc_now()} {message}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        # 兜底：Windows cp1252 等非 UTF-8 控制台无法编码中文时，直接写 UTF-8 字节
        sys.stdout.buffer.write((line + "\n").encode("utf-8", "replace"))
        sys.stdout.buffer.flush()


class SchedulerService:
    def __init__(
        self,
        *,
        config_getter: ConfigGetter,
        send_event: SendEvent | None = None,
        send_remote: SendRemote | None = None,
        memory_context_getter: MemoryContextGetter | None = None,
    ) -> None:
        from ..subagent_manager import SubAgentManager

        self._config_getter = config_getter
        self._send_event = send_event
        self._send_remote = send_remote
        self._memory_context_getter = memory_context_getter
        self._scheduler = AsyncIOScheduler()
        self._subagents = SubAgentManager()
        self._running = False
        self._running_task_ids: set[str] = set()
        self._manual_tasks: set[asyncio.Task[ScheduledTaskExecution | None]] = set()

    def start(self) -> None:
        if self._running:
            return
        self._scheduler.start()
        self._running = True
        _log("started")
        self.reload_tasks()

    def shutdown(self) -> None:
        if not self._running:
            return
        self._scheduler.shutdown(wait=False)
        self._running = False
        _log("stopped")

    def add_task(self, task: ScheduledTask) -> None:
        job_id = self._job_id(task.id)
        self.remove_task(task.id)
        if not task.enabled:
            _log(f"task disabled id={task.id}")
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
                _log(
                    "task registered "
                    f"id={task.id} schedule={task.schedule_expr} next_run={next_run.next_run_time.isoformat()}"
                )
            else:
                update_task_next_run(task.id, None)
                _log(f"task registered id={task.id} schedule={task.schedule_expr} next_run=-")
        except Exception as exc:
            update_task_next_run(task.id, None)
            _log(f"task register failed id={task.id} schedule={task.schedule_expr} error={exc}")
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

    def trigger_task_now(
        self,
        task_id: str,
    ) -> asyncio.Task[ScheduledTaskExecution | None]:
        task = get_task(task_id)
        if task is None:
            raise ValueError("定时任务不存在")
        if task_id in self._running_task_ids:
            raise RuntimeError("该定时任务正在执行，请等待本次执行完成")

        self._running_task_ids.add(task_id)
        manual_task = asyncio.create_task(
            self._execute_task(task_id, allow_disabled=True, already_claimed=True)
        )
        self._manual_tasks.add(manual_task)
        manual_task.add_done_callback(self._manual_tasks.discard)
        return manual_task

    def reload_tasks(self) -> None:
        tasks = list_tasks()
        registered = 0
        disabled = 0
        invalid = 0
        for task in tasks:
            if not task.enabled:
                disabled += 1
                update_task_next_run(task.id, None)
                continue
            try:
                self.add_task(task)
                registered += 1
            except ValueError:
                invalid += 1
                continue
        _log(
            "reload complete "
            f"total={len(tasks)} registered={registered} disabled={disabled} invalid={invalid}"
        )

    @staticmethod
    def _job_id(task_id: str) -> str:
        return f"scheduled-task-{task_id}"

    async def _execute_task(
        self,
        task_id: str,
        *,
        allow_disabled: bool = False,
        already_claimed: bool = False,
    ) -> ScheduledTaskExecution | None:
        task = get_task(task_id)
        if task is None or (not task.enabled and not allow_disabled):
            if already_claimed:
                self._running_task_ids.discard(task_id)
            return None
        if not already_claimed:
            if task_id in self._running_task_ids:
                _log(f"task execution skipped id={task_id} reason=already_running")
                return None
            self._running_task_ids.add(task_id)

        execution = ScheduledTaskExecution(
            task_id=task_id,
            conversation_id=f"scheduled-{task_id}-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}",
        )
        create_execution(execution)
        update_task_last_run(task_id, utc_now())
        _log(
            "task execution started "
            f"id={task_id} execution={execution.id} worker={task.worker_kind}"
        )

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
            async with token_usage_scope(
                request_id=execution.id,
                conversation_id=execution.conversation_id or task_id,
                source="scheduled",
            ):
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
                        await self._complete_and_deliver(task, execution, result)
                        return execution

            result = "".join(result_parts) if result_parts else "任务执行完成，但未返回结果。"
            await self._complete_and_deliver(task, execution, result)
            return execution
        except Exception as exc:
            task_error = str(exc)
            fail_execution(execution.id, task_error)
            _log(
                "task execution failed "
                f"id={task_id} execution={execution.id} error={type(exc).__name__}: {exc}"
            )
            try:
                await self._deliver_result(task, error=task_error)
            except Exception as delivery_exc:
                combined_error = f"{task_error}\n结果投递失败: {delivery_exc}"
                fail_execution(execution.id, combined_error)
                _log(
                    "task failure delivery failed "
                    f"id={task_id} execution={execution.id} "
                    f"error={type(delivery_exc).__name__}: {delivery_exc}"
                )
            return execution
        finally:
            self._sync_next_run(task_id)
            self._running_task_ids.discard(task_id)

    async def _complete_and_deliver(
        self,
        task: ScheduledTask,
        execution: ScheduledTaskExecution,
        result: str,
    ) -> None:
        try:
            await self._deliver_result(task, result)
        except Exception as exc:
            delivery_error = f"任务已完成，但结果投递失败: {exc}"
            fail_execution(execution.id, delivery_error, result=result)
            _log(
                "task result delivery failed "
                f"id={task.id} execution={execution.id} "
                f"error={type(exc).__name__}: {exc}"
            )
            return

        complete_execution(execution.id, result)
        _log(
            "task execution completed "
            f"id={task.id} execution={execution.id} result_len={len(result)}"
        )

    def _sync_next_run(self, task_id: str) -> None:
        job = self._scheduler.get_job(self._job_id(task_id))
        next_run_at = (
            job.next_run_time.isoformat()
            if job is not None and job.next_run_time is not None
            else None
        )
        update_task_next_run(task_id, next_run_at)
        _log(f"task next run updated id={task_id} next_run={next_run_at or '-'}")

    async def _deliver_result(
        self, task: ScheduledTask, result: str | None = None, error: str | None = None
    ) -> None:
        text = result or f"任务执行失败: {error}"

        if task.im_channel and task.im_chat_id:
            if self._send_remote is None:
                raise RuntimeError("远程投递服务未初始化")
            remote_text = f"【定时任务：{task.name}】\n\n{text}"
            await self._send_remote(task.im_channel, task.im_chat_id, remote_text)
            _log(
                "task remote result delivered "
                f"id={task.id} channel={task.im_channel} target={task.im_chat_id}"
            )
            return

        if task.conversation_id:
            if self._send_event is None:
                raise RuntimeError("本地客户端未连接")
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
            _log(f"task local result delivered id={task.id}")
            return

        raise RuntimeError("定时任务没有可用的结果投递目标")


class _AutoConfirmStore:
    """一个假的 confirmation store，自动允许所有工具操作。"""

    def get(self, confirmation_id: str) -> Any:
        return None

    def pop(self, confirmation_id: str) -> Any:
        return None
