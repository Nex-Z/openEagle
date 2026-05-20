from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, AsyncIterator

from .config import AppConfig
from .confirmations import ToolConfirmationStore
from .attachments import AttachmentStore
from .models import AttachmentRef
from .providers.agno_provider import AgnoAgentProvider
from .providers.anthropic_provider import AnthropicAgentProvider
from .providers.base import AgentProvider, ProviderStreamEvent
from .providers.mock import MockAgentProvider

ContextSnapshotCallback = Callable[
    [str, str, str, dict[str, Any]],
    Awaitable[None],
]


class AgentService:
    def __init__(self, provider: AgentProvider) -> None:
        self._provider = provider

    async def generate_reply(
        self,
        conversation_id: str,
        prompt: str,
        attachments: list[AttachmentRef] | None = None,
    ) -> str:
        return await self._provider.reply(conversation_id, prompt, attachments=attachments)

    async def stream_reply(
        self,
        conversation_id: str,
        prompt: str,
        attachments: list[AttachmentRef] | None = None,
    ) -> AsyncIterator[ProviderStreamEvent]:
        async for chunk in self._provider.stream_reply(
            conversation_id,
            prompt,
            attachments=attachments,
        ):
            yield chunk


def build_agent_service(
    config: AppConfig,
    confirmation_store: ToolConfirmationStore | None = None,
    attachment_store: AttachmentStore | None = None,
    request_id: str | None = None,
    conversation_id: str | None = None,
    context_snapshot: ContextSnapshotCallback | None = None,
) -> AgentService:
    if config.agent.provider in {"openai", "openai-like"}:
        provider = AgnoAgentProvider(
            config,
            confirmation_store=confirmation_store,
            attachment_store=attachment_store,
            request_id=request_id,
            conversation_id=conversation_id,
            context_snapshot=context_snapshot,
        )
    elif config.agent.provider == "anthropic":
        provider = AnthropicAgentProvider(
            config,
            confirmation_store=confirmation_store,
            attachment_store=attachment_store,
            request_id=request_id,
            conversation_id=conversation_id,
            context_snapshot=context_snapshot,
        )
    else:
        provider = MockAgentProvider(config)

    return AgentService(provider)
