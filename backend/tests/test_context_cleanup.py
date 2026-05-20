from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from app.config import ContextConfig
from app.context_cleanup import (
    TOOL_INPUT_PLACEHOLDER,
    TOOL_RESULT_PLACEHOLDER,
    compact_messages_for_prompt,
    compact_messages_for_prompt_with_ai,
    should_cleanup_for_idle,
)


class ContextCleanupTest(unittest.TestCase):
    def test_compaction_preserves_system_and_recent_messages(self) -> None:
        recent_user = {"role": "user", "content": "recent user"}
        recent_assistant = {"role": "assistant", "content": "recent assistant"}
        messages = [
            {"role": "system", "content": "system must stay"},
            {"role": "user", "content": "old middle text " * 50},
            {"role": "tool", "content": "large tool output " * 50},
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "tool-1", "name": "read_file", "input": {"path": "x"}},
                    {"type": "text", "text": "assistant middle text " * 50},
                ],
            },
            recent_user,
            recent_assistant,
        ]

        result = compact_messages_for_prompt(
            messages,
            ContextConfig(
                maxInputTokens=1,
                preserveRecentMessages=2,
                middleMessageCharLimit=60,
            ),
        )

        self.assertTrue(result.changed)
        self.assertEqual(result.messages[0], messages[0])
        self.assertIs(result.messages[-2], recent_user)
        self.assertIs(result.messages[-1], recent_assistant)
        self.assertIn("[...middle context truncated...]", result.messages[1]["content"])
        self.assertEqual(result.messages[2]["content"], TOOL_RESULT_PLACEHOLDER)
        structured_blocks = result.messages[3]["content"]
        self.assertEqual(structured_blocks[0]["input"], TOOL_INPUT_PLACEHOLDER)
        self.assertIn("[...middle context truncated...]", structured_blocks[1]["text"])

    def test_remove_mode_drops_standalone_middle_tool_messages_only(self) -> None:
        recent_tool = {"role": "tool", "content": "recent tool output"}
        messages = [
            {"role": "user", "content": "old user"},
            {"role": "tool", "content": "old tool output"},
            recent_tool,
        ]

        result = compact_messages_for_prompt(
            messages,
            ContextConfig(
                maxInputTokens=1,
                preserveRecentMessages=1,
                toolMessageMode="remove",
            ),
        )

        self.assertTrue(result.changed)
        self.assertEqual([message["content"] for message in result.messages], ["old user", "recent tool output"])
        self.assertIs(result.messages[-1], recent_tool)

    def test_idle_cleanup_uses_configured_minutes(self) -> None:
        config = ContextConfig(imIdleCleanupMinutes=15)
        now = datetime.now(UTC)

        self.assertFalse(
            should_cleanup_for_idle(now - timedelta(minutes=14), config, now=now)
        )
        self.assertTrue(
            should_cleanup_for_idle(now - timedelta(minutes=16), config, now=now)
        )


class ContextCleanupAiSummaryTest(unittest.IsolatedAsyncioTestCase):
    async def test_ai_summary_precleans_tools_before_summarizer(self) -> None:
        prompts: list[str] = []
        snapshots: list[tuple[str, dict[str, object]]] = []
        messages = [
            {"role": "user", "content": "old user goal"},
            {"role": "tool", "content": "SECRET_TOOL_OUTPUT " * 200},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": "read_file",
                        "input": {"path": "E:/very/long/path", "raw": "x" * 4000},
                    },
                    {"type": "text", "text": "tool finished"},
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-1",
                        "content": "ANOTHER_SECRET_TOOL_OUTPUT " * 200,
                    }
                ],
            },
            {"role": "user", "content": "latest question"},
        ]

        async def summarize(prompt: str) -> str:
            prompts.append(prompt)
            return "用户之前要求处理目标，工具已执行，当前继续回答最新问题。"

        async def snapshot(content: str, payload: dict[str, object]) -> None:
            snapshots.append((content, payload))

        result = await compact_messages_for_prompt_with_ai(
            messages,
            ContextConfig(
                maxInputTokens=1,
                preserveRecentMessages=1,
                toolResultCharLimit=0,
            ),
            summarizer=summarize,
            snapshot=snapshot,
        )

        self.assertTrue(result.changed)
        self.assertEqual(result.method, "ai_summary")
        self.assertEqual(len(prompts), 1)
        self.assertNotIn("SECRET_TOOL_OUTPUT", prompts[0])
        self.assertNotIn("ANOTHER_SECRET_TOOL_OUTPUT", prompts[0])
        self.assertIn(TOOL_RESULT_PLACEHOLDER, prompts[0])
        self.assertEqual(len(snapshots), 1)
        self.assertNotIn("SECRET_TOOL_OUTPUT", snapshots[0][0])
        self.assertEqual(result.messages[0]["role"], "user")
        self.assertIn("已压缩的中段上下文摘要", result.messages[0]["content"])
        self.assertIs(result.messages[-1], messages[-1])

    async def test_ai_summary_failure_falls_back_to_rule_compaction(self) -> None:
        async def fail_summary(_: str) -> str:
            raise RuntimeError("model down")

        result = await compact_messages_for_prompt_with_ai(
            [
                {"role": "user", "content": "old middle text " * 50},
                {"role": "tool", "content": "tool output " * 50},
                {"role": "user", "content": "latest"},
            ],
            ContextConfig(maxInputTokens=1, preserveRecentMessages=1, middleMessageCharLimit=80),
            summarizer=fail_summary,
        )

        self.assertTrue(result.changed)
        self.assertEqual(result.method, "rule")
        self.assertIn("[...middle context truncated...]", result.messages[0]["content"])
        self.assertEqual(result.messages[1]["content"], TOOL_RESULT_PLACEHOLDER)

    async def test_recent_tool_result_keeps_previous_tool_use_pair(self) -> None:
        assistant_tool = {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "tool-1", "name": "read_file", "input": {"path": "x"}}
            ],
        }
        recent_result = {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "tool-1", "content": "latest result"}
            ],
        }

        result = await compact_messages_for_prompt_with_ai(
            [
                {"role": "user", "content": "older middle"},
                assistant_tool,
                recent_result,
            ],
            ContextConfig(maxInputTokens=1, preserveRecentMessages=1),
            summarizer=lambda _: _async_value("summary"),
        )

        self.assertEqual(result.method, "ai_summary")
        self.assertIs(result.messages[-2], assistant_tool)
        self.assertIs(result.messages[-1], recent_result)


async def _async_value(value: str) -> str:
    return value


if __name__ == "__main__":
    unittest.main()
