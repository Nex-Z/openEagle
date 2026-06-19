from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .models import ScheduledTask, ScheduledTaskExecution


_DB_PATH: Path | None = None


def init_db(db_path: Path) -> None:
    global _DB_PATH
    _DB_PATH = db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS scheduled_tasks (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                prompt TEXT NOT NULL,
                schedule_expr TEXT NOT NULL,
                schedule_type TEXT NOT NULL DEFAULT 'cron',
                enabled INTEGER NOT NULL DEFAULT 1,
                worker_kind TEXT NOT NULL DEFAULT 'general',
                conversation_id TEXT,
                im_channel TEXT,
                im_chat_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                next_run_at TEXT,
                last_run_at TEXT
            );

            CREATE TABLE IF NOT EXISTS scheduled_task_executions (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES scheduled_tasks(id) ON DELETE CASCADE,
                status TEXT NOT NULL DEFAULT 'running',
                result TEXT,
                error TEXT,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                conversation_id TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_executions_task_id ON scheduled_task_executions(task_id);
            CREATE INDEX IF NOT EXISTS idx_executions_started_at ON scheduled_task_executions(started_at);
            """
        )
        conn.commit()
    finally:
        conn.close()


def _conn() -> sqlite3.Connection:
    if _DB_PATH is None:
        raise RuntimeError("scheduler store not initialized; call init_db first")
    return sqlite3.connect(str(_DB_PATH))


def _row_to_task(row: sqlite3.Row) -> ScheduledTask:
    return ScheduledTask.model_validate(
        {key: row[key] for key in row.keys() if row[key] is not None}
    )


def _row_to_execution(row: sqlite3.Row) -> ScheduledTaskExecution:
    return ScheduledTaskExecution.model_validate(
        {key: row[key] for key in row.keys() if row[key] is not None}
    )


def create_task(task: ScheduledTask) -> None:
    conn = _conn()
    try:
        conn.execute(
            """
            INSERT INTO scheduled_tasks (
                id, name, prompt, schedule_expr, schedule_type, enabled,
                worker_kind, conversation_id, im_channel, im_chat_id,
                created_at, updated_at, next_run_at, last_run_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task.id,
                task.name,
                task.prompt,
                task.schedule_expr,
                task.schedule_type,
                int(task.enabled),
                task.worker_kind,
                task.conversation_id,
                task.im_channel,
                task.im_chat_id,
                task.created_at,
                task.updated_at,
                task.next_run_at,
                task.last_run_at,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def update_task(task: ScheduledTask) -> None:
    conn = _conn()
    try:
        conn.execute(
            """
            UPDATE scheduled_tasks SET
                name = ?,
                prompt = ?,
                schedule_expr = ?,
                schedule_type = ?,
                enabled = ?,
                worker_kind = ?,
                conversation_id = ?,
                im_channel = ?,
                im_chat_id = ?,
                updated_at = ?,
                next_run_at = ?,
                last_run_at = ?
            WHERE id = ?
            """,
            (
                task.name,
                task.prompt,
                task.schedule_expr,
                task.schedule_type,
                int(task.enabled),
                task.worker_kind,
                task.conversation_id,
                task.im_channel,
                task.im_chat_id,
                task.updated_at,
                task.next_run_at,
                task.last_run_at,
                task.id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def delete_task(task_id: str) -> None:
    conn = _conn()
    try:
        conn.execute("DELETE FROM scheduled_tasks WHERE id = ?", (task_id,))
        conn.commit()
    finally:
        conn.close()


def get_task(task_id: str) -> ScheduledTask | None:
    conn = _conn()
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return _row_to_task(row) if row else None
    finally:
        conn.close()


def list_tasks() -> list[ScheduledTask]:
    conn = _conn()
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM scheduled_tasks ORDER BY created_at DESC"
        ).fetchall()
        return [_row_to_task(row) for row in rows]
    finally:
        conn.close()


def create_execution(execution: ScheduledTaskExecution) -> None:
    conn = _conn()
    try:
        conn.execute(
            """
            INSERT INTO scheduled_task_executions (
                id, task_id, status, result, error, started_at, completed_at, conversation_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                execution.id,
                execution.task_id,
                execution.status,
                execution.result,
                execution.error,
                execution.started_at,
                execution.completed_at,
                execution.conversation_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def complete_execution(execution_id: str, result: str) -> None:
    from datetime import UTC, datetime

    conn = _conn()
    try:
        conn.execute(
            """
            UPDATE scheduled_task_executions
            SET status = 'completed', result = ?, completed_at = ?
            WHERE id = ?
            """,
            (result, datetime.now(UTC).isoformat(), execution_id),
        )
        conn.commit()
    finally:
        conn.close()


def fail_execution(
    execution_id: str,
    error: str,
    *,
    result: str | None = None,
) -> None:
    from datetime import UTC, datetime

    conn = _conn()
    try:
        conn.execute(
            """
            UPDATE scheduled_task_executions
            SET status = 'failed', result = COALESCE(?, result), error = ?, completed_at = ?
            WHERE id = ?
            """,
            (result, error, datetime.now(UTC).isoformat(), execution_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_history(task_id: str, limit: int = 50) -> list[ScheduledTaskExecution]:
    conn = _conn()
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT * FROM scheduled_task_executions
            WHERE task_id = ?
            ORDER BY started_at DESC
            LIMIT ?
            """,
            (task_id, limit),
        ).fetchall()
        return [_row_to_execution(row) for row in rows]
    finally:
        conn.close()


def update_task_next_run(task_id: str, next_run_at: str | None) -> None:
    conn = _conn()
    try:
        conn.execute(
            "UPDATE scheduled_tasks SET next_run_at = ? WHERE id = ?",
            (next_run_at, task_id),
        )
        conn.commit()
    finally:
        conn.close()


def update_task_last_run(task_id: str, last_run_at: str) -> None:
    conn = _conn()
    try:
        conn.execute(
            "UPDATE scheduled_tasks SET last_run_at = ? WHERE id = ?",
            (last_run_at, task_id),
        )
        conn.commit()
    finally:
        conn.close()
