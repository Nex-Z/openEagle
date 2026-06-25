from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.messages import convert_to_openai_messages
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.config import get_stream_writer
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode

from .config import AgentConfig, ContextConfig
from .context_cleanup import compact_messages_for_prompt_with_ai
from .models import AttachmentRef
from .observability import (
    AsyncOpenAI,
    openai_observation_kwargs,
    trace_tool,
    update_observation,
)
from .token_usage import record_model_usage

MAX_TOOL_ROUNDS = 100
MAX_INLINE_ATTACHMENT_CHARS = 24_000
MAX_INLINE_ATTACHMENT_TOTAL_CHARS = 48_000
ImageBlockFormat = Literal["openai", "anthropic", "none"]
TEXT_ATTACHMENT_EXTENSIONS = {
    ".css",
    ".csv",
    ".html",
    ".ini",
    ".js",
    ".jsx",
    ".json",
    ".log",
    ".md",
    ".py",
    ".sql",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}


@dataclass
class ToolTraceEvent:
    trace_id: str
    name: str
    status: str
    summary: str
    params: dict[str, Any] = field(default_factory=dict)
    result: str | None = None
    started_at: str = ""
    completed_at: str | None = None
    kind: str = "tool"


@dataclass
class ToolConfirmationEvent:
    confirmation_id: str


@dataclass
class LangGraphRunResult:
    content: str
    traces: list[ToolTraceEvent] = field(default_factory=list)
    confirmations: list[ToolConfirmationEvent] = field(default_factory=list)


@dataclass
class ContentChunkEvent:
    content: str


class AgentGraphState(TypedDict):
    messages: list[BaseMessage]
    last_tool_calls: list[dict[str, Any]]
    content: str
    traces: list[ToolTraceEvent]
    confirmations: list[ToolConfirmationEvent]
    rounds: int
    tool_rounds: int
    force_final: bool
    stream_model: bool


ContextSnapshot = Callable[[str, dict[str, Any]], Awaitable[None]]
ContextSummarizer = Callable[[str], Awaitable[str]]


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def to_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return to_jsonable(model_dump())
        except Exception:
            pass
    return str(value)


def stringify_tool_result(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(to_jsonable(value), ensure_ascii=False, indent=2)
    except TypeError:
        return str(value)


def _openai_client(agent_config: AgentConfig, *, vision: bool = False) -> AsyncOpenAI:
    provider = agent_config.vl_provider if vision else agent_config.provider
    api_key = agent_config.vl_api_key if vision else agent_config.api_key
    base_url = agent_config.vl_base_url if vision else agent_config.base_url
    if not api_key:
        raise ValueError("当前 provider 需要配置 API Key。")
    if provider == "openai-like":
        if not base_url:
            raise ValueError("openai-like 模式需要配置 Base URL。")
        return AsyncOpenAI(api_key=api_key, base_url=base_url)
    return AsyncOpenAI(api_key=api_key)


def _model_id(agent_config: AgentConfig, *, vision: bool = False) -> str:
    if vision:
        return agent_config.vl_model_id or "gpt-4.1-mini"
    return agent_config.model_id or "gpt-5-mini"


def encode_file_data_url(path: Path, mime_type: str) -> str:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _anthropic_image_block_from_data_url(data_url: str) -> dict[str, Any] | None:
    if not data_url.startswith("data:") or ";base64," not in data_url:
        return None
    media_type, encoded = data_url[5:].split(";base64,", 1)
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type or "image/png",
            "data": encoded,
        },
    }


def _restore_anthropic_image_content(content: Any) -> Any:
    if not isinstance(content, list):
        return content
    restored_blocks: list[Any] = []
    changed = False
    for block in content:
        if isinstance(block, dict) and block.get("type") == "image_url":
            image_url = block.get("image_url")
            url = image_url.get("url") if isinstance(image_url, dict) else image_url
            if isinstance(url, str):
                restored = _anthropic_image_block_from_data_url(url)
                if restored is not None:
                    restored_blocks.append(restored)
                    changed = True
                    continue
        restored_blocks.append(block)
    return restored_blocks if changed else content


def _restore_anthropic_image_blocks(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    restored: list[dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        next_content = _restore_anthropic_image_content(content)
        restored.append({**message, "content": next_content} if next_content is not content else message)
    return restored


def _convert_messages_for_chat(
    messages: list[BaseMessage],
    *,
    preserve_anthropic_image_blocks: bool,
) -> list[dict[str, Any]]:
    converted = list(convert_to_openai_messages(messages))
    if preserve_anthropic_image_blocks:
        return _restore_anthropic_image_blocks(converted)
    return converted


def _attachment_path(attachment: AttachmentRef) -> Path | None:
    if not attachment.local_path or attachment.status == "error":
        return None
    path = Path(attachment.local_path)
    if not path.exists() or not path.is_file():
        return None
    return path


def _attachment_name(attachment: AttachmentRef, path: Path) -> str:
    return attachment.name or path.name or attachment.id


def _is_text_attachment(attachment: AttachmentRef, path: Path) -> bool:
    mime_type = (attachment.mime_type or "").lower()
    return mime_type.startswith("text/") or path.suffix.lower() in TEXT_ATTACHMENT_EXTENSIONS


def _decode_attachment_text(path: Path) -> str | None:
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def _attachment_file_part(attachment: AttachmentRef, path: Path) -> dict[str, Any]:
    return {
        "type": "file",
        "file": {
            "filename": _attachment_name(attachment, path),
            "file_data": base64.b64encode(path.read_bytes()).decode("ascii"),
        },
    }


def _attachment_image_part(
    attachment: AttachmentRef,
    path: Path,
    image_block_format: ImageBlockFormat,
) -> dict[str, Any] | None:
    mime_type = attachment.mime_type or "image/png"
    if image_block_format == "none":
        return None
    if image_block_format == "anthropic":
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": mime_type,
                "data": base64.standard_b64encode(path.read_bytes()).decode("ascii"),
            },
        }
    return {
        "type": "image_url",
        "image_url": {
            "url": encode_file_data_url(path, mime_type),
            "detail": "auto",
        },
    }


def _inline_attachment_text(attachments: list[AttachmentRef] | None) -> str:
    remaining_chars = MAX_INLINE_ATTACHMENT_TOTAL_CHARS
    blocks: list[str] = []
    for attachment in attachments or []:
        if attachment.kind == "image":
            continue
        path = _attachment_path(attachment)
        if path is None:
            continue
        name = _attachment_name(attachment, path)
        header = f"--- {name} ({attachment.mime_type}, {attachment.size} bytes) ---"
        if not _is_text_attachment(attachment, path):
            blocks.append(
                f"{header}\n[该附件是二进制或当前不支持直接文本读取的文件，当前仅可参考附件元数据。]"
            )
            continue

        text = _decode_attachment_text(path)
        if text is None:
            blocks.append(f"{header}\n[该附件无法按文本解码，当前仅可参考附件元数据。]")
            continue
        if remaining_chars <= 0:
            blocks.append(f"{header}\n[附件内容已因长度限制省略。]")
            continue

        limit = min(MAX_INLINE_ATTACHMENT_CHARS, remaining_chars)
        excerpt = text[:limit]
        remaining_chars -= len(excerpt)
        if len(text) > len(excerpt):
            excerpt = f"{excerpt}\n[附件内容已截断，原始长度 {len(text)} 字符。]"
        blocks.append(f"{header}\n{excerpt}")

    if not blocks:
        return ""
    return "附件内容摘录:\n" + "\n\n".join(blocks)


def attachment_user_content(
    prompt: str,
    attachments: list[AttachmentRef] | None,
    *,
    include_file_parts: bool = True,
    include_image_parts: bool = True,
    image_block_format: ImageBlockFormat = "openai",
) -> str | list[dict[str, Any]]:
    inline_text = "" if include_file_parts else _inline_attachment_text(attachments)
    text_prompt = f"{prompt}\n\n{inline_text}" if inline_text else prompt
    image_blocks: list[dict[str, Any]] = []
    file_blocks: list[dict[str, Any]] = []
    for attachment in attachments or []:
        path = _attachment_path(attachment)
        if path is None:
            continue
        if attachment.kind == "image":
            if not include_image_parts or image_block_format == "none":
                continue
            image_part = _attachment_image_part(attachment, path, image_block_format)
            if image_part is not None:
                image_blocks.append(image_part)
        elif include_file_parts:
            file_blocks.append(_attachment_file_part(attachment, path))
    if not image_blocks and not file_blocks:
        return text_prompt
    return [{"type": "text", "text": text_prompt}, *image_blocks, *file_blocks]


def image_user_content(prompt: str, image_url: str) -> list[dict[str, Any]]:
    return [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": image_url, "detail": "auto"}},
    ]


class LangGraphToolAgent:
    def __init__(
        self,
        *,
        agent_config: AgentConfig,
        instructions: list[str] | str,
        tools: list[BaseTool] | None = None,
        tool_display_names: dict[str, str] | None = None,
        context_config: ContextConfig | None = None,
        summarizer: ContextSummarizer | None = None,
        snapshot: ContextSnapshot | None = None,
        vision: bool = False,
        preserve_anthropic_image_blocks: bool = False,
        max_tool_rounds: int = MAX_TOOL_ROUNDS,
    ) -> None:
        self.agent_config = agent_config
        self.system_prompt = "\n\n".join(instructions) if isinstance(instructions, list) else instructions
        self.tools = list(tools or [])
        self._tool_node = ToolNode(self.tools)
        self.tool_display_names = tool_display_names or {}
        self.context_config = context_config
        self.summarizer = summarizer
        self.snapshot = snapshot
        self.vision = vision
        self.preserve_anthropic_image_blocks = preserve_anthropic_image_blocks
        self.max_tool_rounds = max(1, max_tool_rounds)
        self.client = _openai_client(agent_config, vision=vision)
        self.model_id = _model_id(agent_config, vision=vision)
        self.graph = self._build_graph()

    def _build_graph(self) -> Any:
        graph = StateGraph(AgentGraphState)
        graph.add_node("model", self._model_node)
        graph.add_node("tools", self._tools_node)
        graph.add_node("final", self._final_node)
        graph.set_entry_point("model")
        graph.add_conditional_edges(
            "model",
            self._route_from_model,
            {"tools": "tools", "final": "final", "end": END},
        )
        graph.add_conditional_edges(
            "tools",
            self._route_from_tools,
            {"model": "model", "final": "final"},
        )
        graph.add_edge("final", END)
        return graph.compile()

    async def run(self, user_content: str | list[dict[str, Any]]) -> LangGraphRunResult:
        initial: AgentGraphState = {
            "messages": [
                SystemMessage(content=self.system_prompt),
                HumanMessage(content=user_content),
            ],
            "last_tool_calls": [],
            "content": "",
            "traces": [],
            "confirmations": [],
            "rounds": 0,
            "tool_rounds": 0,
            "force_final": False,
            "stream_model": False,
        }
        state = await self.graph.ainvoke(initial)
        return LangGraphRunResult(
            content=str(state.get("content") or "").strip(),
            traces=list(state.get("traces") or []),
            confirmations=list(state.get("confirmations") or []),
        )

    async def stream(
        self,
        user_content: str | list[dict[str, Any]],
        *,
        stream_model: bool = True,
    ) -> AsyncIterator[ContentChunkEvent | ToolTraceEvent | LangGraphRunResult]:
        initial: AgentGraphState = {
            "messages": [
                SystemMessage(content=self.system_prompt),
                HumanMessage(content=user_content),
            ],
            "last_tool_calls": [],
            "content": "",
            "traces": [],
            "confirmations": [],
            "rounds": 0,
            "tool_rounds": 0,
            "force_final": False,
            "stream_model": stream_model,
        }
        final_state: AgentGraphState | None = None
        async for mode, chunk in self.graph.astream(initial, stream_mode=["custom", "values"]):
            if mode == "custom":
                if isinstance(chunk, (ContentChunkEvent, ToolTraceEvent)):
                    yield chunk
                continue
            if mode == "values":
                final_state = chunk

        final_state = final_state or initial
        yield LangGraphRunResult(
            content=str(final_state.get("content") or "").strip(),
            traces=list(final_state.get("traces") or []),
            confirmations=list(final_state.get("confirmations") or []),
        )

    async def _model_node(self, state: AgentGraphState) -> AgentGraphState:
        messages = list(state["messages"])
        openai_messages = _convert_messages_for_chat(
            messages,
            preserve_anthropic_image_blocks=self.preserve_anthropic_image_blocks,
        )
        if self.context_config is not None:
            cleanup = await compact_messages_for_prompt_with_ai(
                openai_messages,
                self.context_config,
                system_prompt=None,
                summarizer=self.summarizer,
                snapshot=self.snapshot,
            )
            openai_messages = cleanup.messages
            if self.preserve_anthropic_image_blocks:
                openai_messages = _restore_anthropic_image_blocks(openai_messages)

        request: dict[str, Any] = {
            "model": self.model_id,
            "messages": openai_messages,
        }
        tool_schemas = [convert_to_openai_tool(tool) for tool in self.tools]
        if tool_schemas:
            request["tools"] = tool_schemas
        request.update(
            openai_observation_kwargs(
                "agent-model",
                metadata={
                    "provider": (
                        self.agent_config.vl_provider
                        if self.vision
                        else self.agent_config.provider
                    ),
                    "vision": self.vision,
                    "toolCount": len(tool_schemas),
                },
            )
        )
        if state.get("stream_model"):
            content, tool_calls = await self._stream_chat_completion(request)
        else:
            response = await self.client.chat.completions.create(**request)
            await record_model_usage(
                self.agent_config.vl_provider if self.vision else self.agent_config.provider,
                self.model_id,
                getattr(response, "usage", None),
            )
            message = response.choices[0].message
            content = message.content or ""
            tool_calls = []
            if message.tool_calls:
                for tool_call in message.tool_calls:
                    arguments = _parse_tool_arguments(tool_call.function.arguments)
                    tool_calls.append(
                        {
                            "id": tool_call.id,
                            "name": tool_call.function.name,
                            "args": arguments,
                            "type": "tool_call",
                        }
                    )
        assistant_message = AIMessage(content=content, tool_calls=tool_calls)

        return {
            **state,
            "messages": [*messages, assistant_message],
            "last_tool_calls": tool_calls,
            "content": content,
            "rounds": state["rounds"] + 1,
        }

    async def _stream_chat_completion(self, request: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        try:
            stream = await self.client.chat.completions.create(
                **request,
                stream=True,
                stream_options={"include_usage": True},
            )
        except Exception as exc:
            if "stream_options" not in str(exc).lower():
                raise
            stream = await self.client.chat.completions.create(**request, stream=True)
        content_parts: list[str] = []
        tool_call_chunks: dict[int, dict[str, Any]] = {}
        async for chunk in stream:
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                await record_model_usage(
                    self.agent_config.vl_provider if self.vision else self.agent_config.provider,
                    self.model_id,
                    usage,
                )
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            delta_content = getattr(delta, "content", None)
            if delta_content:
                content_parts.append(delta_content)
                self._write_stream_event(ContentChunkEvent(content=delta_content))

            for tool_call in getattr(delta, "tool_calls", None) or []:
                index = int(getattr(tool_call, "index", len(tool_call_chunks)) or 0)
                current = tool_call_chunks.setdefault(
                    index,
                    {"id": "", "name_parts": [], "argument_parts": []},
                )
                call_id = getattr(tool_call, "id", None)
                if call_id:
                    current["id"] = call_id
                function = getattr(tool_call, "function", None)
                if function is None:
                    continue
                name = getattr(function, "name", None)
                if name:
                    current["name_parts"].append(name)
                arguments = getattr(function, "arguments", None)
                if arguments:
                    current["argument_parts"].append(arguments)

        tool_calls: list[dict[str, Any]] = []
        for index in sorted(tool_call_chunks):
            current = tool_call_chunks[index]
            name = "".join(current["name_parts"])
            if not name:
                continue
            tool_calls.append(
                {
                    "id": current["id"] or f"tool-call-{index + 1}",
                    "name": name,
                    "args": _parse_tool_arguments("".join(current["argument_parts"])),
                    "type": "tool_call",
                }
            )
        return "".join(content_parts), tool_calls

    async def _tools_node(self, state: AgentGraphState) -> AgentGraphState:
        messages = list(state["messages"])
        traces = list(state["traces"])
        confirmations = list(state["confirmations"])
        tool_messages: list[ToolMessage] = []
        tool_calls = list(state["last_tool_calls"])

        for tool_call in tool_calls:
            call_id = str(tool_call.get("id") or f"tool-call-{len(traces) + 1}")
            name = str(tool_call.get("name") or "unknown")
            arguments = tool_call.get("args")
            if not isinstance(arguments, dict):
                arguments = {}
            display_name = self.tool_display_names.get(name, name)
            started_at = utc_now()
            traces.append(
                ToolTraceEvent(
                    trace_id=call_id,
                    kind="mcp" if name.startswith("mcp_") else "tool",
                    name=display_name,
                    status="started",
                    summary="Agent 正在调用工具。",
                    params=to_jsonable(arguments),
                    started_at=started_at,
                )
            )
            self._write_stream_event(traces[-1])
            kind = "mcp" if name.startswith("mcp_") else "tool"
            with trace_tool(display_name, arguments, kind=kind) as tool_observation:
                tool_message = await self._invoke_single_tool(messages, tool_call)
                if tool_message is not None:
                    tool_messages.append(tool_message)
                if tool_message is None:
                    result_text = f"Error: tool '{name}' did not return a result"
                    status = "error"
                else:
                    result_text = stringify_tool_result(tool_message.content)
                    status = "error" if result_text.startswith("Error:") else "completed"
                update_observation(
                    tool_observation,
                    output=result_text,
                    level="ERROR" if status == "error" else "DEFAULT",
                    status_message=result_text if status == "error" else None,
                )
            if result_text.startswith("CONFIRMATION_REQUIRED "):
                confirmation_id = result_text.split(" ", 1)[1].split(":", 1)[0].strip()
                if confirmation_id:
                    confirmations.append(ToolConfirmationEvent(confirmation_id=confirmation_id))
            traces.append(
                ToolTraceEvent(
                    trace_id=call_id,
                    kind="mcp" if name.startswith("mcp_") else "tool",
                    name=display_name,
                    status=status,
                    summary="Agent 已完成工具调用。" if status == "completed" else "Agent 工具调用失败。",
                    params=to_jsonable(arguments),
                    result=result_text,
                    started_at=started_at,
                    completed_at=utc_now(),
                )
            )
            self._write_stream_event(traces[-1])
        return {
            **state,
            "messages": [*messages, *tool_messages],
            "last_tool_calls": [],
            "traces": traces,
            "confirmations": confirmations,
            "tool_rounds": state["tool_rounds"] + 1,
            "force_final": False,
        }

    async def _final_node(self, state: AgentGraphState) -> AgentGraphState:
        messages = self._messages_for_final_answer(list(state["messages"]))
        openai_messages = _convert_messages_for_chat(
            messages,
            preserve_anthropic_image_blocks=self.preserve_anthropic_image_blocks,
        )
        request: dict[str, Any] = {
            "model": self.model_id,
            "messages": openai_messages,
        }
        request.update(
            openai_observation_kwargs(
                "agent-final",
                metadata={
                    "provider": (
                        self.agent_config.vl_provider
                        if self.vision
                        else self.agent_config.provider
                    ),
                    "vision": self.vision,
                },
            )
        )
        if state.get("stream_model"):
            content, _ = await self._stream_chat_completion(request)
        else:
            response = await self.client.chat.completions.create(**request)
            await record_model_usage(
                self.agent_config.vl_provider if self.vision else self.agent_config.provider,
                self.model_id,
                getattr(response, "usage", None),
            )
            content = response.choices[0].message.content or ""
        content = content.strip() or self._fallback_final_content(state)
        trace = ToolTraceEvent(
            trace_id=f"finalize-{state['rounds']}-{state['tool_rounds']}",
            kind="agent",
            name="finalize_answer",
            status="completed",
            summary="已停止继续调用工具，并基于现有结果生成最终回复。",
            params={
                "rounds": state["rounds"],
                "toolRounds": state["tool_rounds"],
                "traceCount": len(state["traces"]),
            },
            started_at=utc_now(),
            completed_at=utc_now(),
        )
        self._write_stream_event(trace)
        return {
            **state,
            "messages": [*messages, AIMessage(content=content)],
            "last_tool_calls": [],
            "content": content,
            "traces": [*state["traces"], trace],
            "force_final": False,
        }

    def _route_from_model(self, state: AgentGraphState) -> str:
        if state["force_final"]:
            return "final"
        if state["last_tool_calls"] and state["tool_rounds"] < self.max_tool_rounds:
            return "tools"
        if state["last_tool_calls"]:
            return "final"
        if state["traces"] and not str(state.get("content") or "").strip():
            return "final"
        return "end"

    @staticmethod
    def _route_from_tools(state: AgentGraphState) -> str:
        return "final" if state["force_final"] else "model"

    @staticmethod
    def _messages_for_final_answer(messages: list[BaseMessage]) -> list[BaseMessage]:
        if messages and isinstance(messages[-1], AIMessage) and messages[-1].tool_calls:
            messages = messages[:-1]
        final_instruction = (
            "停止继续调用工具。不要再提出新的工具调用。"
            "请只基于上文用户请求、已经得到的工具观察和可确定事实，给出面向用户的最终答复。"
            "如果已有工具结果不足以完成任务，要明确说明已经得到什么、缺什么、下一步建议是什么。"
            "不要留空，不要只说执行结束。"
        )
        if messages and isinstance(messages[0], SystemMessage):
            return [
                SystemMessage(content=f"{messages[0].content}\n\n{final_instruction}"),
                *messages[1:],
            ]
        return [SystemMessage(content=final_instruction), *messages]

    @staticmethod
    def _fallback_final_content(state: AgentGraphState) -> str:
        trace_count = len(state.get("traces") or [])
        if trace_count:
            return (
                f"我已经停止继续调用工具。前面共产生了 {trace_count} 条工具记录，"
                "但模型没有生成可用的最终结论；请缩小目标或让我按更明确的步骤重新执行。"
            )
        return "我这轮没有生成可用回复。请再补充一句你的目标，我会重新处理。"

    async def _invoke_single_tool(
        self,
        messages: list[BaseMessage],
        tool_call: dict[str, Any],
    ) -> ToolMessage | None:
        tool_input = {
            "messages": [
                *messages[:-1],
                AIMessage(content="", tool_calls=[tool_call]),
            ]
        }
        tool_output = await self._tool_node.ainvoke(tool_input)
        for message in tool_output.get("messages", []):
            if isinstance(message, ToolMessage) and message.tool_call_id == tool_call.get("id"):
                return message
        return None

    @staticmethod
    def _write_stream_event(event: ContentChunkEvent | ToolTraceEvent) -> None:
        with contextlib.suppress(RuntimeError, LookupError):
            get_stream_writer()(event)


def _parse_tool_arguments(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


async def run_text_model(
    *,
    agent_config: AgentConfig,
    instructions: list[str] | str,
    prompt: str,
) -> str:
    runner = LangGraphToolAgent(
        agent_config=agent_config,
        instructions=instructions,
        tools=[],
    )
    result = await runner.run(prompt)
    return result.content
