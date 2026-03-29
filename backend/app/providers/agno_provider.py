from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from typing import AsyncIterator

from agno.agent import Agent
from agno.models.openai import OpenAIResponses
from agno.models.openai.like import OpenAILike
from agno.run.agent import (
    IntermediateRunContentEvent,
    RunContentEvent,
    ToolCallCompletedEvent,
    ToolCallErrorEvent,
    ToolCallStartedEvent,
)
from .. import default_tools

from ..config import AgentConfig, AppConfig, McpConfig, SkillConfig, ToolConfig
from .base import ProviderStreamEvent, ReplyChunk, ReplyTrace


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def workspace_root() -> Path:
    return Path(__file__).resolve().parents[3]


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


class AgnoAgentProvider:
    def __init__(self, config: AppConfig) -> None:
        self._config = config

    @property
    def _agent_config(self) -> AgentConfig:
        return self._config.agent

    def _build_agent(
        self,
        conversation_id: str,
        selected_tools: list[ToolConfig],
        selected_mcp: list[McpConfig],
        selected_skills: list[SkillConfig],
    ) -> Agent:
        model_id = self._agent_config.model_id or "gpt-5-mini"

        if self._agent_config.provider == "openai-like":
            if not self._agent_config.base_url:
                raise ValueError("openai-like 模式需要配置 Base URL。")
            model = OpenAILike(
                id=model_id,
                api_key=self._agent_config.api_key,
                base_url=self._agent_config.base_url,
            )
        else:
            model = OpenAIResponses(
                id=model_id,
                api_key=self._agent_config.api_key,
            )

        instructions = [
            "你是 openEagle 的桌面 Agent 助手。",
            f"当前会话 ID: {conversation_id}",
            "回答默认使用简洁中文。",
        ]

        if selected_tools:
          instructions.append(
              "本轮可用工具：" + "；".join(
                  f"{item.name}（命令: {item.command or '未配置'}，说明: {item.description or '无'}）"
                  for item in selected_tools
              )
          )
        if selected_mcp:
          instructions.append(
              "本轮可用 MCP：" + "；".join(
                  f"{item.name}（transport: {item.transport}，endpoint: {item.endpoint or '未配置'}，说明: {item.description or '无'}）"
                  for item in selected_mcp
              )
          )
        if selected_skills:
          instructions.append(
              "本轮需遵循的 Skill：" + "；".join(
                  f"{item.name}（说明: {item.description or '无'}；提示: {item.prompt or '无'}）"
                  for item in selected_skills
              )
          )

        return Agent(
            model=model,
            markdown=True,
            instructions=instructions,
            tools=[default_tools.build_default_tools(workspace_root=workspace_root())],
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
                        summary="已将工具描述注入当前轮上下文。",
                        params={
                            "command": tool.command,
                            "description": tool.description,
                        },
                        result="模型已知晓该工具的名称、命令和说明。",
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

    async def reply(self, conversation_id: str, prompt: str) -> str:
        if not self._agent_config.api_key:
            raise ValueError("当前 provider 需要配置 API Key。")

        cleaned_prompt, selected_tools, selected_mcp, selected_skills, _ = (
            self._extract_selected_capabilities(prompt)
        )
        agent = self._build_agent(
            conversation_id,
            selected_tools=selected_tools,
            selected_mcp=selected_mcp,
            selected_skills=selected_skills,
        )
        result = await agent.arun(cleaned_prompt or "请结合已选能力处理当前请求。")
        content = getattr(result, "content", None)
        if isinstance(content, str) and content.strip():
            return content
        return str(result)

    async def stream_reply(
        self,
        conversation_id: str,
        prompt: str,
    ) -> AsyncIterator[ProviderStreamEvent]:
        if not self._agent_config.api_key:
            raise ValueError("当前 provider 需要配置 API Key。")

        cleaned_prompt, selected_tools, selected_mcp, selected_skills, selection_traces = (
            self._extract_selected_capabilities(prompt)
        )
        for trace in selection_traces:
            yield trace

        agent = self._build_agent(
            conversation_id,
            selected_tools=selected_tools,
            selected_mcp=selected_mcp,
            selected_skills=selected_skills,
        )
        stream = agent.arun(
            cleaned_prompt or "请结合已选能力处理当前请求。",
            stream=True,
            stream_events=True,
        )

        async for event in stream:
            if isinstance(event, (RunContentEvent, IntermediateRunContentEvent)):
                content = getattr(event, "content", None)
                if isinstance(content, str) and content:
                    yield ReplyChunk(content=content)
                continue

            if isinstance(event, ToolCallStartedEvent):
                tool = getattr(event, "tool", None)
                if tool is None:
                    continue
                yield ReplyTrace(
                    trace_id=tool.tool_call_id or f"tool-call-{tool.tool_name or 'unknown'}",
                    kind="tool",
                    name=tool.tool_name or "未命名工具",
                    status="started",
                    summary="Agent 正在调用工具。",
                    params=to_jsonable(tool.tool_args or {}),
                    started_at=utc_now(),
                )
                continue

            if isinstance(event, ToolCallCompletedEvent):
                tool = getattr(event, "tool", None)
                if tool is None:
                    continue
                now = utc_now()
                yield ReplyTrace(
                    trace_id=tool.tool_call_id or f"tool-call-{tool.tool_name or 'unknown'}",
                    kind="tool",
                    name=tool.tool_name or "未命名工具",
                    status="completed",
                    summary="Agent 已完成工具调用。",
                    params=to_jsonable(tool.tool_args or {}),
                    result=stringify_trace_result(tool.result),
                    started_at=now,
                    completed_at=now,
                )
                continue

            if isinstance(event, ToolCallErrorEvent):
                tool = getattr(event, "tool", None)
                now = utc_now()
                yield ReplyTrace(
                    trace_id=(tool.tool_call_id if tool else None)
                    or f"tool-call-{tool.tool_name if tool else 'unknown'}",
                    kind="tool",
                    name=tool.tool_name if tool else "未命名工具",
                    status="error",
                    summary="Agent 工具调用失败。",
                    params=to_jsonable((tool.tool_args if tool else {}) or {}),
                    result=stringify_trace_result(getattr(event, "error", None)),
                    started_at=now,
                    completed_at=now,
                )
