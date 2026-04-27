from __future__ import annotations

from typing import AsyncIterator

from .config import AppConfig
from .confirmations import ToolConfirmationStore
from .providers.agno_provider import AgnoAgentProvider
from .providers.base import AgentProvider, ProviderStreamEvent
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
    ) -> AsyncIterator[ProviderStreamEvent]:
        async for chunk in self._provider.stream_reply(conversation_id, prompt):
            yield chunk


def build_agent_service(
    config: AppConfig,
    confirmation_store: ToolConfirmationStore | None = None,
    request_id: str | None = None,
    conversation_id: str | None = None,
) -> AgentService:
    if config.agent.provider in {"openai", "openai-like"}:
        provider = AgnoAgentProvider(
            config,
            confirmation_store=confirmation_store,
            request_id=request_id,
            conversation_id=conversation_id,
        )
    else:
        provider = MockAgentProvider(config)

    return AgentService(provider)
