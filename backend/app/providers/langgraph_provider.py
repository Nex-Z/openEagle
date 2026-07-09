from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, AsyncIterator

from .. import default_tools
from ..attachments import AttachmentStore
from ..config import AgentConfig, AppConfig, McpConfig, SkillConfig, ToolConfig
from ..confirmations import ToolConfirmationStore
from ..langgraph_agent import (
    ContentChunkEvent,
    ImageBlockFormat,
    LangGraphRunResult,
    LangGraphToolAgent,
    ToolTraceEvent,
    attachment_user_content,
    run_text_model,
)
from ..memory import MemoryService
from ..models import AttachmentRef
from ..paths import resolve_workspace_root
from ..prompts import build_chat_instructions
from .base import ProviderStreamEvent, ReplyChunk, ReplyToolConfirmation, ReplyTrace


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def workspace_root() -> Path:
    return resolve_workspace_root()


def _attachment_prompt(prompt: str, attachments: list[AttachmentRef]) -> str:
    if not attachments:
        return prompt
    rows = [
        f"{index}. {item.name or item.id} ({item.kind}, {item.mime_type}, {item.size} bytes)"
        for index, item in enumerate(attachments, start=1)
    ]
    base = prompt.strip() or "请结合附件处理当前请求。"
    return f"{base}\n\n用户本轮附加了以下文件:\n" + "\n".join(rows)


def _has_image_attachment(attachments: list[AttachmentRef] | None) -> bool:
    return any(item.kind == "image" for item in attachments or [])


def _is_image_block_compat_error(exc: Exception) -> bool:
    message = str(exc).lower()
    has_image_marker = any(
        marker in message
        for marker in ("image_url", "image", "source", "base64", "vision")
    )
    has_compat_marker = any(
        marker in message
        for marker in (
            "unknown variant",
            "expected",
            "invalid_request",
            "invalid request",
            "deserialize",
            "unsupported",
            "not supported",
        )
    )
    return has_image_marker and has_compat_marker


def _image_compat_fallback_prompt(prompt: str) -> str:
    return (
        f"{prompt}\n\n"
        "注意：当前 openai-like 端点同时拒绝了 OpenAI image_url 和 Anthropic image source 图片消息，"
        "本轮无法直接读取图片像素，只能参考附件文件名和元数据。"
        "请明确说明这个限制，并建议用户切换到原生 anthropic provider、"
        "原生 openai provider，或使用支持视觉输入的 openai-like 端点后重试。"
    )


class LangGraphAgentProvider:
    def __init__(
        self,
        config: AppConfig,
        confirmation_store: ToolConfirmationStore | None = None,
        attachment_store: AttachmentStore | None = None,
        request_id: str | None = None,
        conversation_id: str | None = None,
        context_snapshot: Callable[[str, str, str, dict[str, Any]], Awaitable[None]] | None = None,
        memory_service: MemoryService | None = None,
    ) -> None:
        self._config = config
        self._confirmation_store = confirmation_store
        self._attachment_store = attachment_store
        self._request_id = request_id
        self._conversation_id = conversation_id
        self._context_snapshot = context_snapshot
        self._memory_service = memory_service

    @property
    def _agent_config(self) -> AgentConfig:
        return self._config.agent

    async def _summarize_context(self, prompt: str) -> str:
        return await run_text_model(
            agent_config=self._agent_config,
            instructions="你是上下文压缩器，只输出摘要正文，不输出 Markdown 栅栏。",
            prompt=prompt,
        )

    async def _record_context_snapshot(
        self,
        conversation_id: str,
        content: str,
        payload: dict[str, Any],
    ) -> None:
        if self._context_snapshot is None:
            return
        await self._context_snapshot(
            self._conversation_id or conversation_id,
            self._request_id or f"context-{utc_now()}",
            content,
            payload,
        )

    @staticmethod
    def _consume_command(prompt: str, prefix: str, name: str) -> tuple[str, bool]:
        pattern = re.compile(rf"/{prefix}\s+{re.escape(name)}(?=\s|$)")
        match = pattern.search(prompt)
        if not match:
            return prompt, False

        cleaned = f"{prompt[:match.start()]}{prompt[match.end():]}".strip()
        return cleaned, True

    def _extract_selected_capabilities(
        self,
        prompt: str,
    ) -> tuple[str, list[ToolConfig], list[McpConfig], list[SkillConfig], list[ReplyTrace]]:
        cleaned = prompt
        selected_tools: list[ToolConfig] = []
        selected_mcp: list[McpConfig] = []
        selected_skills: list[SkillConfig] = []
        traces: list[ReplyTrace] = []

        for tool in self._config.tools:
            if not tool.enabled:
                continue
            cleaned, matched = self._consume_command(cleaned, "tool", tool.name)
            if matched:
                selected_tools.append(tool)
                now = utc_now()
                traces.append(
                    ReplyTrace(
                        trace_id=f"selected-tool-{tool.id}",
                        kind="tool",
                        name=tool.name,
                        status="completed",
                        summary="用户本轮显式选择了该工具。",
                        params={
                            "command": tool.command,
                            "cwd": tool.cwd,
                            "description": tool.description,
                        },
                        result="该工具本轮已被用户点名，可优先考虑使用。",
                        started_at=now,
                        completed_at=now,
                    )
                )

        for server in self._config.mcp:
            if not server.enabled:
                continue
            cleaned, matched = self._consume_command(cleaned, "mcp", server.name)
            if matched:
                selected_mcp.append(server)
                now = utc_now()
                traces.append(
                    ReplyTrace(
                        trace_id=f"selected-mcp-{server.id}",
                        kind="mcp",
                        name=server.name,
                        status="completed",
                        summary="已将 MCP 能力描述注入当前轮上下文。",
                        params={
                            "transport": server.transport,
                            "endpoint": server.endpoint,
                            "description": server.description,
                        },
                        result="模型已知晓该 MCP 的 transport、endpoint 和用途。",
                        started_at=now,
                        completed_at=now,
                    )
                )

        for skill in self._config.skills:
            if not skill.enabled:
                continue
            cleaned, matched = self._consume_command(cleaned, "skill", skill.name)
            if matched:
                selected_skills.append(skill)
                now = utc_now()
                traces.append(
                    ReplyTrace(
                        trace_id=f"selected-skill-{skill.id}",
                        kind="skill",
                        name=skill.name,
                        status="completed",
                        summary="已将 Skill 提示注入当前轮上下文。",
                        params={
                            "description": skill.description,
                            "prompt": skill.prompt,
                        },
                        result="模型将优先遵循该 Skill 的提示约束。",
                        started_at=now,
                        completed_at=now,
                    )
                )

        return cleaned.strip(), selected_tools, selected_mcp, selected_skills, traces

    def _build_runner(
        self,
        conversation_id: str,
        selected_tools: list[ToolConfig],
        selected_mcp: list[McpConfig],
        selected_skills: list[SkillConfig],
        task_context: str = "",
        preserve_anthropic_image_blocks: bool = False,
    ) -> LangGraphToolAgent:
        active_workspace_root = (
            self._attachment_store.workspace_root
            if self._attachment_store is not None
            else workspace_root()
        )
        instructions = build_chat_instructions(
            conversation_id=conversation_id,
            selected_tools=selected_tools,
            selected_mcp=selected_mcp,
            selected_skills=selected_skills,
        )

        default_tool_set = default_tools.build_default_tools(
            workspace_root=active_workspace_root,
            confirmation_store=self._confirmation_store,
            request_id=self._request_id,
            conversation_id=self._conversation_id or conversation_id,
            permission_mode=self._config.permissions.mode,
            builtin_tools=[bt.model_dump() for bt in self._config.builtin_tools],
            web_search_config=self._config.web_search,
            attachment_store=self._attachment_store,
            memory_service=self._memory_service,
            task_context=task_context,
        )
        if default_tool_set.instructions:
            instructions.append(default_tool_set.instructions)
        configured_tools, configured_name_map = default_tools.build_configured_tools(
            self._config.tools,
            workspace_root=active_workspace_root,
            confirmation_store=self._confirmation_store,
            request_id=self._request_id,
            conversation_id=self._conversation_id or conversation_id,
            permission_mode=self._config.permissions.mode,
            task_context=task_context,
        )

        return LangGraphToolAgent(
            agent_config=self._agent_config,
            instructions=instructions,
            tools=[*default_tool_set.agent_tools, *configured_tools],
            tool_display_names=configured_name_map,
            context_config=self._config.context,
            summarizer=self._summarize_context,
            snapshot=lambda content, payload: self._record_context_snapshot(
                conversation_id,
                content,
                payload,
            ),
            preserve_anthropic_image_blocks=preserve_anthropic_image_blocks,
        )

    async def reply(
        self,
        conversation_id: str,
        prompt: str,
        attachments: list[AttachmentRef] | None = None,
    ) -> str:
        if not self._agent_config.api_key:
            raise ValueError("当前 provider 需要配置 API Key。")

        cleaned_prompt, selected_tools, selected_mcp, selected_skills, _ = (
            self._extract_selected_capabilities(prompt)
        )

        def build_runner(preserve_anthropic_image_blocks: bool = False) -> LangGraphToolAgent:
            return self._build_runner(
                conversation_id,
                selected_tools=selected_tools,
                selected_mcp=selected_mcp,
                selected_skills=selected_skills,
                task_context=cleaned_prompt,
                preserve_anthropic_image_blocks=preserve_anthropic_image_blocks,
            )

        runner = build_runner()
        user_prompt = _attachment_prompt(
            cleaned_prompt or "请结合已选能力处理当前请求。",
            attachments or [],
        )
        include_file_parts = self._agent_config.provider == "openai"

        def user_content(
            image_block_format: ImageBlockFormat,
            prompt_override: str | None = None,
        ) -> str | list[dict[str, Any]]:
            return attachment_user_content(
                prompt_override or user_prompt,
                attachments,
                include_file_parts=include_file_parts,
                include_image_parts=image_block_format != "none",
                image_block_format=image_block_format,
            )

        try:
            result = await runner.run(user_content("openai"))
        except Exception as exc:
            if (
                self._agent_config.provider != "openai-like"
                or not _has_image_attachment(attachments)
                or not _is_image_block_compat_error(exc)
            ):
                raise
            try:
                anthropic_runner = build_runner(preserve_anthropic_image_blocks=True)
                result = await anthropic_runner.run(user_content("anthropic"))
            except Exception as anthropic_exc:
                if not _is_image_block_compat_error(anthropic_exc):
                    raise
                fallback_runner = build_runner()
                result = await fallback_runner.run(
                    user_content("none", _image_compat_fallback_prompt(user_prompt))
                )
        return result.content

    async def stream_reply(
        self,
        conversation_id: str,
        prompt: str,
        attachments: list[AttachmentRef] | None = None,
    ) -> AsyncIterator[ProviderStreamEvent]:
        if not self._agent_config.api_key:
            raise ValueError("当前 provider 需要配置 API Key。")

        cleaned_prompt, selected_tools, selected_mcp, selected_skills, selection_traces = (
            self._extract_selected_capabilities(prompt)
        )
        for trace in selection_traces:
            yield trace

        def build_runner(preserve_anthropic_image_blocks: bool = False) -> LangGraphToolAgent:
            return self._build_runner(
                conversation_id,
                selected_tools=selected_tools,
                selected_mcp=selected_mcp,
                selected_skills=selected_skills,
                task_context=cleaned_prompt,
                preserve_anthropic_image_blocks=preserve_anthropic_image_blocks,
            )

        runner = build_runner()
        user_prompt = _attachment_prompt(
            cleaned_prompt or "请结合已选能力处理当前请求。",
            attachments or [],
        )
        emitted_content = False
        include_file_parts = self._agent_config.provider == "openai"

        def user_content(
            image_block_format: ImageBlockFormat,
            prompt_override: str | None = None,
        ) -> str | list[dict[str, Any]]:
            return attachment_user_content(
                prompt_override or user_prompt,
                attachments,
                include_file_parts=include_file_parts,
                include_image_parts=image_block_format != "none",
                image_block_format=image_block_format,
            )

        async def emit_runner_events(
            active_runner: LangGraphToolAgent,
            content: str | list[dict[str, Any]],
        ) -> AsyncIterator[ProviderStreamEvent]:
            nonlocal emitted_content
            async for event in active_runner.stream(content):
                if isinstance(event, ContentChunkEvent):
                    emitted_content = True
                    yield ReplyChunk(content=event.content)
                    continue
                if isinstance(event, ToolTraceEvent):
                    if event.status == "completed" and (event.result or "").startswith(
                        "CONFIRMATION_REQUIRED "
                    ):
                        confirmation_id = (event.result or "").split(" ", 1)[1].split(":", 1)[0].strip()
                        if confirmation_id:
                            yield ReplyToolConfirmation(confirmation_id=confirmation_id)
                    yield _reply_trace(event)
                    continue
                if isinstance(event, LangGraphRunResult) and event.content and not emitted_content:
                    yield ReplyChunk(content=event.content)

        try:
            async for event in emit_runner_events(runner, user_content("openai")):
                yield event
        except Exception as exc:
            if (
                emitted_content
                or self._agent_config.provider != "openai-like"
                or not _has_image_attachment(attachments)
                or not _is_image_block_compat_error(exc)
            ):
                raise
            yield ReplyTrace(
                trace_id=f"image-url-fallback-{utc_now()}",
                kind="agent",
                name="attachment-compat",
                status="completed",
                summary="当前 openai-like 端点不接受 OpenAI image_url 图片消息，已改用 Anthropic image source 重试。",
                params={"provider": self._agent_config.provider},
                result=str(exc),
                started_at=utc_now(),
                completed_at=utc_now(),
            )
            try:
                anthropic_runner = build_runner(preserve_anthropic_image_blocks=True)
                async for event in emit_runner_events(anthropic_runner, user_content("anthropic")):
                    yield event
            except Exception as anthropic_exc:
                if emitted_content or not _is_image_block_compat_error(anthropic_exc):
                    raise
                yield ReplyTrace(
                    trace_id=f"image-block-fallback-{utc_now()}",
                    kind="agent",
                    name="attachment-compat",
                    status="completed",
                    summary="当前 openai-like 端点也不接受 Anthropic 图片消息，已改用文本附件摘要重试。",
                    params={"provider": self._agent_config.provider},
                    result=str(anthropic_exc),
                    started_at=utc_now(),
                    completed_at=utc_now(),
                )
                fallback_runner = build_runner()
                async for event in emit_runner_events(
                    fallback_runner,
                    user_content("none", _image_compat_fallback_prompt(user_prompt))
                ):
                    yield event


def _reply_trace(trace: ToolTraceEvent) -> ReplyTrace:
    return ReplyTrace(
        trace_id=trace.trace_id,
        kind=trace.kind,
        name=trace.name,
        status=trace.status,
        summary=trace.summary,
        params=trace.params,
        result=trace.result,
        started_at=trace.started_at,
        completed_at=trace.completed_at,
    )
