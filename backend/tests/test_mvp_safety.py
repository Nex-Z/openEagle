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
from app.prompts import (
    build_chat_instructions,
    build_solo_decision_prompt,
    build_solo_repair_prompt,
    current_datetime_hint,
    solo_decision_instructions,
)
from app.safety import assess_solo_action, assess_tool_action, classify_command_risk
from app.solo_executor import SoloExecutor
from app.solo_kernel import SoloAgentKernel
from app.solo_service import (
    MODEL_IMAGE_MAX_LONG_EDGE,
    SoloDecision,
    SoloService,
    prepare_model_image,
    summarize_solo_step_result,
)


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
                "visualChange": False,
                "usedVirtualCapture": True,
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
        self.assertFalse(summary["visualChange"])
        self.assertTrue(summary["usedVirtualCapture"])
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
        self.assertIn("优先使用 run_command", instructions)
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
        self.assertIn("SOLO 内核状态", prompt)
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
        self.assertRegex(hint, r"[+-]\d{4}$")

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
