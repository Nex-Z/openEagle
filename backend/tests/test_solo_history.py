from __future__ import annotations

import unittest
from unittest.mock import patch

from app.prompts import select_solo_history


def _history(n: int) -> list[dict[str, object]]:
    return [
        {"step": i, "decision": {"action": "click"}, "result": {"success": True}}
        for i in range(1, n + 1)
    ]


class SelectSoloHistoryTest(unittest.TestCase):
    def test_disabled_returns_full(self) -> None:
        history = _history(30)
        result = select_solo_history(history, max_tokens=1, enabled=False, head=2, tail=6)
        self.assertEqual(result, history)
        self.assertEqual(len(result), 30)

    def test_under_budget_returns_full(self) -> None:
        history = _history(30)
        with patch("app.prompts.estimate_text_tokens", return_value=100):
            result = select_solo_history(
                history, max_tokens=24_000, enabled=True, head=2, tail=6
            )
        self.assertEqual(result, history)
        self.assertEqual(len(result), 30)

    def test_over_budget_compacts_to_head_placeholder_tail(self) -> None:
        history = _history(30)  # steps 1..30
        with patch("app.prompts.estimate_text_tokens", return_value=99_999):
            result = select_solo_history(
                history, max_tokens=24_000, enabled=True, head=2, tail=6
            )
        self.assertEqual(len(result), 2 + 1 + 6)  # head + placeholder + tail
        # 首部
        self.assertEqual(result[0]["step"], 1)
        self.assertEqual(result[1]["step"], 2)
        # 中间占位符
        placeholder = result[2]
        self.assertTrue(placeholder.get("omitted"))
        self.assertEqual(placeholder["step_range"], "3~24")
        self.assertEqual(placeholder["omitted_step_count"], 22)
        self.assertIn("已省略", placeholder["note"])
        self.assertIn("findings", placeholder["note"])
        # 尾部
        self.assertEqual([item["step"] for item in result[3:]], [25, 26, 27, 28, 29, 30])

    def test_len_le_head_plus_tail_returns_full(self) -> None:
        history = _history(8)  # 8 == head(2) + tail(6)
        with patch("app.prompts.estimate_text_tokens", return_value=99_999):
            result = select_solo_history(
                history, max_tokens=1, enabled=True, head=2, tail=6
            )
        self.assertEqual(result, history)

    def test_tail_param_honored(self) -> None:
        history = _history(20)
        with patch("app.prompts.estimate_text_tokens", return_value=99_999):
            result = select_solo_history(
                history, max_tokens=1, enabled=True, head=2, tail=3
            )
        self.assertEqual(len(result), 2 + 1 + 3)
        self.assertEqual([item["step"] for item in result[3:]], [18, 19, 20])

    def test_tail_zero_keeps_no_tail(self) -> None:
        history = _history(10)
        with patch("app.prompts.estimate_text_tokens", return_value=99_999):
            result = select_solo_history(
                history, max_tokens=1, enabled=True, head=2, tail=0
            )
        self.assertEqual(len(result), 2 + 1 + 0)
        self.assertEqual(result[0]["step"], 1)
        self.assertEqual(result[1]["step"], 2)
        self.assertTrue(result[2]["omitted"])
        self.assertEqual(result[2]["step_range"], "3~10")


if __name__ == "__main__":
    unittest.main()
