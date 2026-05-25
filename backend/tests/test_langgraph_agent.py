from __future__ import annotations

import asyncio
import base64
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from langchain_core.tools import StructuredTool

from app.config import AgentConfig, ContextConfig
from app.langgraph_agent import (
    LangGraphRunResult,
    LangGraphToolAgent,
    ToolTraceEvent,
    attachment_user_content,
)
from app.models import AttachmentRef


class _FakeFunction:
    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, call_id: str, name: str, arguments: str) -> None:
        self.id = call_id
        self.function = _FakeFunction(name, arguments)


class _FakeMessage:
    def __init__(self, content: str, tool_calls: list[_FakeToolCall] | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls or []


class _FakeChoice:
    def __init__(self, message: _FakeMessage) -> None:
        self.message = message


class _FakeResponse:
    def __init__(self, message: _FakeMessage) -> None:
        self.choices = [_FakeChoice(message)]


class _FakeCompletions:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    async def create(self, **request: Any) -> _FakeResponse:
        self.requests.append(request)
        if len(self.requests) == 1:
            return _FakeResponse(
                _FakeMessage(
                    "",
                    [_FakeToolCall("call-1", "echo", '{"text":"hi"}')],
                )
            )
        return _FakeResponse(_FakeMessage("done"))


class _FakeChat:
    def __init__(self) -> None:
        self.completions = _FakeCompletions()


class _FakeClient:
    def __init__(self) -> None:
        self.chat = _FakeChat()


class _FakeTextCompletions:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    async def create(self, **request: Any) -> _FakeResponse:
        self.requests.append(request)
        return _FakeResponse(_FakeMessage("done"))


class _FakeTextChat:
    def __init__(self) -> None:
        self.completions = _FakeTextCompletions()


class _FakeTextClient:
    def __init__(self) -> None:
        self.chat = _FakeTextChat()


class LangGraphAgentTest(unittest.TestCase):
    def test_attachment_user_content_includes_file_part_for_non_image(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "note.txt"
            path.write_text("hello attachment", encoding="utf-8")
            attachment = AttachmentRef(
                name="note.txt",
                mimeType="text/plain",
                size=path.stat().st_size,
                kind="file",
                localPath=str(path),
            )

            content = attachment_user_content("read this", [attachment])

        self.assertIsInstance(content, list)
        file_block = next(block for block in content if block["type"] == "file")
        self.assertEqual(file_block["file"]["filename"], "note.txt")
        self.assertEqual(
            base64.b64decode(file_block["file"]["file_data"]).decode("utf-8"),
            "hello attachment",
        )

    def test_attachment_user_content_inlines_text_when_file_parts_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "note.txt"
            path.write_text("hello attachment", encoding="utf-8")
            attachment = AttachmentRef(
                name="note.txt",
                mimeType="text/plain",
                size=path.stat().st_size,
                kind="file",
                localPath=str(path),
            )

            content = attachment_user_content(
                "read this",
                [attachment],
                include_file_parts=False,
            )

        self.assertIsInstance(content, str)
        self.assertIn("附件内容摘录", content)
        self.assertIn("hello attachment", content)

    def test_structured_tool_uses_langchain_core_schema(self) -> None:
        def echo(text: str, limit: int = 3) -> str:
            """Echo text."""
            return text[:limit]

        tool = StructuredTool.from_function(func=echo, name="echo", description="Echo text.")

        self.assertEqual(tool.name, "echo")
        self.assertEqual(tool.args["text"]["type"], "string")
        self.assertEqual(tool.args["limit"]["type"], "integer")

    def test_langgraph_runner_executes_tool_and_returns_final_content(self) -> None:
        def echo(text: str) -> str:
            return f"echo:{text}"
        tool = StructuredTool.from_function(func=echo, name="echo", description="Echo")

        runner = LangGraphToolAgent(
            agent_config=AgentConfig(provider="openai", api_key="test-key"),
            instructions="system",
            tools=[tool],
        )
        fake_client = _FakeClient()
        runner.client = fake_client

        result = asyncio.run(runner.run("hello"))

        self.assertEqual(result.content, "done")
        self.assertEqual([trace.status for trace in result.traces], ["started", "completed"])
        self.assertEqual(result.traces[-1].result, "echo:hi")
        second_messages = fake_client.chat.completions.requests[1]["messages"]
        self.assertTrue(any(message.get("role") == "tool" for message in second_messages))

    def test_context_cleanup_does_not_count_system_prompt_twice(self) -> None:
        captured: dict[str, Any] = {}

        async def fake_cleanup(messages: list[dict[str, Any]], config: ContextConfig, **kwargs: Any) -> Any:
            captured["system_prompt"] = kwargs.get("system_prompt")
            captured["roles"] = [message.get("role") for message in messages]
            return SimpleNamespace(messages=messages)

        runner = LangGraphToolAgent(
            agent_config=AgentConfig(provider="openai", api_key="test-key"),
            instructions="system",
            tools=[],
            context_config=ContextConfig(enabled=True),
        )
        runner.client = _FakeTextClient()

        with patch("app.langgraph_agent.compact_messages_for_prompt_with_ai", fake_cleanup):
            result = asyncio.run(runner.run("hello"))

        self.assertEqual(result.content, "done")
        self.assertIsNone(captured["system_prompt"])
        self.assertIn("system", captured["roles"])

    def test_langgraph_stream_emits_tool_traces_before_final_result(self) -> None:
        def echo(text: str) -> str:
            return f"echo:{text}"
        tool = StructuredTool.from_function(func=echo, name="echo", description="Echo")

        runner = LangGraphToolAgent(
            agent_config=AgentConfig(provider="openai", api_key="test-key"),
            instructions="system",
            tools=[tool],
        )
        runner.client = _FakeClient()

        async def collect() -> list[object]:
            return [event async for event in runner.stream("hello", stream_model=False)]

        events = asyncio.run(collect())

        self.assertIsInstance(events[0], ToolTraceEvent)
        self.assertEqual([event.status for event in events[:2]], ["started", "completed"])
        self.assertIsInstance(events[-1], LangGraphRunResult)
        self.assertEqual(events[-1].content, "done")


if __name__ == "__main__":
    unittest.main()
