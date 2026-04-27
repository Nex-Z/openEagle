from __future__ import annotations

import sys
import hashlib
import inspect
import tempfile
import unittest
from pathlib import Path

from app.config import ToolConfig
from app.confirmations import ToolConfirmationStore
from app.default_tools import build_configured_tool_functions, build_default_tools
from app.prompts import build_chat_instructions, solo_decision_instructions
from app.safety import assess_solo_action, assess_tool_action, classify_command_risk
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
            self.assertEqual(command.level, "safe")

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

    def test_command_risk_classification(self) -> None:
        self.assertEqual(classify_command_risk("git status --short").level, "safe")
        self.assertEqual(classify_command_risk("rg TODO src").level, "safe")
        self.assertEqual(classify_command_risk("python build.py").level, "confirm")
        self.assertEqual(classify_command_risk("git reset --hard HEAD").level, "blocked")
        self.assertEqual(classify_command_risk("").level, "blocked")

    def test_file_mutation_actions_are_confirmed_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.txt"
            source.write_text("hello", encoding="utf-8")

            for name, params in [
                ("create_directory", {"path": "new-dir"}),
                ("copy_path", {"source": "source.txt", "destination": "copy.txt"}),
                ("move_path", {"source": "source.txt", "destination": "moved.txt"}),
                ("delete_path", {"path": "source.txt"}),
                (
                    "apply_text_edits",
                    {
                        "path": "source.txt",
                        "edits": [{"old_text": "hello", "new_text": "world"}],
                    },
                ),
            ]:
                assessment = assess_tool_action(name, params, root)
                self.assertEqual(assessment.level, "confirm", name)

            blocked = assess_tool_action("delete_path", {"path": "."}, root)
            self.assertEqual(blocked.level, "blocked")


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

    def test_permission_mode_controls_tool_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = ToolConfirmationStore()
            guarded_toolkit = build_default_tools(
                workspace_root=root,
                confirmation_store=store,
                request_id="req",
                conversation_id="conv",
                permission_mode="default",
            )
            guarded_result = guarded_toolkit.write_text_file("guarded.txt", "hello")
            self.assertTrue(guarded_result.startswith("CONFIRMATION_REQUIRED"))
            self.assertFalse((root / "guarded.txt").exists())

            all_permissions_toolkit = build_default_tools(
                workspace_root=root,
                confirmation_store=store,
                request_id="req",
                conversation_id="conv",
                permission_mode="all",
            )
            direct_result = all_permissions_toolkit.write_text_file("direct.txt", "hello")
            self.assertIn("Successfully wrote", direct_result)
            self.assertEqual((root / "direct.txt").read_text(encoding="utf-8"), "hello")

    def test_run_command_uses_risk_classification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = ToolConfirmationStore()
            toolkit = build_default_tools(
                workspace_root=root,
                confirmation_store=store,
                request_id="req",
                conversation_id="conv",
                permission_mode="default",
            )

            safe_result = toolkit.run_command("dir")
            self.assertFalse(safe_result.startswith("CONFIRMATION_REQUIRED"))

            unknown_result = toolkit.run_command(f'"{sys.executable}" -c "print(123)"')
            self.assertTrue(unknown_result.startswith("CONFIRMATION_REQUIRED"))

            blocked_result = toolkit.run_command("git reset --hard HEAD")
            self.assertIn("明确高危操作", blocked_result)

    def test_replace_text_confirmation_and_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "note.txt"
            target.write_text("hello world", encoding="utf-8")
            store = ToolConfirmationStore()

            guarded_toolkit = build_default_tools(
                workspace_root=root,
                confirmation_store=store,
                request_id="req",
                conversation_id="conv",
                permission_mode="default",
            )
            guarded_result = guarded_toolkit.replace_text_in_file(
                "note.txt",
                "world",
                "codex",
            )
            self.assertTrue(guarded_result.startswith("CONFIRMATION_REQUIRED"))
            self.assertEqual(target.read_text(encoding="utf-8"), "hello world")

            all_permissions_toolkit = build_default_tools(
                workspace_root=root,
                confirmation_store=store,
                request_id="req",
                conversation_id="conv",
                permission_mode="all",
            )
            direct_result = all_permissions_toolkit.replace_text_in_file(
                "note.txt",
                "world",
                "codex",
            )
            self.assertIn("Successfully replaced 1 occurrence", direct_result)
            self.assertEqual(target.read_text(encoding="utf-8"), "hello codex")

    def test_read_text_file_ranges_and_truncation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "note.txt"
            target.write_text("line1\nline2\nline3\nline4", encoding="utf-8")
            toolkit = build_default_tools(workspace_root=root)

            excerpt = toolkit.read_text_file("note.txt", start_line=2, end_line=3)
            self.assertEqual(excerpt, "line2\nline3")

            truncated = toolkit.read_text_file("note.txt", max_chars=5)
            self.assertTrue(truncated.startswith("line1"))
            self.assertIn("[truncated", truncated)

            numbered = toolkit.read_text_file("note.txt", start_line=2, end_line=2, include_line_numbers=True)
            self.assertEqual(numbered, "2: line2")

    def test_read_text_file_rejects_binary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "image.png").write_bytes(b"\x89PNG\x00binary")
            toolkit = build_default_tools(workspace_root=root)

            result = toolkit.read_text_file("image.png")
            self.assertIn("二进制文件", result)

    def test_search_ignores_heavy_directories_and_caps_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "src" / "note.txt").write_text("needle here", encoding="utf-8")
            (root / "node_modules").mkdir()
            (root / "node_modules" / "hidden.txt").write_text("needle hidden", encoding="utf-8")
            toolkit = build_default_tools(workspace_root=root)

            files = toolkit.search_files("hidden")
            self.assertEqual(files, "(no matches)")

            text = toolkit.search_text("needle", max_results=10)
            self.assertIn("src/note.txt:1", text)
            self.assertNotIn("node_modules", text)

    def test_replace_text_requires_exact_occurrence_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "note.txt"
            target.write_text("hello hello", encoding="utf-8")
            toolkit = build_default_tools(workspace_root=root, permission_mode="all")

            result = toolkit.replace_text_in_file("note.txt", "hello", "world")
            self.assertIn("命中次数不符合预期", result)
            self.assertEqual(target.read_text(encoding="utf-8"), "hello hello")

    def test_apply_text_edits_confirmation_hash_and_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "note.txt"
            original = "alpha beta gamma"
            target.write_text(original, encoding="utf-8")
            expected_hash = hashlib.sha256(original.encode("utf-8")).hexdigest()
            store = ToolConfirmationStore()

            guarded = build_default_tools(
                workspace_root=root,
                confirmation_store=store,
                request_id="req",
                conversation_id="conv",
                permission_mode="default",
            )
            guarded_result = guarded.apply_text_edits(
                "note.txt",
                [{"old_text": "beta", "new_text": "BETA"}],
                expected_sha256=expected_hash,
            )
            self.assertTrue(guarded_result.startswith("CONFIRMATION_REQUIRED"))
            self.assertEqual(target.read_text(encoding="utf-8"), original)

            direct = build_default_tools(workspace_root=root, permission_mode="all")
            hash_miss = direct.apply_text_edits(
                "note.txt",
                [{"old_text": "beta", "new_text": "BETA"}],
                expected_sha256="bad",
            )
            self.assertIn("expected_sha256", hash_miss)
            self.assertEqual(target.read_text(encoding="utf-8"), original)

            applied = direct.apply_text_edits(
                "note.txt",
                [{"old_text": "beta", "new_text": "BETA"}],
                expected_sha256=expected_hash,
            )
            self.assertIn("Successfully applied 1 text edit", applied)
            self.assertEqual(target.read_text(encoding="utf-8"), "alpha BETA gamma")

    def test_file_operations_confirmation_and_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "source.txt").write_text("hello", encoding="utf-8")
            store = ToolConfirmationStore()
            guarded = build_default_tools(
                workspace_root=root,
                confirmation_store=store,
                request_id="req",
                conversation_id="conv",
                permission_mode="default",
            )
            self.assertTrue(guarded.create_directory("new-dir").startswith("CONFIRMATION_REQUIRED"))

            direct = build_default_tools(workspace_root=root, permission_mode="all")
            self.assertIn("Successfully created", direct.create_directory("new-dir"))
            self.assertIn("Successfully copied", direct.copy_path("source.txt", "copy.txt"))
            self.assertTrue((root / "copy.txt").exists())
            self.assertIn("Successfully moved", direct.move_path("copy.txt", "moved.txt"))
            self.assertTrue((root / "moved.txt").exists())
            self.assertIn("Successfully deleted", direct.delete_path("moved.txt"))
            self.assertFalse((root / "moved.txt").exists())


class ConfiguredToolFunctionTest(unittest.TestCase):
    def test_build_configured_tool_functions_filters_and_names_are_unique(self) -> None:
        tools, name_map = build_configured_tool_functions(
            [
                ToolConfig(id="one", name="Git Status", command="git status", enabled=True),
                ToolConfig(id="two", name="Git Status", command="git status", enabled=True),
                ToolConfig(id="three", name="Disabled", command="git status", enabled=False),
                ToolConfig(id="four", name="Blank", command="   ", enabled=True),
            ],
            workspace_root=Path.cwd(),
        )

        self.assertEqual(len(tools), 2)
        self.assertEqual(len(name_map), 2)
        self.assertEqual(len({tool.name for tool in tools}), 2)
        for tool in tools:
            self.assertEqual(len(inspect.signature(tool.entrypoint).parameters), 0)

    def test_configured_tool_confirmation_and_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = ToolConfirmationStore()
            command = f'"{sys.executable}" -c "print(\'tool ok\')"'
            tool_config = ToolConfig(
                id="tool-1",
                name="Python Tool",
                command=command,
                enabled=True,
            )

            guarded_tools, _ = build_configured_tool_functions(
                [tool_config],
                workspace_root=root,
                confirmation_store=store,
                request_id="req",
                conversation_id="conv",
                permission_mode="default",
            )
            guarded_result = guarded_tools[0].entrypoint()
            self.assertTrue(guarded_result.startswith("CONFIRMATION_REQUIRED"))

            direct_tools, name_map = build_configured_tool_functions(
                [tool_config],
                workspace_root=root,
                confirmation_store=store,
                request_id="req",
                conversation_id="conv",
                permission_mode="all",
            )
            self.assertEqual(name_map[direct_tools[0].name], "Python Tool")
            direct_result = direct_tools[0].entrypoint()
            self.assertEqual(direct_result.strip(), "tool ok")

    def test_configured_tool_blocks_invalid_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tools, _ = build_configured_tool_functions(
                [
                    ToolConfig(
                        id="tool-2",
                        name="Invalid Cwd",
                        command="git status",
                        cwd="../outside",
                        enabled=True,
                    )
                ],
                workspace_root=root,
                permission_mode="all",
            )

            result = tools[0].entrypoint()
            self.assertIn("路径超出工作区范围", result)


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


class PromptPolicyTest(unittest.TestCase):
    def test_chat_prompt_contains_command_first_and_visual_boundary(self) -> None:
        instructions = "\n".join(build_chat_instructions("conv", [], [], []))
        self.assertIn("优先使用 run_command", instructions)
        self.assertIn("不要启动视觉桌面动作", instructions)
        self.assertIn("小步精确修改", instructions)
        self.assertIn("CONFIRMATION_REQUIRED", instructions)

    def test_solo_prompt_contains_visual_boundary_and_json_contract(self) -> None:
        instructions = "\n".join(solo_decision_instructions())
        self.assertIn("必须仅输出 JSON", instructions)
        self.assertIn("优先使用 execute_command", instructions)
        self.assertIn("视觉动作", instructions)
        self.assertIn("归一化比例坐标", instructions)


if __name__ == "__main__":
    unittest.main()
