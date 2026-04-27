from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.confirmations import ToolConfirmationStore
from app.safety import assess_solo_action, assess_tool_action
from app.solo_executor import SoloExecutor


class SafetyAssessmentTest(unittest.TestCase):
    def test_solo_safe_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            assessment = assess_solo_action("click", {"x": 0.5, "y": 0.5}, root)
            self.assertEqual(assessment.level, "safe")

    def test_solo_confirm_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            assessment = assess_solo_action("press_keys", {"keys": ["ctrl", "s"]}, root)
            self.assertEqual(assessment.level, "confirm")

            command = assess_solo_action("execute_command", {"command": "dir", "cwd": "."}, root)
            self.assertEqual(command.level, "confirm")

    def test_solo_blocked_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            empty_command = assess_solo_action("execute_command", {"command": ""}, root)
            self.assertEqual(empty_command.level, "blocked")

            unknown = assess_solo_action("unknown", {}, root)
            self.assertEqual(unknown.level, "blocked")

    def test_tool_confirm_and_blocked_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write = assess_tool_action("write_text_file", {"path": "note.txt"}, root)
            self.assertEqual(write.level, "confirm")

            outside = assess_tool_action("write_text_file", {"path": "../note.txt"}, root)
            self.assertEqual(outside.level, "blocked")


class ConfirmationStoreTest(unittest.TestCase):
    def test_create_get_and_pop_confirmation(self) -> None:
        store = ToolConfirmationStore()
        pending = store.create(
            request_id="req",
            conversation_id="conv",
            kind="tool",
            name="run_command",
            reason="将执行工作区命令。",
            params={"command": "dir"},
        )
        self.assertIs(store.get(pending.confirmation_id), pending)
        self.assertIs(store.pop(pending.confirmation_id), pending)
        self.assertIsNone(store.get(pending.confirmation_id))


class ScreenshotHashTest(unittest.TestCase):
    def test_content_hash_is_stable_and_content_based(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "first.bin"
            second = Path(tmp) / "second.bin"
            first.write_bytes(b"same")
            second.write_bytes(b"same")
            self.assertEqual(SoloExecutor.content_hash(first), SoloExecutor.content_hash(second))

            second.write_bytes(b"different")
            self.assertNotEqual(SoloExecutor.content_hash(first), SoloExecutor.content_hash(second))


if __name__ == "__main__":
    unittest.main()
