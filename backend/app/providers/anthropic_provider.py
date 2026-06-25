from __future__ import annotations

import base64
import json
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, AsyncIterator

import anthropic
from langchain_core.tools import BaseTool

from .. import default_tools
from ..attachments import AttachmentStore
from ..config import AgentConfig, AppConfig, McpConfig, SkillConfig, ToolConfig
from ..confirmations import ToolConfirmationStore
from ..context_cleanup import compact_messages_for_prompt_with_ai
from ..memory import MemoryService
from ..models import AttachmentRef
from ..observability import trace_tool, update_observation
from ..paths import resolve_workspace_root
from ..prompts import build_chat_instructions
from ..token_usage import record_model_usage
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


def to_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(item) for item in value]
    return str(value)


def stringify_trace_result(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return json.dumps(to_jsonable(value), ensure_ascii=False, indent=2)
    except TypeError:
        return str(value)


class AnthropicAgentProvider:
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
        self._client = anthropic.AsyncAnthropic(
            api_key=config.agent.api_key,
            base_url=config.agent.base_url or None,
        )

    @property
    def _agent_config(self) -> AgentConfig:
        return self._config.agent

    async def _summarize_context(self, prompt: str) -> str:
        response = await self._client.messages.create(
            model=self._agent_config.model_id or "claude-sonnet-4-20250514",
            max_tokens=2048,
            system="你是上下文压缩器，只输出摘要正文，不输出 Markdown 栅栏。",
            messages=[{"role": "user", "content": prompt}],
        )
        await record_model_usage(
            "anthropic",
            self._agent_config.model_id or "claude-sonnet-4-20250514",
            getattr(response, "usage", None),
        )
        text_parts = [block.text for block in response.content if block.type == "text"]
        return "".join(text_parts)

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

    # ------------------------------------------------------------------
    # Capability extraction
    # ------------------------------------------------------------------

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
                traces.append(ReplyTrace(
                    trace_id=f"selected-tool-{tool.id}",
                    kind="tool",
                    name=tool.name,
                    status="completed",
                    summary="用户本轮显式选择了该工具。",
                    params={"command": tool.command, "cwd": tool.cwd, "description": tool.description},
                    result="该工具本轮已被用户点名，可优先考虑使用。",
                    started_at=now,
                    completed_at=now,
                ))

        for server in self._config.mcp:
            if not server.enabled:
                continue
            cleaned, matched = self._consume_command(cleaned, "mcp", server.name)
            if matched:
                selected_mcp.append(server)
                now = utc_now()
                traces.append(ReplyTrace(
                    trace_id=f"selected-mcp-{server.id}",
                    kind="mcp",
                    name=server.name,
                    status="completed",
                    summary="已将 MCP 能力描述注入当前轮上下文。",
                    params={"transport": server.transport, "endpoint": server.endpoint, "description": server.description},
                    result="模型已知晓该 MCP 的 transport、endpoint 和用途。",
                    started_at=now,
                    completed_at=now,
                ))

        for skill in self._config.skills:
            if not skill.enabled:
                continue
            cleaned, matched = self._consume_command(cleaned, "skill", skill.name)
            if matched:
                selected_skills.append(skill)
                now = utc_now()
                traces.append(ReplyTrace(
                    trace_id=f"selected-skill-{skill.id}",
                    kind="skill",
                    name=skill.name,
                    status="completed",
                    summary="已将 Skill 提示注入当前轮上下文。",
                    params={"description": skill.description, "prompt": skill.prompt},
                    result="模型将优先遵循该 Skill 的提示约束。",
                    started_at=now,
                    completed_at=now,
                ))

        return cleaned.strip(), selected_tools, selected_mcp, selected_skills, traces

    # ------------------------------------------------------------------
    # Tool building & conversion
    # ------------------------------------------------------------------

    def _build_tools(
        self,
        conversation_id: str,
        selected_tools: list[ToolConfig],
        selected_mcp: list[McpConfig],
        selected_skills: list[SkillConfig],
    ) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, str], list[str]]:
        instructions = build_chat_instructions(
            conversation_id=conversation_id,
            selected_tools=selected_tools,
            selected_mcp=selected_mcp,
            selected_skills=selected_skills,
        )

        default_tool_set = default_tools.build_default_tools(
            workspace_root=workspace_root(),
            confirmation_store=self._confirmation_store,
            request_id=self._request_id,
            conversation_id=self._conversation_id or conversation_id,
            permission_mode=self._config.permissions.mode,
            builtin_tools=[bt.model_dump() for bt in self._config.builtin_tools],
            web_search_config=self._config.web_search,
            attachment_store=self._attachment_store,
            memory_service=self._memory_service,
        )
        if default_tool_set.instructions:
            instructions.append(default_tool_set.instructions)
        configured_tools, configured_name_map = default_tools.build_configured_tools(
            self._config.tools,
            workspace_root=workspace_root(),
            confirmation_store=self._confirmation_store,
            request_id=self._request_id,
            conversation_id=self._conversation_id or conversation_id,
            permission_mode=self._config.permissions.mode,
        )

        function_map: dict[str, Any] = {}
        anthropic_tools: list[dict[str, Any]] = []

        for tool in default_tool_set.agent_tools:
            function_map[tool.name] = tool
            anthropic_tools.append(self._convert_tool(tool))

        for tool in configured_tools:
            function_map[tool.name] = tool
            anthropic_tools.append(self._convert_tool(tool))

        return anthropic_tools, function_map, configured_name_map, instructions

    @staticmethod
    def _convert_tool(tool: BaseTool) -> dict[str, Any]:
        if isinstance(tool.args_schema, dict):
            parameters = tool.args_schema
        elif tool.args_schema is not None:
            parameters = tool.args_schema.model_json_schema()
        else:
            parameters = {"type": "object", "properties": {}, "required": []}
        return {
            "name": tool.name,
            "description": tool.description or "",
            "input_schema": parameters,
        }

    @staticmethod
    def _execute_tool_call(
        function_map: dict[str, Any],
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> str:
        with trace_tool(tool_name, tool_input) as tool_observation:
            tool = function_map.get(tool_name)
            if tool is None:
                result_text = f"Error: tool '{tool_name}' not found"
            else:
                try:
                    result = tool.invoke(tool_input)
                    result_text = str(result) if result is not None else ""
                except Exception as exc:
                    result_text = f"Error: {exc}"
            is_error = result_text.startswith("Error:")
            update_observation(
                tool_observation,
                output=result_text,
                level="ERROR" if is_error else "DEFAULT",
                status_message=result_text if is_error else None,
            )
            return result_text

    # ------------------------------------------------------------------
    # Attachment handling
    # ------------------------------------------------------------------

    @staticmethod
    def _build_image_content(attachments: list[AttachmentRef]) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        for att in attachments:
            if att.kind != "image" or not att.local_path:
                continue
            path = Path(att.local_path)
            if not path.exists() or not path.is_file():
                continue
            try:
                data = path.read_bytes()
                encoded = base64.standard_b64encode(data).decode("ascii")
                blocks.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": att.mime_type or "image/png",
                        "data": encoded,
                    },
                })
            except Exception:
                continue
        return blocks

    # ------------------------------------------------------------------
    # reply (non-streaming)
    # ------------------------------------------------------------------

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
        anthropic_tools, function_map, _, instructions = self._build_tools(
            conversation_id, selected_tools, selected_mcp, selected_skills,
        )
        system_prompt = "\n\n".join(instructions)

        user_text = _attachment_prompt(
            cleaned_prompt or "请结合已选能力处理当前请求。", attachments or [],
        )
        user_content: list[dict[str, Any]] = []
        image_blocks = self._build_image_content(attachments or [])
        user_content.extend(image_blocks)
        user_content.append({"type": "text", "text": user_text})

        model_id = self._agent_config.model_id or "claude-sonnet-4-20250514"
        messages: list[dict[str, Any]] = [{"role": "user", "content": user_content}]

        max_rounds = 20
        for _ in range(max_rounds):
            cleanup = await compact_messages_for_prompt_with_ai(
                messages,
                self._config.context,
                system_prompt=system_prompt,
                summarizer=self._summarize_context,
                snapshot=lambda content, payload: self._record_context_snapshot(
                    conversation_id,
                    content,
                    payload,
                ),
            )
            response = await self._client.messages.create(
                model=model_id,
                max_tokens=8192,
                system=system_prompt,
                tools=anthropic_tools or anthropic.NOT_GIVEN,
                messages=cleanup.messages,
            )
            await record_model_usage(
                "anthropic",
                model_id,
                getattr(response, "usage", None),
            )

            text_parts: list[str] = []
            tool_use_blocks: list[dict[str, Any]] = []
            for block in response.content:
                if block.type == "text":
                    text_parts.append(block.text)
                elif block.type == "tool_use":
                    tool_use_blocks.append({
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    })

            if response.stop_reason != "tool_use" or not tool_use_blocks:
                return "".join(text_parts)

            # Build assistant message with all content blocks
            messages.append({"role": "assistant", "content": response.content})

            # Execute tools and build tool_result blocks
            tool_results: list[dict[str, Any]] = []
            for tub in tool_use_blocks:
                result_text = self._execute_tool_call(
                    function_map, tub["name"], tub["input"],
                )
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tub["id"],
                    "content": result_text,
                })

            messages.append({"role": "user", "content": tool_results})

        return "（达到最大工具调用轮次）"

    # ------------------------------------------------------------------
    # stream_reply (streaming)
    # ------------------------------------------------------------------

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

        anthropic_tools, function_map, configured_name_map, instructions = self._build_tools(
            conversation_id, selected_tools, selected_mcp, selected_skills,
        )
        system_prompt = "\n\n".join(instructions)

        user_text = _attachment_prompt(
            cleaned_prompt or "请结合已选能力处理当前请求。", attachments or [],
        )
        user_content: list[dict[str, Any]] = []
        image_blocks = self._build_image_content(attachments or [])
        user_content.extend(image_blocks)
        user_content.append({"type": "text", "text": user_text})

        model_id = self._agent_config.model_id or "claude-sonnet-4-20250514"
        messages: list[dict[str, Any]] = [{"role": "user", "content": user_content}]

        max_rounds = 20
        for _ in range(max_rounds):
            tool_use_blocks: list[dict[str, Any]] = []
            current_tool_id: str | None = None
            current_tool_name: str | None = None
            current_tool_input_json = ""
            assistant_content_blocks: list[dict[str, Any]] = []
            cleanup = await compact_messages_for_prompt_with_ai(
                messages,
                self._config.context,
                system_prompt=system_prompt,
                summarizer=self._summarize_context,
                snapshot=lambda content, payload: self._record_context_snapshot(
                    conversation_id,
                    content,
                    payload,
                ),
            )

            async with self._client.messages.stream(
                model=model_id,
                max_tokens=8192,
                system=system_prompt,
                tools=anthropic_tools or anthropic.NOT_GIVEN,
                messages=cleanup.messages,
            ) as stream:
                async for event in stream:
                    if event.type == "content_block_start":
                        cb = event.content_block
                        if cb.type == "tool_use":
                            current_tool_id = cb.id
                            current_tool_name = cb.name
                            current_tool_input_json = ""
                            assistant_content_blocks.append({
                                "type": "tool_use",
                                "id": cb.id,
                                "name": cb.name,
                                "input": {},
                            })
                        elif cb.type == "text":
                            assistant_content_blocks.append({"type": "text", "text": ""})

                    elif event.type == "content_block_delta":
                        if event.delta.type == "text_delta":
                            yield ReplyChunk(content=event.delta.text)
                            # Update the last text block
                            for blk in reversed(assistant_content_blocks):
                                if blk.get("type") == "text":
                                    blk["text"] += event.delta.text
                                    break
                        elif event.delta.type == "input_json_delta":
                            current_tool_input_json += event.delta.partial_json

                    elif event.type == "content_block_stop":
                        if current_tool_id is not None and current_tool_name is not None:
                            try:
                                tool_input = json.loads(current_tool_input_json) if current_tool_input_json else {}
                            except json.JSONDecodeError:
                                tool_input = {}
                            tool_use_blocks.append({
                                "type": "tool_use",
                                "id": current_tool_id,
                                "name": current_tool_name,
                                "input": tool_input,
                            })
                            # Update the assistant content block with parsed input
                            for blk in reversed(assistant_content_blocks):
                                if blk.get("type") == "tool_use" and blk.get("id") == current_tool_id:
                                    blk["input"] = tool_input
                                    break
                            current_tool_id = None
                            current_tool_name = None
                            current_tool_input_json = ""

                    elif event.type == "message_stop":
                        pass  # handled after stream exits
                get_final_message = getattr(stream, "get_final_message", None)
                if callable(get_final_message):
                    final_message = await get_final_message()
                    await record_model_usage(
                        "anthropic",
                        model_id,
                        getattr(final_message, "usage", None),
                    )

            # Stream finished for this round
            if not tool_use_blocks:
                break

            # Execute tools, yield traces
            assistant_messages_content = assistant_content_blocks
            messages.append({"role": "assistant", "content": assistant_messages_content})

            tool_results: list[dict[str, Any]] = []
            for tub in tool_use_blocks:
                display_name = configured_name_map.get(tub["name"], tub["name"])
                now = utc_now()
                yield ReplyTrace(
                    trace_id=tub["id"],
                    kind="tool",
                    name=display_name,
                    status="started",
                    summary="Agent 正在调用工具。",
                    params=to_jsonable(tub["input"]),
                    started_at=now,
                )

                result_text = self._execute_tool_call(
                    function_map, tub["name"], tub["input"],
                )
                result_text_str = str(result_text) if result_text is not None else ""

                # Check for confirmation sentinel
                if result_text_str.startswith("CONFIRMATION_REQUIRED "):
                    confirmation_id = result_text_str.split(" ", 1)[1].split(":", 1)[0].strip()
                    if confirmation_id:
                        yield ReplyToolConfirmation(confirmation_id=confirmation_id)

                yield ReplyTrace(
                    trace_id=tub["id"],
                    kind="tool",
                    name=display_name,
                    status="completed",
                    summary="Agent 已完成工具调用。",
                    params=to_jsonable(tub["input"]),
                    result=stringify_trace_result(result_text),
                    started_at=now,
                    completed_at=utc_now(),
                )

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tub["id"],
                    "content": result_text_str,
                })

            messages.append({"role": "user", "content": tool_results})
            # Continue loop for next round
