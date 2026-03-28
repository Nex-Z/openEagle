from __future__ import annotations

from typing import AsyncIterator

from agno.agent import Agent
from agno.models.openai import OpenAIResponses
from agno.models.openai.like import OpenAILike
from agno.run.agent import IntermediateRunContentEvent, RunContentEvent

from ..config import AgentConfig


class AgnoAgentProvider:
    def __init__(self, config: AgentConfig) -> None:
        self._config = config

    def _build_agent(self, conversation_id: str) -> Agent:
        model_id = self._config.model_id or "gpt-5-mini"

        if self._config.provider == "openai-like":
            if not self._config.base_url:
                raise ValueError("openai-like 模式需要配置 Base URL。")
            model = OpenAILike(
                id=model_id,
                api_key=self._config.api_key,
                base_url=self._config.base_url,
            )
        else:
            model = OpenAIResponses(
                id=model_id,
                api_key=self._config.api_key,
            )

        return Agent(
            model=model,
            markdown=True,
            instructions=[
                "你是 openEagle 的桌面 Agent 助手。",
                f"当前会话 ID: {conversation_id}",
                "回答默认使用简洁中文。",
            ],
        )

    async def reply(self, conversation_id: str, prompt: str) -> str:
        if not self._config.api_key:
            raise ValueError("当前 provider 需要配置 API Key。")

        agent = self._build_agent(conversation_id)
        result = await agent.arun(prompt)
        content = getattr(result, "content", None)
        if isinstance(content, str) and content.strip():
            return content
        return str(result)

    async def stream_reply(
        self,
        conversation_id: str,
        prompt: str,
    ) -> AsyncIterator[str]:
        if not self._config.api_key:
            raise ValueError("当前 provider 需要配置 API Key。")

        agent = self._build_agent(conversation_id)
        stream = agent.arun(prompt, stream=True)

        async for event in stream:
            if isinstance(event, (RunContentEvent, IntermediateRunContentEvent)):
                content = getattr(event, "content", None)
                if isinstance(content, str) and content:
                    yield content
