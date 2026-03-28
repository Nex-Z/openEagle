from __future__ import annotations

from typing import AsyncIterator

from .config import AppConfig
from .providers.agno_provider import AgnoAgentProvider
from .providers.base import AgentProvider
from .providers.mock import MockAgentProvider


class AgentService:
    def __init__(self, provider: AgentProvider) -> None:
        self._provider = provider

    async def generate_reply(self, conversation_id: str, prompt: str) -> str:
        return await self._provider.reply(conversation_id, prompt)

    async def stream_reply(
        self,
        conversation_id: str,
        prompt: str,
    ) -> AsyncIterator[str]:
        async for chunk in self._provider.stream_reply(conversation_id, prompt):
            yield chunk


def build_agent_service(config: AppConfig) -> AgentService:
    if config.agent.provider in {"openai", "openai-like"}:
        provider = AgnoAgentProvider(config.agent)
    else:
        provider = MockAgentProvider()

    return AgentService(provider)
