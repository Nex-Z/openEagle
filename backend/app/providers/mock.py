from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from typing import AsyncIterator

from ..config import AppConfig, McpConfig, SkillConfig, ToolConfig
from .base import ProviderStreamEvent, ReplyChunk, ReplyTrace


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class MockAgentProvider:
    def __init__(self, config: AppConfig) -> None:
        self._config = config

    def _extract_selected_capabilities(
        self,
        prompt: str,
    ) -> tuple[str, list[ReplyTrace]]:
        traces: list[ReplyTrace] = []
        cleaned = prompt

        for tool in self._config.tools:
            if not tool.enabled:
                continue
            cleaned, matched = self._consume_command(cleaned, "tool", tool.name)
            if matched:
                now = utc_now()
                traces.append(
                    ReplyTrace(
                        trace_id=f"tool-{tool.id}",
                        kind="tool",
                        name=tool.name,
                        status="completed",
                        summary="已将工具加入当前轮可用能力。",
                        params={
                            "command": tool.command,
                            "description": tool.description,
                        },
                        result="Mock provider 未真实执行命令，仅记录为本轮可用工具。",
                        started_at=now,
                        completed_at=now,
                    )
                )

        for server in self._config.mcp:
            if not server.enabled:
                continue
            cleaned, matched = self._consume_command(cleaned, "mcp", server.name)
            if matched:
                now = utc_now()
                traces.append(
                    ReplyTrace(
                        trace_id=f"mcp-{server.id}",
                        kind="mcp",
                        name=server.name,
                        status="completed",
                        summary="已将 MCP Server 声明到当前轮上下文。",
                        params={
                            "transport": server.transport,
                            "endpoint": server.endpoint,
                            "description": server.description,
                        },
                        result="Mock provider 未真实连接 MCP，仅记录可用端点信息。",
                        started_at=now,
                        completed_at=now,
                    )
                )

        for skill in self._config.skills:
            if not skill.enabled:
                continue
            cleaned, matched = self._consume_command(cleaned, "skill", skill.name)
            if matched:
                now = utc_now()
                traces.append(
                    ReplyTrace(
                        trace_id=f"skill-{skill.id}",
                        kind="skill",
                        name=skill.name,
                        status="completed",
                        summary="已将 Skill 提示注入当前轮上下文。",
                        params={
                            "description": skill.description,
                            "prompt": skill.prompt,
                        },
                        result="Mock provider 未真实执行技能，仅记录本轮注入的技能提示。",
                        started_at=now,
                        completed_at=now,
                    )
                )

        return cleaned.strip(), traces

    @staticmethod
    def _consume_command(prompt: str, prefix: str, name: str) -> tuple[str, bool]:
        pattern = re.compile(rf"/{prefix}\s+{re.escape(name)}(?=\s|$)")
        match = pattern.search(prompt)
        if not match:
            return prompt, False

        cleaned = f"{prompt[:match.start()]}{prompt[match.end():]}".strip()
        return cleaned, True

    async def reply(self, conversation_id: str, prompt: str) -> str:
        normalized, traces = self._extract_selected_capabilities(prompt)
        trace_names = ", ".join(trace.name for trace in traces) or "无"
        content = normalized or "请结合已选能力处理当前请求。"
        return (
            "openEagle 已收到你的请求。\n\n"
            f"conversationId: {conversation_id}\n"
            f"echo: {content}\n"
            f"已挂载能力: {trace_names}\n\n"
            "当前回复来自 mock provider。你可以在设置中切换到 openai 或 openai-like，并通过 Agno 驱动真实模型。"
        )

    async def stream_reply(
        self,
        conversation_id: str,
        prompt: str,
    ) -> AsyncIterator[ProviderStreamEvent]:
        _, traces = self._extract_selected_capabilities(prompt)
        for trace in traces:
            yield trace

        reply = await self.reply(conversation_id, prompt)
        chunks = [reply[index : index + 24] for index in range(0, len(reply), 24)]

        for chunk in chunks:
            if not chunk:
                continue
            await asyncio.sleep(0.05)
            yield ReplyChunk(content=chunk)
