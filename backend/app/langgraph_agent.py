from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.messages import convert_to_openai_messages
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.config import get_stream_writer
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode
from openai import AsyncOpenAI

from .config import AgentConfig, ContextConfig
from .context_cleanup import compact_messages_for_prompt_with_ai
from .models import AttachmentRef

MAX_TOOL_ROUNDS = 20
MAX_INLINE_ATTACHMENT_CHARS = 24_000
MAX_INLINE_ATTACHMENT_TOTAL_CHARS = 48_000
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
            image_blocks.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": encode_file_data_url(path, attachment.mime_type or "image/png"),
                        "detail": "auto",
                    },
                }
            )
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
        self.client = _openai_client(agent_config, vision=vision)
        self.model_id = _model_id(agent_config, vision=vision)
        self.graph = self._build_graph()

    def _build_graph(self) -> Any:
        graph = StateGraph(AgentGraphState)
        graph.add_node("model", self._model_node)
        graph.add_node("tools", self._tools_node)
        graph.set_entry_point("model")
        graph.add_conditional_edges(
            "model",
            self._route_from_model,
            {"tools": "tools", "end": END},
        )
        graph.add_edge("tools", "model")
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
        openai_messages = list(convert_to_openai_messages(messages))
        if self.context_config is not None:
            cleanup = await compact_messages_for_prompt_with_ai(
                openai_messages,
                self.context_config,
                system_prompt=None,
                summarizer=self.summarizer,
                snapshot=self.snapshot,
            )
            openai_messages = cleanup.messages

        request: dict[str, Any] = {
            "model": self.model_id,
            "messages": openai_messages,
        }
        tool_schemas = [convert_to_openai_tool(tool) for tool in self.tools]
        if tool_schemas:
            request["tools"] = tool_schemas
        if state.get("stream_model"):
            content, tool_calls = await self._stream_chat_completion(request)
        else:
            response = await self.client.chat.completions.create(**request)
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
        stream = await self.client.chat.completions.create(**request, stream=True)
        content_parts: list[str] = []
        tool_call_chunks: dict[int, dict[str, Any]] = {}
        async for chunk in stream:
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

        for tool_call in state["last_tool_calls"]:
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
            tool_message = await self._invoke_single_tool(messages, tool_call)
            if tool_message is not None:
                tool_messages.append(tool_message)
            if tool_message is None:
                result_text = f"Error: tool '{name}' did not return a result"
                status = "error"
            else:
                result_text = stringify_tool_result(tool_message.content)
                status = "error" if result_text.startswith("Error:") else "completed"
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
        }

    @staticmethod
    def _route_from_model(state: AgentGraphState) -> str:
        if state["last_tool_calls"] and state["rounds"] < MAX_TOOL_ROUNDS:
            return "tools"
        return "end"

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
