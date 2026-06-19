from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from .models import ScheduledTask
from .service import SchedulerService
from .store import create_task

_scheduler_service: SchedulerService | None = None
ScheduledTaskOriginResolver = Callable[[str], tuple[str, str] | None]
_origin_resolver: ScheduledTaskOriginResolver | None = None


def set_scheduler_service(service: SchedulerService) -> None:
    global _scheduler_service
    _scheduler_service = service


def set_scheduled_task_origin_resolver(
    resolver: ScheduledTaskOriginResolver | None,
) -> None:
    global _origin_resolver
    _origin_resolver = resolver


def create_scheduled_task(
    name: str,
    prompt: str,
    schedule_expr: str,
    worker_kind: str = "general",
    conversation_id: str | None = None,
) -> str:
    """Create a new scheduled task that will execute automatically at the specified time.

    Args:
        name: Human-readable task name, e.g. "Daily News Summary"
        prompt: The instruction to execute when the task fires, e.g. "Summarize today's important news"
        schedule_expr: Cron expression, e.g. "0 20 * * *" for 8pm daily
        worker_kind: Which worker to use: general, coding, research, or solo
        conversation_id: Optional conversation ID for result delivery

    Returns:
        Confirmation message with task ID and next run time
    """
    if _scheduler_service is None:
        return "Error: scheduler service not initialized."

    try:
        im_channel: str | None = None
        im_chat_id: str | None = None
        if conversation_id and _origin_resolver is not None:
            origin = _origin_resolver(conversation_id)
            if origin is not None:
                im_channel, im_chat_id = origin
        task = ScheduledTask(
            name=name,
            prompt=prompt,
            schedule_expr=schedule_expr,
            worker_kind=worker_kind,  # type: ignore[arg-type]
            conversation_id=conversation_id,
            im_channel=im_channel,
            im_chat_id=im_chat_id,
            created_at=datetime.now(UTC).isoformat(),
            updated_at=datetime.now(UTC).isoformat(),
        )
        create_task(task)
        _scheduler_service.add_task(task)
        return f"已创建定时任务「{name}」（ID: {task.id}），调度表达式: {schedule_expr}。"
    except Exception as exc:
        return f"创建定时任务失败: {exc}"
