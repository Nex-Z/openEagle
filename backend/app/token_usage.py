from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, closing
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, AsyncIterator
from uuid import uuid4

UsageCallback = Callable[[dict[str, Any]], Awaitable[None]]


def _read_int(value: Any, *names: str) -> int:
    for name in names:
        if isinstance(value, dict):
            candidate = value.get(name)
        else:
            candidate = getattr(value, name, None)
        if candidate is not None:
            try:
                return max(0, int(candidate))
            except (TypeError, ValueError):
                continue
    return 0


def normalize_usage(value: Any) -> tuple[int, int, int]:
    input_tokens = _read_int(value, "prompt_tokens", "input_tokens")
    output_tokens = _read_int(value, "completion_tokens", "output_tokens")
    total_tokens = _read_int(value, "total_tokens")
    if total_tokens <= 0:
        total_tokens = input_tokens + output_tokens
    return input_tokens, output_tokens, total_tokens


@dataclass
class RequestUsage:
    request_id: str
    conversation_id: str
    source: str
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    calls: int = 0
    models: set[str] = field(default_factory=set)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    on_update: UsageCallback | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "requestId": self.request_id,
            "conversationId": self.conversation_id,
            "source": self.source,
            "inputTokens": self.input_tokens,
            "outputTokens": self.output_tokens,
            "totalTokens": self.total_tokens,
            "calls": self.calls,
            "models": sorted(self.models),
            "updatedAt": datetime.now().astimezone().isoformat(),
        }


_current_request: ContextVar[RequestUsage | None] = ContextVar(
    "token_usage_request",
    default=None,
)


class TokenUsageStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS model_usage (
                    id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL,
                    total_tokens INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_model_usage_created_at "
                "ON model_usage(created_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_model_usage_request_id "
                "ON model_usage(request_id)"
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def record(
        self,
        *,
        request: RequestUsage,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        total_tokens: int,
    ) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO model_usage (
                    id, request_id, conversation_id, source, provider, model,
                    input_tokens, output_tokens, total_tokens, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uuid4().hex,
                    request.request_id,
                    request.conversation_id,
                    request.source,
                    provider,
                    model,
                    input_tokens,
                    output_tokens,
                    total_tokens,
                    datetime.now().astimezone().isoformat(),
                ),
            )
            connection.commit()

    @staticmethod
    def _summary(row: sqlite3.Row | None) -> dict[str, int]:
        return {
            "inputTokens": int(row["input_tokens"] or 0) if row else 0,
            "outputTokens": int(row["output_tokens"] or 0) if row else 0,
            "totalTokens": int(row["total_tokens"] or 0) if row else 0,
            "calls": int(row["calls"] or 0) if row else 0,
        }

    def dashboard(self) -> dict[str, Any]:
        today = date.today()
        first_day = today - timedelta(days=6)
        with closing(self._connect()) as connection:
            total_row = connection.execute(
                """
                SELECT SUM(input_tokens) AS input_tokens,
                       SUM(output_tokens) AS output_tokens,
                       SUM(total_tokens) AS total_tokens,
                       COUNT(*) AS calls
                FROM model_usage
                """
            ).fetchone()
            today_row = connection.execute(
                """
                SELECT SUM(input_tokens) AS input_tokens,
                       SUM(output_tokens) AS output_tokens,
                       SUM(total_tokens) AS total_tokens,
                       COUNT(*) AS calls
                FROM model_usage
                WHERE substr(created_at, 1, 10) = ?
                """,
                (today.isoformat(),),
            ).fetchone()
            day_rows = connection.execute(
                """
                SELECT substr(created_at, 1, 10) AS day,
                       SUM(input_tokens) AS input_tokens,
                       SUM(output_tokens) AS output_tokens,
                       SUM(total_tokens) AS total_tokens,
                       COUNT(*) AS calls
                FROM model_usage
                WHERE substr(created_at, 1, 10) >= ?
                GROUP BY substr(created_at, 1, 10)
                ORDER BY day
                """,
                (first_day.isoformat(),),
            ).fetchall()
            model_rows = connection.execute(
                """
                SELECT provider, model,
                       SUM(input_tokens) AS input_tokens,
                       SUM(output_tokens) AS output_tokens,
                       SUM(total_tokens) AS total_tokens,
                       COUNT(*) AS calls
                FROM model_usage
                GROUP BY provider, model
                ORDER BY total_tokens DESC
                LIMIT 12
                """
            ).fetchall()
            request_rows = connection.execute(
                """
                SELECT request_id, conversation_id, source,
                       GROUP_CONCAT(DISTINCT model) AS models,
                       SUM(input_tokens) AS input_tokens,
                       SUM(output_tokens) AS output_tokens,
                       SUM(total_tokens) AS total_tokens,
                       COUNT(*) AS calls,
                       MAX(created_at) AS updated_at
                FROM model_usage
                GROUP BY request_id, conversation_id, source
                ORDER BY updated_at DESC
                LIMIT 20
                """
            ).fetchall()

        days_by_date = {row["day"]: row for row in day_rows}
        days: list[dict[str, Any]] = []
        for offset in range(7):
            day = first_day + timedelta(days=offset)
            days.append({"date": day.isoformat(), **self._summary(days_by_date.get(day.isoformat()))})

        models = [
            {
                "provider": str(row["provider"]),
                "model": str(row["model"]),
                **self._summary(row),
            }
            for row in model_rows
        ]
        recent_requests = [
            {
                "requestId": str(row["request_id"]),
                "conversationId": str(row["conversation_id"]),
                "source": str(row["source"]),
                "models": [item for item in str(row["models"] or "").split(",") if item],
                "updatedAt": str(row["updated_at"]),
                **self._summary(row),
            }
            for row in request_rows
        ]
        return {
            "total": self._summary(total_row),
            "today": self._summary(today_row),
            "days": days,
            "models": models,
            "recentRequests": recent_requests,
        }


_store: TokenUsageStore | None = None


def init_token_usage_db(path: Path) -> TokenUsageStore:
    global _store
    _store = TokenUsageStore(path)
    return _store


def token_usage_dashboard() -> dict[str, Any]:
    if _store is None:
        return {
            "total": TokenUsageStore._summary(None),
            "today": TokenUsageStore._summary(None),
            "days": [],
            "models": [],
            "recentRequests": [],
        }
    return _store.dashboard()


@asynccontextmanager
async def token_usage_scope(
    *,
    request_id: str,
    conversation_id: str,
    source: str,
    on_update: UsageCallback | None = None,
) -> AsyncIterator[RequestUsage]:
    request = RequestUsage(
        request_id=request_id,
        conversation_id=conversation_id,
        source=source,
        on_update=on_update,
    )
    token = _current_request.set(request)
    try:
        yield request
    finally:
        _current_request.reset(token)


async def record_model_usage(provider: str, model: str, usage: Any) -> None:
    request = _current_request.get()
    if request is None or _store is None or usage is None:
        return
    input_tokens, output_tokens, total_tokens = normalize_usage(usage)
    if total_tokens <= 0:
        return

    async with request.lock:
        await asyncio.to_thread(
            _store.record,
            request=request,
            provider=provider or "unknown",
            model=model or "unknown",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
        )
        request.input_tokens += input_tokens
        request.output_tokens += output_tokens
        request.total_tokens += total_tokens
        request.calls += 1
        request.models.add(model or "unknown")
        if request.on_update is not None:
            try:
                dashboard = await asyncio.to_thread(_store.dashboard)
                await request.on_update(
                    {
                        "requestUsage": request.payload(),
                        "dashboard": dashboard,
                    }
                )
            except Exception:
                pass
