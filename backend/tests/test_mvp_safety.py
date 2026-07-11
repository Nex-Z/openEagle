from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.agent_router import AgentRouter
from app.agent_runtime import AgentRuntime
from app.config import AgentConfig, AppConfig, McpConfig, SkillConfig, ToolConfig
from app.confirmations import ToolConfirmationStore
from app.default_tools import build_configured_tools, build_default_tools
from app.prompts import (
    build_chat_instructions,
    build_direct_answer_prompt,
    build_main_router_instructions,
    build_main_router_prompt,
    build_solo_decision_prompt,
    build_solo_repair_prompt,
    current_datetime_hint,
    solo_decision_instructions,
)
from app.providers.base import ReplyTrace
from app.safety import (
    BlockedActionError,
    assess_solo_action,
    assess_tool_action,
    classify_command_risk,
    is_repairable_solo_block,
)
from app.solo_executor import SoloExecutor
from app.solo_kernel import SoloAgentKernel, action_signature
from app.solo_capabilities import (
    SOLO_CONFIRMATION_PREFIX,
    SoloDefaultCapabilities,
    SoloCapabilityRuntime,
    _split_stdio_endpoint,
    parse_confirmation_request,
)
from app.solo_service import (
    MODEL_IMAGE_MAX_LONG_EDGE,
    SoloDecision,
    SoloService,
    prepare_model_image,
    summarize_solo_step_result,
)
from app.solo_toolkit import SoloToolkit
from app.subagent_manager import SubAgentManager
from app.subagent_models import WorkerReport


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

    def test_solo_repairable_blocks_can_be_fed_back_to_agent(self) -> None:
        self.assertTrue(
            is_repairable_solo_block(
                "press_keys",
                "press_keys 缺少有效按键列表。",
            )
        )
        self.assertTrue(
            is_repairable_solo_block(
                "execute_command",
                "命令为空或未提供。",
            )
        )
        self.assertFalse(
            is_repairable_solo_block(
                "execute_command",
                "命令包含明确高危操作，已阻断。",
            )
        )

    def test_solo_open_url_allows_only_http_urls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            safe = assess_solo_action("open_url", {"url": "https://www.google.com/search?q=news"}, root)
            blocked = assess_solo_action("open_url", {"url": "file:///C:/Windows/System32/calc.exe"}, root)

        self.assertEqual(safe.level, "safe")
        self.assertEqual(blocked.level, "blocked")

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
        self.assertEqual(
            classify_command_risk(
                "wmic logicaldisk get size,freespace,caption,volumename /format:list"
            ).level,
            "safe",
        )
        self.assertEqual(classify_command_risk("format C:").level, "blocked")
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

    def test_negative_constraints_block_destructive_tools_even_with_all_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "project.txt"
            target.write_text("keep me", encoding="utf-8")
            toolkit = build_default_tools(
                workspace_root=root,
                permission_mode="all",
                task_context="删除 project.txt 之前先判断风险；如果会破坏，请不要删除。",
            )

            result = toolkit.delete_path("project.txt")

            self.assertIn("用户明确要求不要删除", result)
            self.assertTrue(target.exists())

    def test_explicit_delete_without_negative_constraint_is_not_overblocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "temp.txt"
            target.write_text("remove me", encoding="utf-8")
            toolkit = build_default_tools(
                workspace_root=root,
                permission_mode="all",
                task_context="请删除这个临时文件。",
            )

            result = toolkit.delete_path("temp.txt")

            self.assertIn("Successfully deleted", result)
            self.assertFalse(target.exists())

    def test_read_only_context_blocks_command_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            toolkit = build_default_tools(
                workspace_root=root,
                permission_mode="all",
                task_context="不要执行命令，只分析这个命令会输出什么。",
            )

            result = toolkit.run_command(f'"{sys.executable}" -c "print(123)"')

            self.assertIn("不要执行命令", result)

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

    def test_all_permissions_allow_external_file_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside_tmp:
            root = Path(tmp)
            outside = Path(outside_tmp)
            source = outside / "source.txt"
            source.write_text("external content", encoding="utf-8")

            restricted = build_default_tools(workspace_root=root)
            with self.assertRaises(BlockedActionError):
                restricted.get_file_info(str(source))

            toolkit = build_default_tools(workspace_root=root, permission_mode="all")
            self.assertIn(str(source), toolkit.get_file_info(str(source)))
            self.assertEqual(toolkit.read_text_file(str(source)), "external content")

            destination = outside / "written.txt"
            result = toolkit.write_text_file(str(destination), "written externally")
            self.assertIn("Successfully wrote", result)
            self.assertEqual(destination.read_text(encoding="utf-8"), "written externally")

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

    def test_search_files_honors_limit_and_solo_capability_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index in range(3):
                (root / f"match-{index}.txt").write_text("", encoding="utf-8")
            toolkit = build_default_tools(workspace_root=root)

            limited = toolkit.search_files("match", max_results=2)
            self.assertEqual(limited.count("match-"), 2)
            self.assertIn("truncated at max_results=2", limited)

            solo_capabilities = SoloDefaultCapabilities(toolkit)
            search_files_tool = next(
                tool for tool in solo_capabilities.agent_tools if tool.name == "search_files"
            )
            solo_limited = search_files_tool.invoke(
                {"keyword": "match", "max_results": 1}
            )
            self.assertEqual(solo_limited.count("match-"), 1)
            self.assertIn("truncated at max_results=1", solo_limited)

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


class ConfiguredToolTest(unittest.TestCase):
    def test_build_configured_tools_filters_and_names_are_unique(self) -> None:
        tools, name_map = build_configured_tools(
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
            self.assertEqual(tool.args, {})

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

            guarded_tools, _ = build_configured_tools(
                [tool_config],
                workspace_root=root,
                confirmation_store=store,
                request_id="req",
                conversation_id="conv",
                permission_mode="default",
            )
            guarded_result = guarded_tools[0].invoke({})
            self.assertTrue(guarded_result.startswith("CONFIRMATION_REQUIRED"))

            direct_tools, name_map = build_configured_tools(
                [tool_config],
                workspace_root=root,
                confirmation_store=store,
                request_id="req",
                conversation_id="conv",
                permission_mode="all",
            )
            self.assertEqual(name_map[direct_tools[0].name], "Python Tool")
            direct_result = direct_tools[0].invoke({})
            self.assertEqual(direct_result.strip(), "tool ok")

    def test_configured_tool_blocks_invalid_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tools, _ = build_configured_tools(
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

            result = tools[0].invoke({})
            self.assertIn("路径超出工作区范围", result)


class SoloCapabilityRuntimeTest(unittest.TestCase):
    def test_stdio_endpoint_parser_preserves_windows_paths(self) -> None:
        if os.name != "nt":
            self.skipTest("Windows command-line parsing is only used on Windows.")

        self.assertEqual(
            _split_stdio_endpoint(r"C:\Users\me\mcp.exe --stdio"),
            [r"C:\Users\me\mcp.exe", "--stdio"],
        )
        self.assertEqual(
            _split_stdio_endpoint(
                r'"C:\Program Files\nodejs\node.exe" "C:\Users\me\server.js" --stdio'
            ),
            [
                r"C:\Program Files\nodejs\node.exe",
                r"C:\Users\me\server.js",
                "--stdio",
            ],
        )

    def test_solo_toolkit_dispatches_default_tool_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "note.txt").write_text("alpha needle omega", encoding="utf-8")
            (root / "note-copy.txt").write_text("needle", encoding="utf-8")
            toolkit = SoloToolkit(
                SoloExecutor(default_tools=build_default_tools(workspace_root=root))
            )

            current_time = toolkit.execute("get_current_time", {})
            self.assertTrue(current_time["ok"])
            self.assertEqual(current_time["action"], "get_current_time")

            read = toolkit.execute("read_text_file", {"path": "note.txt"})
            self.assertIn("needle", read["output"])

            search = toolkit.execute("search_text", {"keyword": "needle", "path": "."})
            self.assertIn("note.txt", search["output"])

            limited_files = toolkit.execute(
                "search_files", {"keyword": "note", "max_results": 1}
            )
            self.assertIn("truncated at max_results=1", limited_files["output"])

            limited_text = toolkit.execute(
                "search_text", {"keyword": "needle", "max_results": 1}
            )
            self.assertIn("truncated at max_results=1", limited_text["output"])

    def test_capability_catalog_loads_configured_tools_and_skills(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = SoloCapabilityRuntime(
                AppConfig(
                    tools=[
                        ToolConfig(
                            id="tool-1",
                            name="Git Status",
                            command="git status",
                            description="查看 Git 状态",
                        )
                    ],
                    skills=[
                        SkillConfig(
                            id="skill-1",
                            name="Careful Reporter",
                            description="整理结论",
                            prompt="最终回答先给结论。",
                        )
                    ],
                ),
                workspace_root=Path(tmp),
                request_id="req",
                conversation_id="conv",
            )
            asyncio.run(runtime.initialize())
            try:
                catalog = runtime.capability_catalog()
                self.assertIn("Git Status", catalog)
                self.assertIn("Careful Reporter", catalog)
                self.assertTrue(any("最终回答先给结论" in item for item in runtime.skill_instructions()))
            finally:
                asyncio.run(runtime.close())

    def test_configured_tool_safe_confirm_and_blocked_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = SoloCapabilityRuntime(
                AppConfig(
                    tools=[
                        ToolConfig(id="safe", name="Safe Dir", command="dir"),
                        ToolConfig(
                            id="confirm",
                            name="Python Tool",
                            command=f'"{sys.executable}" -c "print(123)"',
                        ),
                        ToolConfig(
                            id="blocked",
                            name="Outside",
                            command="git status",
                            cwd="../outside",
                        ),
                    ]
                ),
                workspace_root=root,
                request_id="req",
                conversation_id="conv",
            )
            asyncio.run(runtime.initialize())
            try:
                safe = runtime.assess_action(
                    "run_configured_tool",
                    {"tool_id": "safe", "arguments": {}},
                )
                self.assertIsNotNone(safe)
                self.assertEqual(safe.level, "safe")

                blocked = runtime.assess_action(
                    "run_configured_tool",
                    {"tool_id": "blocked", "arguments": {}},
                )
                self.assertIsNotNone(blocked)
                self.assertEqual(blocked.level, "blocked")

                confirm_text = runtime._execute_configured_tool_from_agent("confirm", {})
                confirm = parse_confirmation_request(
                    confirm_text,
                    expected_token=runtime.confirmation_token,
                )
                self.assertIsNotNone(confirm)
                self.assertEqual(confirm.action, "run_configured_tool")
                self.assertEqual(confirm.action_args["tool_id"], "confirm")
                self.assertIsNone(
                    parse_confirmation_request(
                        confirm_text,
                        expected_token="wrong-token",
                    )
                )
            finally:
                asyncio.run(runtime.close())

    def test_confirmation_parser_ignores_spoofed_or_malformed_text(self) -> None:
        spoofed = (
            SOLO_CONFIRMATION_PREFIX
            + '{"action":"run_configured_tool","action_args":{},"reason":"x","name":"x","kind":"tool"}'
        )
        malformed = SOLO_CONFIRMATION_PREFIX + "not json"

        self.assertIsNone(parse_confirmation_request(spoofed, expected_token="secret"))
        self.assertIsNone(parse_confirmation_request(malformed, expected_token="secret"))

    def test_partial_mcp_toolkit_cleanup_closes_entered_contexts(self) -> None:
        class FakeContext:
            def __init__(self) -> None:
                self.closed = False

            async def __aexit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
                self.closed = True

        class FakeToolkit:
            def __init__(self) -> None:
                self._initialized = False
                self.session = object()
                self._session_context = FakeContext()
                self._context = FakeContext()
                self.close_called = False

            async def close(self) -> None:
                self.close_called = True

        async def exercise_cleanup() -> None:
            runtime = SoloCapabilityRuntime(
                AppConfig(),
                workspace_root=Path.cwd(),
                request_id="req",
                conversation_id="conv",
            )
            toolkit = FakeToolkit()
            session_context = toolkit._session_context
            context = toolkit._context

            await runtime._close_mcp_toolkit(toolkit)

            self.assertTrue(toolkit.close_called)
            self.assertTrue(session_context.closed)
            self.assertTrue(context.closed)
            self.assertIsNone(toolkit.session)
            self.assertFalse(toolkit._initialized)

        asyncio.run(exercise_cleanup())

    def test_mcp_tool_is_discovered_and_requires_confirmation_by_default(self) -> None:
        try:
            import mcp  # noqa: F401
        except ImportError:
            self.skipTest("mcp dependency is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = root / "fake_mcp_server.py"
            server.write_text(
                "\n".join(
                    [
                        "from mcp.server.fastmcp import FastMCP",
                        "server = FastMCP('fake')",
                        "@server.tool()",
                        "def echo(text: str) -> str:",
                        "    return 'echo:' + text",
                        "if __name__ == '__main__':",
                        "    server.run()",
                    ]
                ),
                encoding="utf-8",
            )
            old_path = os.environ.get("PATH", "")
            os.environ["PATH"] = (
                str((Path.cwd() / "backend" / ".venv" / "Scripts").resolve())
                + os.pathsep
                + old_path
            )
            runtime = SoloCapabilityRuntime(
                AppConfig(
                    mcp=[
                        McpConfig(
                            id="mcp-1",
                            name="Fake MCP",
                            transport="stdio",
                            endpoint=f'python "{server}"',
                            enabled=True,
                        )
                    ]
                ),
                workspace_root=root,
                request_id="req",
                conversation_id="conv",
            )

            async def exercise_runtime() -> None:
                traces = await runtime.initialize()
                try:
                    self.assertTrue(any(trace.status == "completed" for trace in traces))
                    catalog = runtime.capability_catalog()
                    self.assertIn("mcp-1", catalog)
                    assessment = runtime.assess_action(
                        "call_mcp_tool",
                        {
                            "server_id": "mcp-1",
                            "tool_name": "echo",
                            "arguments": {"text": "hi"},
                        },
                    )
                    self.assertIsNotNone(assessment)
                    self.assertEqual(assessment.level, "confirm")
                finally:
                    await runtime.close()

            try:
                asyncio.run(exercise_runtime())
            finally:
                os.environ["PATH"] = old_path


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


class SoloModelImageTest(unittest.TestCase):
    def test_model_image_is_resized_jpeg_without_touching_source(self) -> None:
        from PIL import Image as PillowImage

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "large.png"
            image = PillowImage.new("RGB", (2400, 1200), color=(245, 245, 245))
            image.save(source)

            prepared = prepare_model_image(source.as_posix(), max_long_edge=1200)

            self.assertEqual(prepared["mime_type"], "image/jpeg")
            self.assertTrue(prepared["compressed"])
            self.assertLessEqual(max(prepared["model_width"], prepared["model_height"]), 1200)
            self.assertEqual(prepared["source_width"], 2400)
            self.assertEqual(prepared["source_height"], 1200)
            self.assertTrue(Path(prepared["path"]).exists())
            self.assertTrue(source.exists())

    def test_default_model_image_long_edge_stays_readable_size(self) -> None:
        self.assertGreaterEqual(MODEL_IMAGE_MAX_LONG_EDGE, 1920)


class SoloResultSummaryTest(unittest.TestCase):
    def test_solo_result_summary_keeps_decision_signal_without_full_output(self) -> None:
        summary = summarize_solo_step_result(
            {
                "success": True,
                "action": "execute_command",
                "captureAttempts": 3,
                "postActionDelayMs": 900,
                "postActionTotalDelayMs": 1880,
                "visualChange": False,
                "usedVirtualCapture": True,
                "stableAfterChange": True,
                "stabilitySamples": 1,
                "executionResult": {
                    "ok": False,
                    "command": "dir",
                    "cwd": "E:/workspace",
                    "exitCode": 1,
                    "output": "0123456789abcdef",
                },
                "screenshot": {
                    "path": "C:/temp/shot.png",
                    "contentHash": "abc",
                    "capturedAt": "2026-04-27T00:00:00Z",
                    "width": 1920,
                    "height": 1080,
                    "displayIndex": 1,
                },
            },
            max_output_chars=6,
        )

        self.assertEqual(summary["command"], "dir")
        self.assertEqual(summary["exitCode"], 1)
        self.assertEqual(summary["outputTail"], "abcdef")
        self.assertTrue(summary["outputTruncated"])
        self.assertEqual(summary["captureAttempts"], 3)
        self.assertEqual(summary["postActionDelayMs"], 900)
        self.assertEqual(summary["postActionTotalDelayMs"], 1880)
        self.assertFalse(summary["visualChange"])
        self.assertTrue(summary["usedVirtualCapture"])
        self.assertTrue(summary["stableAfterChange"])
        self.assertEqual(summary["stabilitySamples"], 1)
        self.assertEqual(summary["screenshot"]["contentHash"], "abc")
        self.assertNotIn("path", summary["screenshot"])

    def test_solo_result_summary_keeps_open_url_signal(self) -> None:
        summary = summarize_solo_step_result(
            {
                "success": True,
                "action": "open_url",
                "executionResult": {"ok": True, "action": "open_url", "url": "https://example.com"},
            }
        )

        self.assertEqual(summary["url"], "https://example.com")


class SoloAgentKernelTest(unittest.TestCase):
    def test_kernel_initializes_plan_and_prompt_context(self) -> None:
        kernel = SoloAgentKernel.create("打开记事本")
        payload = kernel.plan_payload()

        self.assertEqual(payload["items"][0]["status"], "in_progress")
        self.assertIn("应用操作任务", payload["taskAnalysis"])
        self.assertIn("plan", kernel.prompt_context())
        self.assertIn("completionRequirement", kernel.prompt_context())
        self.assertFalse(kernel.prompt_context()["requiresFindings"])

    def test_kernel_requires_findings_for_news_tasks_before_finish(self) -> None:
        kernel = SoloAgentKernel.create("最近有哪些值得关注的新闻")
        decision = SoloDecision(
            screen_state="搜索结果页可见",
            thought_summary="[状态] 页面可见 [上步] 成功 [决策] 收尾",
            action="finish",
            action_args={},
            progress="准备结束",
            is_task_done=True,
            confidence=0.8,
            agent_message="已打开新闻搜索结果。",
        )

        self.assertTrue(kernel.requires_findings())
        self.assertFalse(kernel.has_completion_evidence([], decision))
        self.assertTrue(kernel.reject_premature_finish("需要先提取 findings"))
        self.assertFalse(all(item.status == "completed" for item in kernel.plan))
        self.assertIn("完成证据", kernel.agent_message)

    def test_kernel_information_finish_ignores_screen_state_and_agent_message(self) -> None:
        kernel = SoloAgentKernel.create("最近有哪些值得关注的新闻")
        decision = SoloDecision(
            screen_state="百度搜索结果页已经打开，页面显示若干结果但尚未提取新闻标题。",
            thought_summary="[状态] 页面可见 [上步] 搜索成功 [决策] 错误收尾",
            action="finish",
            action_args={},
            progress="搜索成功，页面已打开。",
            is_task_done=True,
            confidence=0.8,
            agent_message="已打开搜索结果页面，搜索请求提交成功。",
        )

        self.assertFalse(kernel.has_completion_evidence([], decision))

    def test_kernel_information_finish_accepts_real_finish_report(self) -> None:
        kernel = SoloAgentKernel.create("最近有哪些值得关注的新闻")
        decision = SoloDecision(
            screen_state="结果可见",
            thought_summary="[状态] 已提取 [上步] 成功 [决策] 收尾",
            action="finish",
            action_args={},
            progress="已整理新闻",
            is_task_done=True,
            confidence=0.9,
            finish_report="值得关注的新闻包括：\n1. A 事件持续发酵。\n2. B 政策发布。\n3. C 公司公布新产品。",
        )

        self.assertTrue(kernel.has_completion_evidence([], decision))

    def test_kernel_allows_finish_without_findings_for_app_tasks(self) -> None:
        kernel = SoloAgentKernel.create("打开记事本")
        decision = SoloDecision(
            screen_state="记事本窗口可见",
            thought_summary="[状态] 已打开 [上步] 成功 [决策] 收尾",
            action="finish",
            action_args={},
            progress="已打开应用",
            is_task_done=True,
            confidence=0.9,
            agent_message="已打开记事本。",
        )

        self.assertFalse(kernel.requires_findings())
        self.assertTrue(kernel.has_completion_evidence([], decision))

    def test_kernel_rejects_empty_finish_for_any_task(self) -> None:
        kernel = SoloAgentKernel.create("把当前任务处理好")
        decision = SoloDecision(
            screen_state="",
            thought_summary="[状态] 不清楚 [上步] 不清楚 [决策] 收尾",
            action="finish",
            action_args={},
            progress="完成",
            is_task_done=True,
            confidence=0.5,
            agent_message="完成",
        )

        self.assertEqual(kernel.completion_mode(), "general")
        self.assertFalse(kernel.has_completion_evidence([], decision))

    def test_kernel_does_not_complete_plan_on_passive_done_signal(self) -> None:
        kernel = SoloAgentKernel.create("打开记事本")
        decision = SoloDecision(
            screen_state="还在桌面",
            thought_summary="[状态] 桌面 [上步] 未执行 [决策] 启动",
            action="wait",
            action_args={"ms": 100},
            progress="准备等待",
            is_task_done=True,
            confidence=0.5,
        )

        kernel.record_decision(decision)

        self.assertFalse(all(item.status == "completed" for item in kernel.plan))

    def test_kernel_applies_decision_plan_updates_and_findings(self) -> None:
        kernel = SoloAgentKernel.create("查询天气")
        decision = SoloDecision(
            screen_state="浏览器打开",
            thought_summary="[状态] 已看到天气 [上步] 成功 [决策] 汇报",
            action="finish",
            action_args={},
            progress="已拿到天气",
            is_task_done=True,
            confidence=0.9,
            findings=["重庆 5 月 1 日有雨"],
            plan_updates=[{"index": 2, "status": "completed"}],
            agent_message="查到了。",
        )

        changed = kernel.record_decision(decision)

        self.assertTrue(changed)
        self.assertIn("重庆 5 月 1 日有雨", kernel.findings)
        self.assertTrue(all(item.status == "completed" for item in kernel.plan))

    def test_kernel_treats_command_nonzero_as_recoverable_failure(self) -> None:
        kernel = SoloAgentKernel.create("打开应用")
        decision = SoloDecision(
            screen_state="终端可见",
            thought_summary="[状态] 终端 [上步] 失败 [决策] 换命令",
            action="execute_command",
            action_args={"command": "bad-command"},
            progress="尝试启动应用",
            is_task_done=False,
            confidence=0.4,
        )

        outcome = kernel.assess_step(
            decision,
            {"success": True, "action": "execute_command", "ok": False, "exitCode": 1, "outputTail": "not found"},
            repeat_action_count=1,
            same_screenshot_count=0,
        )

        self.assertFalse(outcome.semantic_success)
        self.assertFalse(outcome.should_pause)
        self.assertIn("命令执行失败", outcome.recovery_hint or "")
        self.assertGreater(kernel.replan_count, 0)

    def test_kernel_pauses_after_repeated_recovery_failures(self) -> None:
        kernel = SoloAgentKernel.create("点击按钮")
        decision = SoloDecision(
            screen_state="按钮不可见",
            thought_summary="[状态] 未找到按钮 [上步] 失败 [决策] 截图",
            action="click",
            action_args={"x": 0.5, "y": 0.5},
            progress="尝试点击",
            is_task_done=False,
            confidence=0.2,
        )
        outcome = None
        for _ in range(kernel.max_consecutive_failures):
            outcome = kernel.assess_step(
                decision,
                {"success": False, "action": "click", "executionError": "target missing"},
                repeat_action_count=1,
                same_screenshot_count=0,
            )

        self.assertIsNotNone(outcome)
        self.assertTrue(outcome.should_pause)


class AgentRouterTest(unittest.TestCase):
    def _model_route_payload(self, **overrides: object) -> str:
        payload = {
            "route": "delegate_new",
            "worker_kind": "general",
            "task_title": "用户请求",
            "task_brief": "完成用户请求",
            "success_criteria": ["完成用户请求"],
            "requires_write": False,
            "requires_gui": False,
            "target_worker_id": None,
            "answer": "",
            "user_visible_summary": "我来处理。",
            "context_summary": "",
        }
        payload.update(overrides)
        return json.dumps(payload, ensure_ascii=False)

    def test_router_parse_accepts_json_wrapper(self) -> None:
        decision = AgentRouter.parse(
            '路由如下：{"route":"delegate_new","task_title":"改 README",'
            '"task_brief":"修改 README 并验证","success_criteria":["README 已更新"],'
            '"worker_kind":"coding","requires_write":true,"requires_gui":false,'
            '"user_visible_summary":"交给 coding worker","context_summary":""}',
            "修改 README 并验证",
        )

        self.assertEqual(decision.route, "delegate_new")
        self.assertEqual(decision.worker_kind, "coding")
        self.assertTrue(decision.requires_write)

    def test_router_bad_json_falls_back_to_general_worker(self) -> None:
        decision = AgentRouter.parse("not json", "解释一下项目结构")

        self.assertEqual(decision.route, "delegate_new")
        self.assertEqual(decision.worker_kind, "general")

    def test_router_extracts_negative_constraints_from_user_message(self) -> None:
        decision = AgentRouter.heuristic(
            "删除 notes/project.txt 之前先判断风险；如果会破坏测评工作区，请不要删除。"
        )

        self.assertIn("不要删除文件或目录。", decision.negative_constraints)
        self.assertIn("delete_path", decision.forbidden_actions)
        self.assertTrue(any("硬性约束" in item for item in decision.success_criteria))
        self.assertIn("硬性约束", decision.context_summary)

    def test_router_routes_command_and_direct_translation_without_case_names(self) -> None:
        command = AgentRouter.heuristic('运行 python -c "print(1)"，告诉我输出。')
        translation = AgentRouter.heuristic("不用工具，把 hello world 翻译成中文。")

        self.assertEqual(command.route, "delegate_new")
        self.assertEqual(command.worker_kind, "coding")
        self.assertEqual(translation.route, "answer_directly")

    def test_router_parse_accepts_principle_routing_cases(self) -> None:
        cases = [
            (
                "你好，你是谁？",
                {
                    "route": "answer_directly",
                    "worker_kind": "general",
                    "task_title": "介绍自己",
                    "task_brief": "直接回答用户身份问题",
                    "success_criteria": ["用户知道 openEagle 可以做什么"],
                    "answer": "我是 openEagle，可以直接聊，也能在需要时调度执行。",
                    "user_visible_summary": "",
                },
                ("answer_directly", "general", False),
            ),
            (
                "明天下午四点把行业新闻整理给我",
                {
                    "route": "delegate_new",
                    "worker_kind": "general",
                    "task_title": "明天下午整理行业新闻",
                    "task_brief": "创建持久化任务：明天下午四点整理行业新闻并提供给用户",
                    "success_criteria": ["已创建按时执行的持久化任务"],
                },
                ("delegate_new", "general", False),
            ),
            (
                "现在搜索今天的行业新闻",
                {
                    "route": "delegate_new",
                    "worker_kind": "research",
                    "task_title": "查询今天行业新闻",
                    "task_brief": "检索并汇总今天的行业新闻",
                    "success_criteria": ["提供今天行业新闻汇总"],
                },
                ("delegate_new", "research", False),
            ),
            (
                "看一下当前窗口，把登录表单填好",
                {
                    "route": "start_solo",
                    "worker_kind": "solo",
                    "task_title": "填写当前窗口登录表单",
                    "task_brief": "查看当前屏幕并填写登录表单",
                    "success_criteria": ["登录表单已按用户意图填写"],
                    "requires_gui": True,
                },
                ("start_solo", "solo", True),
            ),
            (
                "把这个处理一下",
                {
                    "route": "clarify",
                    "worker_kind": "general",
                    "task_title": "澄清任务目标",
                    "task_brief": "询问用户希望处理的对象和目标",
                    "success_criteria": ["获得足够执行的信息"],
                },
                ("clarify", "general", False),
            ),
        ]

        for content, payload_overrides, expected in cases:
            with self.subTest(content=content):
                decision = AgentRouter.parse(
                    self._model_route_payload(**payload_overrides),
                    content,
                )

                self.assertEqual(decision.route, expected[0])
                self.assertEqual(decision.worker_kind, expected[1])
                self.assertEqual(decision.requires_gui, expected[2])

        direct = AgentRouter.parse(
            self._model_route_payload(
                route="answer_directly",
                worker_kind="general",
                answer="我是 openEagle，可以直接聊，也能在需要时调度执行。",
                user_visible_summary="",
            ),
            "你是谁？",
        )
        self.assertEqual(direct.answer, "我是 openEagle，可以直接聊，也能在需要时调度执行。")
        self.assertEqual(direct.user_visible_summary, "")

    def test_router_parse_does_not_rewrite_incomplete_schedule_intent(self) -> None:
        decision = AgentRouter.parse(
            self._model_route_payload(
                route="clarify",
                worker_kind="general",
                task_title="澄清提醒内容",
                task_brief="询问用户希望提醒的具体事项",
                success_criteria=["获得提醒事项后再创建持久化任务"],
            ),
            "明天下午提醒我",
        )

        self.assertEqual(decision.route, "clarify")
        self.assertEqual(decision.worker_kind, "general")

    def test_router_prefers_solo_for_im_default(self) -> None:
        decision = AgentRouter.heuristic("打开浏览器查今天新闻", preferred_mode="solo")

        self.assertEqual(decision.route, "start_solo")
        self.assertEqual(decision.worker_kind, "solo")

    def test_router_allows_direct_chat_in_solo_mode(self) -> None:
        decision = AgentRouter.heuristic("你好", preferred_mode="solo")

        self.assertEqual(decision.route, "answer_directly")

    def test_main_router_prompt_includes_recent_workers(self) -> None:
        manager = SubAgentManager()
        decision = AgentRouter.heuristic("修改 README")
        task = manager.create_or_reuse("conv", decision)
        prompt = build_main_router_prompt("conv", "继续刚才的任务", recent_tasks=[task])

        self.assertIn(task.worker_id, prompt)
        self.assertIn("preferred_mode", prompt)

    def test_main_router_prompt_uses_principles_not_marker_fallbacks(self) -> None:
        instructions = "\n".join(build_main_router_instructions())
        prompt = build_main_router_prompt("conv", "明天下午四点把行业新闻整理给我")

        self.assertIn("worker 选择依据任务所需能力", instructions)
        self.assertIn("时间意图优先级", instructions)
        self.assertIn("clarify 仅用于：执行后无法撤销", instructions)
        self.assertIn("不确定但可以合理假设", instructions)
        self.assertIn("承接上下文", instructions)
        self.assertIn("preferred_mode=solo 时，如果任务涉及桌面状态感知或 GUI 操作", instructions)
        self.assertIn("按实际能力选 worker，忽略 preferred_mode", instructions)
        self.assertIn("context_summary 只填写 MainAgent 层才知道", instructions)
        self.assertIn("success_criteria 只写对 worker 有实际约束意义", instructions)
        self.assertIn("requires_write 与 requires_gui 是意图 hint", instructions)
        self.assertIn("user_visible_summary 是委派或桌面执行前展示给用户的一句话进展", instructions)
        self.assertIn("使用第一人称口语", instructions)
        self.assertIn("当 route=answer_directly 时，将回复写入 answer 字段", instructions)
        self.assertIn("negative_constraints", instructions)
        self.assertIn("forbidden_actions", instructions)
        self.assertIn("非即时的时间安排", prompt)
        self.assertIn("当前日期时间", prompt)
        self.assertIn("不要反问用户今天是周几", prompt)
        self.assertIn('"answer": "route=answer_directly 时的直接回复；其他 route 为空字符串"', prompt)
        self.assertEqual(prompt.count("用户说\""), 4)
        self.assertNotIn("缺少关键执行信息且无法合理推进", instructions)
        self.assertNotIn("当本轮不是纯聊天", instructions)
        self.assertNotIn('"user_visible_summary":"将', prompt)
        self.assertNotIn("将交给 research worker", prompt)
        self.assertNotIn("点帮我", instructions + prompt)


class SubAgentManagerTest(unittest.TestCase):
    def test_manager_reuses_existing_worker_for_followup(self) -> None:
        manager = SubAgentManager()
        first = manager.create_or_reuse("conv", AgentRouter.heuristic("修改 README"))
        followup = AgentRouter.heuristic("继续刚才那个", recent_tasks=[first])
        reused = manager.create_or_reuse("conv", followup)

        self.assertEqual(first.worker_id, reused.worker_id)
        self.assertIn(":worker:", reused.scoped_conversation_id)

    def test_manager_creates_new_worker_for_new_task(self) -> None:
        manager = SubAgentManager()
        first = manager.create_or_reuse("conv", AgentRouter.heuristic("修改 README"))
        second = manager.create_or_reuse("conv", AgentRouter.heuristic("查询天气"))

        self.assertNotEqual(first.worker_id, second.worker_id)

    def test_worker_prompt_contains_current_datetime(self) -> None:
        manager = SubAgentManager()
        task = manager.create_or_reuse("conv", AgentRouter.heuristic("今天有什么动漫更新"))

        prompt = SubAgentManager._build_worker_prompt(task)

        self.assertIn("当前日期时间", prompt)
        self.assertIn("不要反问用户今天是周几", prompt)
        self.assertIn("足够就立即停止调用工具", prompt)
        self.assertIn("工具成功但结果有限不属于执行失败", prompt)
        self.assertIn("批量 web_search", prompt)
        self.assertIn("文件名查找用 search_files", prompt)
        self.assertIn("只有用户明确要求 shell/脚本/系统命令", prompt)

    def test_worker_prompt_contains_negative_constraints(self) -> None:
        manager = SubAgentManager()
        decision = AgentRouter.heuristic(
            "删除 notes/project.txt 之前先判断风险；如果会破坏测评工作区，请不要删除。"
        )
        task = manager.create_or_reuse("conv", decision)

        prompt = SubAgentManager._build_worker_prompt(task)

        self.assertIn("硬性约束", prompt)
        self.assertIn("不要删除文件或目录", prompt)
        self.assertIn("delete_path", prompt)
        self.assertIn("如果工具动作与约束冲突", prompt)

    def test_worker_prompt_contains_persistent_conversation_and_previous_report(self) -> None:
        manager = SubAgentManager()
        task = manager.create_or_reuse("conv", AgentRouter.heuristic("继续修上下文"))
        task.last_report = WorkerReport(
            worker_id=task.worker_id,
            worker_kind=task.worker_kind,
            state="completed",
            title=task.title,
            summary="已定位问题",
            result="上次已经完成 SQLite 表设计。",
        )

        prompt = SubAgentManager._build_worker_prompt(
            task,
            conversation_context="用户: 客户端重启后也要能继续。",
        )

        self.assertIn("最近会话上下文", prompt)
        self.assertIn("客户端重启后也要能继续", prompt)
        self.assertIn("该 worker 上次交付", prompt)
        self.assertIn("SQLite 表设计", prompt)

    def test_worker_detects_tool_errors_for_agent_feedback(self) -> None:
        trace = ReplyTrace(
            trace_id="tool-1",
            kind="tool",
            name="read_text_file",
            status="completed",
            result="Error: 路径不存在",
            started_at="now",
        )

        self.assertTrue(SubAgentManager._trace_needs_agent_feedback(trace))
        self.assertTrue(
            SubAgentManager._should_retry_worker_output(
                "执行失败：路径不存在",
                [SubAgentManager._trace_feedback_text(trace)],
            )
        )
        self.assertFalse(
            SubAgentManager._should_retry_worker_output(
                "已完成，并使用了替代路径。",
                [SubAgentManager._trace_feedback_text(trace)],
            )
        )

    def test_worker_retry_prompt_tells_agent_to_self_repair(self) -> None:
        manager = SubAgentManager()
        task = manager.create_or_reuse("conv", AgentRouter.heuristic("读取文件"))

        prompt = SubAgentManager._build_worker_retry_prompt(
            task,
            errors=["Error: 路径不存在"],
            previous_output="执行失败",
            attempt=1,
        )

        self.assertIn("不要把下面的错误直接交给用户", prompt)
        self.assertIn("自己修正", prompt)
        self.assertIn("当前日期时间", prompt)

    def test_worker_provider_configuration_errors_are_not_self_retried(self) -> None:
        self.assertFalse(SubAgentManager._is_recoverable_worker_exception(ValueError("当前 provider 需要配置 API Key。")))


class AgentRuntimeTest(unittest.TestCase):
    def test_runtime_emits_agent_traces_for_delegated_worker(self) -> None:
        events: list[tuple[str, dict[str, object]]] = []

        async def send_event(
            type_: str,
            request_id: str,
            conversation_id: str,
            payload: dict[str, object],
        ) -> None:
            _ = (request_id, conversation_id)
            events.append((type_, payload))

        async def start_solo(conversation_id: str, task: str, request_id: str) -> str:
            _ = (conversation_id, task, request_id)
            return "solo started"

        async def solo_control(conversation_id: str, request_id: str, action: str) -> str:
            _ = (conversation_id, request_id)
            return f"solo {action}"

        runtime = AgentRuntime(
            config_getter=AppConfig,
            confirmation_store=ToolConfirmationStore(),
            confirmed_tool_results={},
            send_event=send_event,
            start_solo=start_solo,
            solo_control=solo_control,
        )

        reply = asyncio.run(runtime.handle_user_message("conv", "req", "修改 README"))

        self.assertIn("openEagle 已收到你的请求", reply)
        event_types = [type_ for type_, _payload in events]
        self.assertIn("server:agent_progress", event_types)
        self.assertLess(
            event_types.index("server:agent_progress"),
            event_types.index("server:trace"),
        )
        traces = [payload["trace"] for type_, payload in events if type_ == "server:trace"]
        self.assertTrue(any(trace["kind"] == "agent" for trace in traces))
        self.assertFalse(any(trace["name"] == "MainAgent" for trace in traces))
        self.assertTrue(any(trace["name"] == "coding-worker" for trace in traces))

    def test_runtime_uses_model_for_direct_answer_when_configured(self) -> None:
        events: list[tuple[str, dict[str, object]]] = []
        model_calls: list[tuple[str, str, str]] = []

        async def send_event(
            type_: str,
            request_id: str,
            conversation_id: str,
            payload: dict[str, object],
        ) -> None:
            _ = (request_id, conversation_id)
            events.append((type_, payload))

        async def start_solo(conversation_id: str, task: str, request_id: str) -> str:
            _ = (conversation_id, task, request_id)
            return "solo started"

        async def solo_control(conversation_id: str, request_id: str, action: str) -> str:
            _ = (conversation_id, request_id, action)
            return "solo control"

        runtime = AgentRuntime(
            config_getter=lambda: AppConfig(
                agent=AgentConfig(provider="openai", api_key="test-key"),
            ),
            confirmation_store=ToolConfirmationStore(),
            confirmed_tool_results={},
            send_event=send_event,
            start_solo=start_solo,
            solo_control=solo_control,
        )

        async def fake_route(*args, **kwargs):
            _ = (args, kwargs)
            return AgentRouter.heuristic("你好", preferred_mode="solo")

        async def fake_model_reply(conversation_id: str, content: str, config: AppConfig) -> str:
            model_calls.append((conversation_id, content, config.agent.provider))
            return "AI 生成的自然回复"

        runtime._direct_answer_with_model = fake_model_reply  # type: ignore[method-assign]
        with patch.object(AgentRouter, "route", fake_route):
            reply = asyncio.run(runtime.handle_user_message("conv", "req", "你好"))

        self.assertEqual(reply, "AI 生成的自然回复")
        self.assertEqual(model_calls, [("conv", "你好", "openai")])

    def test_runtime_prefers_router_answer_for_direct_reply(self) -> None:
        events: list[tuple[str, dict[str, object]]] = []

        async def send_event(
            type_: str,
            request_id: str,
            conversation_id: str,
            payload: dict[str, object],
        ) -> None:
            _ = (request_id, conversation_id)
            events.append((type_, payload))

        async def start_solo(conversation_id: str, task: str, request_id: str) -> str:
            _ = (conversation_id, task, request_id)
            return "solo started"

        async def solo_control(conversation_id: str, request_id: str, action: str) -> str:
            _ = (conversation_id, request_id, action)
            return "solo control"

        runtime = AgentRuntime(
            config_getter=lambda: AppConfig(
                agent=AgentConfig(provider="openai", api_key="test-key"),
            ),
            confirmation_store=ToolConfirmationStore(),
            confirmed_tool_results={},
            send_event=send_event,
            start_solo=start_solo,
            solo_control=solo_control,
        )

        async def fake_route(*args, **kwargs):
            _ = (args, kwargs)
            return AgentRouter.parse(
                json.dumps(
                    {
                        "route": "answer_directly",
                        "answer": "我是 openEagle，可以直接聊，也能调度执行。",
                        "task_title": "介绍 openEagle",
                        "task_brief": "",
                        "success_criteria": [],
                        "worker_kind": "general",
                        "target_worker_id": None,
                        "requires_write": False,
                        "requires_gui": False,
                        "user_visible_summary": "",
                        "context_summary": "",
                    },
                    ensure_ascii=False,
                ),
                "你是谁？",
            )

        async def fail_model_reply(conversation_id: str, content: str, config: AppConfig) -> str:
            _ = (conversation_id, content, config)
            raise AssertionError("direct answer model should not be called")

        runtime._direct_answer_with_model = fail_model_reply  # type: ignore[method-assign]
        with patch.object(AgentRouter, "route", fake_route):
            reply = asyncio.run(runtime.handle_user_message("conv", "req", "你是谁？"))

        self.assertEqual(reply, "我是 openEagle，可以直接聊，也能调度执行。")
        self.assertFalse(any(type_ == "server:agent_progress" for type_, _payload in events))
        messages = [payload for type_, payload in events if type_ == "server:message"]
        self.assertEqual(messages[-1]["route"], "answer_directly")
        self.assertEqual(messages[-1]["answer"], "我是 openEagle，可以直接聊，也能调度执行。")

    def test_runtime_passes_recent_conversation_to_main_agent_decision(self) -> None:
        events: list[tuple[str, dict[str, object]]] = []
        route_calls: list[dict[str, object]] = []

        async def send_event(
            type_: str,
            request_id: str,
            conversation_id: str,
            payload: dict[str, object],
        ) -> None:
            _ = (request_id, conversation_id)
            events.append((type_, payload))

        async def start_solo(conversation_id: str, task: str, request_id: str) -> str:
            _ = (conversation_id, task, request_id)
            return "solo started"

        async def solo_control(conversation_id: str, request_id: str, action: str) -> str:
            _ = (conversation_id, request_id, action)
            return "solo control"

        runtime = AgentRuntime(
            config_getter=AppConfig,
            confirmation_store=ToolConfirmationStore(),
            confirmed_tool_results={},
            send_event=send_event,
            start_solo=start_solo,
            solo_control=solo_control,
        )

        async def fake_route(*args, **kwargs):
            _ = args
            route_calls.append(dict(kwargs))
            answer = "据记录，遮天是每周三更新。" if len(route_calls) == 1 else "我去搜遮天更新频率。"
            return AgentRouter.parse(
                json.dumps(
                    {
                        "route": "answer_directly",
                        "answer": answer,
                        "task_title": "承接对话",
                        "task_brief": "",
                        "success_criteria": [],
                        "worker_kind": "general",
                        "target_worker_id": None,
                        "requires_write": False,
                        "requires_gui": False,
                        "user_visible_summary": "",
                        "context_summary": "",
                    },
                    ensure_ascii=False,
                ),
                str(kwargs["content"]),
            )

        with patch.object(AgentRouter, "route", fake_route):
            asyncio.run(runtime.handle_user_message("conv", "req1", "遮天一周好像要更新几次吧"))
            asyncio.run(runtime.handle_user_message("conv", "req2", "你搜搜看呢 我也忘了"))

        self.assertEqual(len(route_calls), 2)
        self.assertEqual(route_calls[0].get("conversation_context"), "")
        second_context = str(route_calls[1].get("conversation_context"))
        self.assertIn("遮天一周好像要更新几次吧", second_context)
        self.assertIn("据记录，遮天是每周三更新。", second_context)


class SoloStabilityTest(unittest.TestCase):
    def test_action_signature_redacts_text_url_query_and_command_body(self) -> None:
        self.assertEqual(action_signature("type_text", {"text": "secret token"}), "type_text:short")
        self.assertEqual(
            action_signature("open_url", {"url": "https://example.com/path?token=secret"}),
            "open_url:https://example.com/path",
        )
        command_signature = action_signature("execute_command", {"command": "git status --short"})
        self.assertTrue(command_signature.startswith("execute_command:git:"))
        self.assertNotIn("status --short", command_signature)

    def test_kernel_classifies_visual_no_op_without_immediate_pause(self) -> None:
        kernel = SoloAgentKernel.create("点击按钮")
        decision = SoloDecision(
            screen_state="按钮可见",
            thought_summary="[状态] 按钮可见 [上步] 未知 [决策] 点击",
            action="click",
            action_args={"x": 0.5, "y": 0.5},
            progress="点击按钮",
            is_task_done=False,
            confidence=0.8,
        )

        outcome = kernel.assess_step(
            decision,
            {"success": True, "action": "click", "visualChange": False},
            repeat_action_count=1,
            same_screenshot_count=1,
            repeat_action_signature_count=1,
        )

        self.assertFalse(outcome.semantic_success)
        self.assertEqual(outcome.outcome_class, "no_op")
        self.assertFalse(outcome.should_pause)

    def test_kernel_recovers_after_repeated_same_signature(self) -> None:
        kernel = SoloAgentKernel.create("点击按钮")
        decision = SoloDecision(
            screen_state="按钮可见",
            thought_summary="[状态] 按钮可见 [上步] 没变化 [决策] 点击",
            action="click",
            action_args={"x": 0.5, "y": 0.5},
            progress="点击按钮",
            is_task_done=False,
            confidence=0.5,
        )

        outcome = kernel.assess_step(
            decision,
            {"success": True, "action": "click", "visualChange": False},
            repeat_action_count=3,
            same_screenshot_count=1,
            repeat_action_signature_count=3,
        )

        self.assertTrue(kernel.recovery_mode)
        self.assertIn("动作签名", outcome.recovery_hint or "")

    def test_kernel_treats_successful_command_as_success_without_visual_change(self) -> None:
        kernel = SoloAgentKernel.create("运行测试")
        decision = SoloDecision(
            screen_state="终端可见",
            thought_summary="[状态] 终端 [上步] 成功 [决策] 命令",
            action="execute_command",
            action_args={"command": "echo ok"},
            progress="运行命令",
            is_task_done=False,
            confidence=0.9,
        )

        outcome = kernel.assess_step(
            decision,
            {"success": True, "action": "execute_command", "ok": True, "exitCode": 0, "visualChange": False},
            repeat_action_count=1,
            same_screenshot_count=2,
            repeat_action_signature_count=1,
        )

        self.assertTrue(outcome.semantic_success)
        self.assertEqual(outcome.outcome_class, "success")

    def test_kernel_records_repairable_action_block_without_immediate_pause(self) -> None:
        kernel = SoloAgentKernel.create("按回车")

        outcome = kernel.record_repairable_action_block("press_keys 缺少有效按键列表。")

        self.assertFalse(outcome.semantic_success)
        self.assertEqual(outcome.outcome_class, "failed")
        self.assertFalse(outcome.should_pause)
        self.assertTrue(kernel.recovery_mode)
        self.assertIn("action_args", outcome.recovery_hint or "")


class SoloDecisionParsingTest(unittest.TestCase):
    def test_solo_json_can_be_extracted_from_natural_language_wrapper(self) -> None:
        decision = SoloService._normalize_decision(
            '我准备执行下一步：{"thought_summary":"[状态] ok [上步] 上一步是否成功：是 [决策] 继续",'
            '"action":"screenshot","action_args":{},"expected_outcome":"刷新截图",'
            '"is_task_done":false,"agent_message":"我先看一下当前界面。"}'
        )

        self.assertEqual(decision.action, "screenshot")
        self.assertEqual(decision.agent_message, "我先看一下当前界面。")

    def test_solo_json_accepts_kernel_contract_fields(self) -> None:
        decision = SoloService._normalize_decision(
            '{"screen_state":"桌面可见","thought_summary":"[状态] 桌面 [上步] 成功 [决策] 打开应用",'
            '"action":"execute_command","action_args":{"command":"dir"},"progress":"准备执行",'
            '"is_task_done":false,"confidence":0.76,'
            '"plan_updates":[{"index":2,"status":"in_progress"}],'
            '"findings":["看到开始菜单"]}'
        )

        self.assertEqual(decision.screen_state, "桌面可见")
        self.assertEqual(decision.confidence, 0.76)
        self.assertEqual(decision.plan_updates[0]["status"], "in_progress")
        self.assertEqual(decision.findings, ["看到开始菜单"])

    def test_solo_json_accepts_batch_actions(self) -> None:
        decision = SoloService._normalize_decision(
            '{"screen_state":"输入框可见","thought_summary":"[状态] 可输入 [上步] 成功 [决策] 连续输入",'
            '"action":"click","action_args":{"x":0.5,"y":0.5},"progress":"输入并提交",'
            '"is_task_done":false,"confidence":0.9,'
            '"batch_actions":[{"action":"type_text","action_args":{"text":"hello"}},'
            '{"action":"press_keys","action_args":{"keys":["enter"]}}]}'
        )

        self.assertEqual(decision.batch_actions[0]["action"], "type_text")
        self.assertEqual(decision.batch_actions[1]["action_args"]["keys"], ["enter"])
        self.assertIn("batch_actions", SoloService.decision_dict(decision))

    def test_solo_fallback_decision_preserves_natural_language(self) -> None:
        decision = SoloService._fallback_decision_from_text(
            "我会先打开 QQ 音乐。",
            ValueError("bad json"),
        )

        self.assertEqual(decision.action, "screenshot")
        self.assertTrue(decision.used_parse_fallback)
        self.assertIn("我会先打开 QQ 音乐", decision.agent_message)
        self.assertIn("上一步是否成功", decision.thought_summary)

    def test_plain_language_output_skips_remote_repair(self) -> None:
        self.assertFalse(SoloService._should_attempt_json_repair("我会先打开 QQ 音乐。"))
        self.assertTrue(SoloService._should_attempt_json_repair('说明 {"action": "wait"}'))

    def test_decision_dict_keeps_agent_message_but_hides_raw_output(self) -> None:
        decision = SoloDecision(
            thought_summary="[状态] ok [上步] 上一步是否成功：是 [决策] done",
            action="wait",
            action_args={"ms": 800},
            expected_outcome="等待",
            is_task_done=False,
            agent_message="我会等一下。",
            raw_model_output="raw",
        )

        payload = SoloService.decision_dict(decision)
        self.assertEqual(payload["agent_message"], "我会等一下。")
        self.assertNotIn("raw_model_output", payload)


class PromptPolicyTest(unittest.TestCase):
    def test_chat_prompt_contains_command_first_and_visual_boundary(self) -> None:
        instructions = "\n".join(build_chat_instructions("conv", [], [], []))
        self.assertIn("优先使用最贴合的内置工具", instructions)
        self.assertIn("文件名查找用 search_files", instructions)
        self.assertIn("只有用户明确要求 shell/脚本/系统命令", instructions)
        self.assertIn("当前日期时间", instructions)
        self.assertIn("不要反问用户今天是周几", instructions)
        self.assertIn("不要启动视觉桌面动作", instructions)
        self.assertIn("小步精确修改", instructions)
        self.assertIn("CONFIRMATION_REQUIRED", instructions)

    def test_solo_prompt_contains_visual_boundary_and_json_contract(self) -> None:
        instructions = "\n".join(solo_decision_instructions("Windows 11"))
        self.assertIn("Windows 11 桌面", instructions)
        self.assertIn("仅输出合法 JSON", instructions)
        self.assertIn("screen_state", instructions)
        self.assertIn("thought_summary", instructions)
        self.assertIn("agent_message", instructions)
        self.assertIn("禁止写在 JSON 外", instructions)
        self.assertIn("Q1: 这件事能用命令行做吗", instructions)
        self.assertIn("优先 open_url 直达目标 URL", instructions)
        self.assertIn("execute_command，不要用鼠标键盘绕路", instructions)
        self.assertIn("action_args 参数规范", instructions)
        self.assertIn("plan_updates", instructions)
        self.assertIn("batch_actions", instructions)
        self.assertIn("confidence", instructions)
        self.assertIn("execute_command: {\"command\": string", instructions)
        self.assertIn("open_url: {\"url\": string}", instructions)
        self.assertIn("同一动作或同一思路连续执行 ≥3 次", instructions)
        self.assertIn("归一化比例值", instructions)
        self.assertIn("命令或截图上下文确认目标位置", instructions)
        self.assertIn("所有任务 finish 前都要有完成证据", instructions)
        self.assertIn("结束任务时必须 action=finish", instructions)

    def test_solo_dynamic_prompt_contains_step_history_requirements(self) -> None:
        prompt = build_solo_decision_prompt(
            "打开记事本",
            [{"step": 1, "decision": {"action": "execute_command"}}],
            kernel_state={
                "lastRecoveryHint": "换用 GUI 路线",
                "plan": [],
                "completionRequirement": "必须说明可见完成状态。",
            },
        )
        self.assertIn("用户任务：打开记事本", prompt)
        self.assertIn("当前日期时间", prompt)
        self.assertIn("不要猜年份", prompt)
        self.assertIn("步骤历史（最新在后，共 1 步）", prompt)
        self.assertIn("历史字段说明", prompt)
        self.assertIn("桌面执行内核状态", prompt)
        self.assertIn("换用 GUI 路线", prompt)
        self.assertIn("outputTail", prompt)
        self.assertIn("screenshot.contentHash", prompt)
        self.assertIn("agent_message", prompt)
        self.assertIn("screen_state", prompt)
        self.assertIn("confidence", prompt)
        self.assertIn("plan_updates", prompt)
        self.assertIn("batch_actions", prompt)
        self.assertIn("先判断上一步是否成功", prompt)
        self.assertIn("[状态]", prompt)
        self.assertIn("[上步]", prompt)
        self.assertIn("[决策]", prompt)
        self.assertIn("优先 open_url 直达", prompt)
        self.assertIn("完成门槛", prompt)
        self.assertIn("completionRequirement", prompt)
        self.assertIn("screen_state、progress、agent_message", prompt)

    def test_current_datetime_hint_includes_date_and_timezone(self) -> None:
        hint = current_datetime_hint()

        self.assertRegex(hint, r"\d{4}-\d{2}-\d{2}")
        self.assertRegex(hint, r"星期[一二三四五六日]")
        self.assertRegex(hint, r"[+-]\d{4}$")

    def test_direct_answer_prompt_contains_current_datetime(self) -> None:
        prompt = build_direct_answer_prompt("今天有什么动漫更新")

        self.assertIn("当前日期时间", prompt)
        self.assertIn("不要反问用户今天是周几", prompt)
        self.assertIn("今天有什么动漫更新", prompt)

    def test_solo_repair_prompt_converts_natural_language_to_json_decision(self) -> None:
        prompt = build_solo_repair_prompt(
            "播放 QQ 音乐",
            [],
            "我会打开 QQ 音乐。",
            "VL 输出不包含可解析 JSON。",
        )

        self.assertIn("无法解析为动作决策 JSON", prompt)
        self.assertIn("agent_message", prompt)
        self.assertIn("仅返回一个合法 JSON 对象", prompt)
        self.assertIn("action 仅可取", prompt)
        self.assertIn("open_url", prompt)
        self.assertIn("screen_state", prompt)
        self.assertIn("confidence", prompt)
        self.assertIn("batch_actions", prompt)


if __name__ == "__main__":
    unittest.main()
