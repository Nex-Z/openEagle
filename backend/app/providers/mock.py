from __future__ import annotations

import asyncio
from typing import AsyncIterator


class MockAgentProvider:
    async def reply(self, conversation_id: str, prompt: str) -> str:
        normalized = prompt.strip()
        return (
            "openEagle 已收到你的请求。\n\n"
            f"conversationId: {conversation_id}\n"
            f"echo: {normalized}\n\n"
            "当前回复来自 mock provider。你可以在设置中切换到 openai 或 openai-like，并通过 Agno 驱动真实模型。"
        )

    async def stream_reply(
        self,
        conversation_id: str,
        prompt: str,
    ) -> AsyncIterator[str]:
        reply = await self.reply(conversation_id, prompt)
        chunks = [reply[index : index + 24] for index in range(0, len(reply), 24)]

        for chunk in chunks:
            if not chunk:
                continue
            await asyncio.sleep(0.05)
            yield chunk
