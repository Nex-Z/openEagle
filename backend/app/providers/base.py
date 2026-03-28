from __future__ import annotations

from typing import AsyncIterator, Protocol


class AgentProvider(Protocol):
    async def reply(self, conversation_id: str, prompt: str) -> str:
        ...

    async def stream_reply(
        self,
        conversation_id: str,
        prompt: str,
    ) -> AsyncIterator[str]:
        ...
