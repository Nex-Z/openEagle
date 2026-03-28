from __future__ import annotations

from dataclasses import dataclass
from typing import Any, AsyncIterator, Protocol


@dataclass
class ReplyChunk:
    content: str


@dataclass
class ReplyTrace:
    trace_id: str
    kind: str
    name: str
    status: str
    started_at: str
    summary: str | None = None
    params: dict[str, Any] | None = None
    result: str | None = None
    completed_at: str | None = None


ProviderStreamEvent = ReplyChunk | ReplyTrace


class AgentProvider(Protocol):
    async def reply(self, conversation_id: str, prompt: str) -> str:
        ...

    async def stream_reply(
        self,
        conversation_id: str,
        prompt: str,
    ) -> AsyncIterator[ProviderStreamEvent]:
        ...
