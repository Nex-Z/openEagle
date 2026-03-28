from __future__ import annotations

from typing import Protocol


class AgentProvider(Protocol):
    async def reply(self, conversation_id: str, prompt: str) -> str:
        ...
