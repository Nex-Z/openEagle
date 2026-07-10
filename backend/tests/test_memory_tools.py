from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from app.agent_router import AgentRouter
from app.agent_runtime import AgentRuntime
from app.attachments import AttachmentStore
from app.config import AppConfig
from app.confirmations import ToolConfirmationStore
from app.default_tools import build_default_tools
from app.memory import MemoryService, init_db
from app.subagent_models import AgentRouteDecision


class MemoryToolTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name) / "workspace"
        self.workspace.mkdir()
        init_db(self.workspace / ".open-eagle" / "memory.db")
        self.service = MemoryService()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_default_tools_save_update_delete_memory_without_files(self) -> None:
        tools = build_default_tools(
            workspace_root=self.workspace,
            memory_service=self.service,
        )

        result = tools.save_memory_note(
            "仙逆：每周一更新",
            tags=["anime"],
            confidence=0.9,
        )
        state_payload = json.loads(tools.get_memory_state())
        note = self.service.state().notes[0]
        self.assertIn("已保存到长期记忆用户笔记", result)
        self.assertEqual(state_payload["notes"][0]["id"], note.id)
        self.assertEqual(note.text, "仙逆：每周一更新")
        self.assertEqual(note.tags, ["anime"])
        self.assertFalse((self.workspace / "anime_schedule.txt").exists())

        update_result = tools.update_memory_note(
            note.id,
            text="仙逆：每周一晚更新",
            tags=["anime", "schedule"],
        )
        updated = self.service.state().notes[0]
        self.assertIn("已更新长期记忆用户笔记", update_result)
        self.assertEqual(updated.text, "仙逆：每周一晚更新")
        self.assertEqual(updated.tags, ["anime", "schedule"])

        delete_result = tools.delete_memory_note(note.id, reason="测试删除")
        archived = self.service.state().notes[0]
        self.assertIn("已删除长期记忆用户笔记", delete_result)
        self.assertEqual(archived.status, "archived")

    def test_default_tools_save_profile_soul_and_side_notes(self) -> None:
        tools = build_default_tools(
            workspace_root=self.workspace,
            memory_service=self.service,
        )

        self.assertIn("已保存用户画像", tools.save_user_profile("用户喜欢简洁中文。"))
        self.assertIn("已保存 Soul core", tools.save_soul_core("# SOUL.md\n直接行动。"))
        self.assertIn("已保存 Agent 旁注", tools.save_agent_side_notes("少说套话。"))

        state = self.service.state()
        self.assertIn("简洁中文", state.profile.content)
        self.assertIn("直接行动", state.agent_soul.core)
        self.assertIn("少说套话", state.agent_soul.side_notes)


class AgentRuntimeMemoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name) / "workspace"
        self.workspace.mkdir()
        init_db(self.workspace / ".open-eagle" / "memory.db")
        self.service = MemoryService()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_learning_trigger_only_accepts_corrections_and_durable_preferences(self) -> None:
        self.assertIsNone(AgentRuntime._learning_trigger("你好，帮我看看这个文件。"))
        self.assertEqual(
            AgentRuntime._learning_trigger("刚才不对，以后不要用英文回答。"),
            "user_correction",
        )
        self.assertEqual(
            AgentRuntime._learning_trigger("我喜欢简洁的中文回复。"),
            "durable_preference",
        )

    def test_explicit_memory_request_is_saved_without_router_or_file(self) -> None:
        events: list[tuple[str, str, str, dict[str, Any]]] = []

        async def send_event(
            type_: str,
            request_id: str,
            conversation_id: str,
            payload: dict[str, Any],
        ) -> None:
            events.append((type_, request_id, conversation_id, payload))

        async def start_solo(conversation_id: str, request_id: str, task: str) -> str:
            return "solo"

        async def solo_control(conversation_id: str, request_id: str, action: str) -> str:
            return "solo-control"

        runtime = AgentRuntime(
            config_getter=AppConfig,
            confirmation_store=ToolConfirmationStore(),
            attachment_store=AttachmentStore(self.workspace),
            confirmed_tool_results={},
            send_event=send_event,
            start_solo=start_solo,
            solo_control=solo_control,
            memory_service=self.service,
        )

        reply = asyncio.run(
            runtime.handle_user_message(
                "conv",
                "req",
                "记住 仙逆：每周一更新",
            )
        )

        self.assertEqual(reply, "已记到长期记忆。")
        self.assertEqual(self.service.state().notes[0].text, "仙逆：每周一更新")
        self.assertFalse((self.workspace / "anime_schedule.txt").exists())
        self.assertTrue(any(event[0] == "server:memory_updated" for event in events))
        self.assertTrue(any(event[0] == "server:message" for event in events))

    def test_regular_turn_records_event_without_distilling_notes(self) -> None:
        events: list[tuple[str, str, str, dict[str, Any]]] = []

        async def send_event(
            type_: str,
            request_id: str,
            conversation_id: str,
            payload: dict[str, Any],
        ) -> None:
            events.append((type_, request_id, conversation_id, payload))

        async def start_solo(conversation_id: str, request_id: str, task: str) -> str:
            return "solo"

        async def solo_control(conversation_id: str, request_id: str, action: str) -> str:
            return "solo-control"

        runtime = AgentRuntime(
            config_getter=AppConfig,
            confirmation_store=ToolConfirmationStore(),
            attachment_store=AttachmentStore(self.workspace),
            confirmed_tool_results={},
            send_event=send_event,
            start_solo=start_solo,
            solo_control=solo_control,
            memory_service=self.service,
        )
        distill_event = AsyncMock(return_value=True)
        self.service.distill_event = distill_event  # type: ignore[method-assign]
        decision = AgentRouteDecision(
            route="answer_directly",
            answer="你好。",
            task_title="问候",
            task_brief="你好",
            worker_kind="general",
        )

        async def fake_route(self: AgentRouter, *args: Any, **kwargs: Any) -> AgentRouteDecision:
            return decision

        with patch.object(AgentRouter, "route", fake_route):
            reply = asyncio.run(runtime.handle_user_message("conv", "req", "你好"))

        state = self.service.state()
        self.assertEqual(reply, "你好。")
        self.assertEqual(len(state.events), 1)
        self.assertEqual(state.events[0].source, "turn")
        self.assertEqual(state.notes, [])
        distill_event.assert_not_awaited()
        self.assertFalse(any(event[0] == "server:memory_updated" for event in events))


if __name__ == "__main__":
    unittest.main()
