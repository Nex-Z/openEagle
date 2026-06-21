from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.agent_router import AgentRouter
from app.agent_runtime import AgentRuntime
from app.attachments import AttachmentStore
from app.config import AppConfig, ContextConfig
from app.confirmations import ToolConfirmationStore
from app.conversation_context import ConversationContextService
from app.memory import init_db
from app.memory import store
from app.subagent_models import AgentRouteDecision


class ConversationContextServiceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        init_db(Path(self._tmp.name) / "memory.db")
        self.config = AppConfig(
            context=ContextConfig(
                conversationTurnLimit=30,
                maxInputTokens=100_000,
                preserveRecentMessages=8,
            )
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    async def test_context_survives_service_recreation(self) -> None:
        first = ConversationContextService(lambda: self.config)
        await first.record_turn(
            conversation_id="conv",
            request_id="req-1",
            user_content="我们在修远程会话失忆问题。",
            assistant_content="已经决定使用 SQLite 持久化。",
            route="answer_directly",
        )

        recreated = ConversationContextService(lambda: self.config)
        window = await recreated.context_for_prompt("conv")

        self.assertEqual(window.turn_count, 1)
        self.assertIn("远程会话失忆", window.text)
        self.assertIn("SQLite 持久化", window.text)

    async def test_recreated_runtime_passes_persisted_context_to_main_agent(self) -> None:
        route_calls: list[dict[str, object]] = []

        async def send_event(*args, **kwargs) -> None:
            _ = (args, kwargs)

        async def start_solo(*args, **kwargs) -> str:
            _ = (args, kwargs)
            return "solo"

        async def solo_control(*args, **kwargs) -> str:
            _ = (args, kwargs)
            return "control"

        async def fake_route(self, *args, **kwargs) -> AgentRouteDecision:
            _ = (self, args)
            route_calls.append(dict(kwargs))
            return AgentRouteDecision(
                route="answer_directly",
                answer="已记录。" if len(route_calls) == 1 else "可以继续。",
            )

        def build_runtime() -> AgentRuntime:
            return AgentRuntime(
                config_getter=lambda: self.config,
                confirmation_store=ToolConfirmationStore(),
                attachment_store=AttachmentStore(Path(self._tmp.name)),
                confirmed_tool_results={},
                send_event=send_event,
                start_solo=start_solo,
                solo_control=solo_control,
                conversation_context_service=ConversationContextService(
                    lambda: self.config
                ),
            )

        with patch.object(AgentRouter, "route", fake_route):
            await build_runtime().handle_user_message(
                "conv",
                "req-1",
                "我们正在实现断线后继续对话。",
            )
            await build_runtime().handle_user_message(
                "conv",
                "req-2",
                "继续刚才那个。",
            )

        self.assertEqual(len(route_calls), 2)
        self.assertEqual(route_calls[0]["conversation_context"], "")
        self.assertIn(
            "断线后继续对话",
            str(route_calls[1]["conversation_context"]),
        )

    async def test_retention_keeps_configured_full_turns_and_archives_overflow(self) -> None:
        self.config.context.conversation_turn_limit = 3
        service = ConversationContextService(lambda: self.config)

        for index in range(1, 6):
            await service.record_turn(
                conversation_id="conv",
                request_id=f"req-{index}",
                user_content=f"用户第 {index} 轮",
                assistant_content=f"助手第 {index} 轮",
            )

        turns = store.list_conversation_turns("conv")
        state = store.get_conversation_context_state("conv")
        window = await service.context_for_prompt("conv")

        self.assertEqual([turn.request_id for turn in turns], ["req-3", "req-4", "req-5"])
        self.assertIn("用户第 1 轮", state.archive_summary)
        self.assertIn("用户第 2 轮", state.archive_summary)
        self.assertIn("用户第 5 轮", window.text)

    async def test_client_history_seeds_only_an_empty_backend_conversation(self) -> None:
        service = ConversationContextService(lambda: self.config)
        history = [
            {
                "id": "u-1",
                "requestId": "req-1",
                "role": "user",
                "content": "客户端重启前的问题",
                "createdAt": "2026-06-20T01:00:00Z",
            },
            {
                "id": "a-1",
                "requestId": "req-1",
                "role": "assistant",
                "content": "客户端重启前的回答",
                "createdAt": "2026-06-20T01:00:01Z",
            },
        ]

        await service.seed_from_history("conv", history)
        await service.seed_from_history(
            "conv",
            [
                {
                    "role": "user",
                    "content": "不应覆盖数据库",
                }
            ],
        )
        window = await service.context_for_prompt("conv")

        self.assertEqual(window.turn_count, 1)
        self.assertIn("客户端重启前的问题", window.text)
        self.assertNotIn("不应覆盖数据库", window.text)

    async def test_idle_compaction_preserves_recent_full_turns(self) -> None:
        service = ConversationContextService(lambda: self.config)
        for index in range(1, 7):
            await service.record_turn(
                conversation_id="im_telegram_demo",
                request_id=f"req-{index}",
                user_content=f"远程用户第 {index} 轮",
                assistant_content=f"远程助手第 {index} 轮",
            )

        changed = await service.compact_for_idle("im_telegram_demo")
        state = store.get_conversation_context_state("im_telegram_demo")
        window = await service.context_for_prompt("im_telegram_demo")

        self.assertTrue(changed)
        self.assertGreater(state.idle_through_turn_id, 0)
        self.assertIn("远程用户第 1 轮", state.idle_summary)
        self.assertIn("远程用户第 6 轮", window.text)


if __name__ == "__main__":
    unittest.main()
