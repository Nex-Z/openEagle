from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.token_usage import (
    init_token_usage_db,
    normalize_usage,
    record_model_usage,
    token_usage_dashboard,
    token_usage_scope,
)


class TokenUsageTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        init_token_usage_db(Path(self._tmp.name) / "token_usage.db")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_normalizes_openai_and_anthropic_usage(self) -> None:
        self.assertEqual(
            normalize_usage(
                SimpleNamespace(
                    prompt_tokens=120,
                    completion_tokens=30,
                    total_tokens=150,
                )
            ),
            (120, 30, 150),
        )
        self.assertEqual(
            normalize_usage(SimpleNamespace(input_tokens=80, output_tokens=20)),
            (80, 20, 100),
        )

    async def test_aggregates_calls_by_request_and_builds_dashboard(self) -> None:
        updates: list[dict[str, object]] = []

        async def on_update(payload: dict[str, object]) -> None:
            updates.append(payload)

        async with token_usage_scope(
            request_id="request-1",
            conversation_id="conversation-1",
            source="chat",
            on_update=on_update,
        ):
            await record_model_usage(
                "openai",
                "gpt-test",
                SimpleNamespace(prompt_tokens=100, completion_tokens=25, total_tokens=125),
            )
            await record_model_usage(
                "anthropic",
                "claude-test",
                SimpleNamespace(input_tokens=40, output_tokens=10),
            )

        self.assertEqual(len(updates), 2)
        request_usage = updates[-1]["requestUsage"]
        self.assertIsInstance(request_usage, dict)
        assert isinstance(request_usage, dict)
        self.assertEqual(request_usage["inputTokens"], 140)
        self.assertEqual(request_usage["outputTokens"], 35)
        self.assertEqual(request_usage["totalTokens"], 175)
        self.assertEqual(request_usage["calls"], 2)

        dashboard = token_usage_dashboard()
        self.assertEqual(dashboard["total"]["totalTokens"], 175)
        self.assertEqual(dashboard["total"]["calls"], 2)
        self.assertEqual(len(dashboard["models"]), 2)
        self.assertEqual(dashboard["recentRequests"][0]["requestId"], "request-1")
        self.assertEqual(dashboard["recentRequests"][0]["totalTokens"], 175)
