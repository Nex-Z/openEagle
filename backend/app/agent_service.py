from __future__ import annotations

from .config import AppConfig
from .providers.agno_provider import AgnoAgentProvider
from .providers.base import AgentProvider
from .providers.mock import MockAgentProvider


class AgentService:
    def __init__(self, provider: AgentProvider) -> None:
        self._provider = provider

    async def generate_reply(self, conversation_id: str, prompt: str) -> str:
        return await self._provider.reply(conversation_id, prompt)


def build_agent_service(config: AppConfig) -> AgentService:
    if config.agent.provider in {"openai", "openai-like"}:
        provider = AgnoAgentProvider(config.agent)
    else:
        provider = MockAgentProvider()

    return AgentService(provider)
