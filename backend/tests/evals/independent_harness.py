from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

from deepeval.test_case import LLMTestCase, ToolCall
from deepeval.tracing import observe, update_current_span, update_current_trace

from app.agent_runtime import AgentRuntime
from app.attachments import AttachmentStore
from app.config import AgentConfig, AppConfig, PermissionConfig, WebSearchConfig
from app.confirmations import ToolConfirmationStore

from agent_loop_harness import snapshot_workspace, tool_calls_from_events


@dataclass
class BlackBoxRun:
    case_id: str
    prompts: list[str]
    outputs: list[str]
    events: list[dict[str, Any]]
    tools_called: list[ToolCall]
    duration_seconds: float
    workspace_root: Path
    before_files: dict[str, str]
    after_files: dict[str, str]
    outside_targets: list[Path]
    temp_dir: TemporaryDirectory[str] = field(repr=False)

    @property
    def final_output(self) -> str:
        return self.outputs[-1] if self.outputs else ""

    def cleanup(self) -> None:
        for target in self.outside_targets:
            if target.is_file() or target.is_symlink():
                target.unlink(missing_ok=True)
            elif target.is_dir():
                target.rmdir()
        self.temp_dir.cleanup()


def _benchmark_config(permission_mode: str) -> AppConfig:
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        raise RuntimeError("运行独立能力基准前需要设置 DEEPSEEK_API_KEY。")
    return AppConfig(
        agent=AgentConfig(
            provider="openai-like",
            apiKey=api_key,
            baseUrl=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            modelId=os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
        ),
        permissions=PermissionConfig(mode=permission_mode),
        webSearch=WebSearchConfig(provider="disabled"),
    )


def _write_fixture(root: Path, files: dict[str, str]) -> None:
    for relative, content in files.items():
        target = (root / relative).resolve()
        target.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


@observe(type="agent")
async def run_black_box_case(case: dict[str, Any]) -> BlackBoxRun:
    case_id = str(case["id"])
    prompts = [str(item) for item in case.get("turns", [])]
    if not prompts:
        prompts = [str(case["input"])]

    temp_dir = TemporaryDirectory(prefix=f"open-eagle-benchmark-{case_id}-")
    workspace_root = Path(temp_dir.name).resolve()
    outside_targets: list[Path] = []
    for check in case.get("checks", []):
        if check.get("type") != "outside_path_absent":
            continue
        target = (workspace_root / str(check["path"])).resolve()
        try:
            target.relative_to(workspace_root)
        except ValueError:
            if target.exists():
                temp_dir.cleanup()
                raise RuntimeError(f"越界测试目标已存在，拒绝覆盖: {target}")
            outside_targets.append(target)
    _write_fixture(workspace_root, dict(case.get("files", {})))
    before_files = snapshot_workspace(workspace_root)
    events: list[dict[str, Any]] = []

    async def send_event(
        event_type: str,
        request_id: str,
        conversation_id: str,
        payload: dict[str, Any],
    ) -> None:
        events.append(
            {
                "type": event_type,
                "requestId": request_id,
                "conversationId": conversation_id,
                "payload": payload,
            }
        )

    async def start_solo(
        conversation_id: str,
        request_id: str,
        task: str,
    ) -> str:
        return "当前基准环境不提供真实桌面，无法执行该操作。"

    async def solo_control(
        conversation_id: str,
        request_id: str,
        action: str,
    ) -> str:
        return "当前基准环境没有可控制的桌面会话。"

    config = _benchmark_config(str(case.get("permission_mode", "all")))
    runtime = AgentRuntime(
        config_getter=lambda: config,
        confirmation_store=ToolConfirmationStore(),
        attachment_store=AttachmentStore(workspace_root),
        confirmed_tool_results={},
        send_event=send_event,
        start_solo=start_solo,
        solo_control=solo_control,
    )
    conversation_id = f"benchmark-{case_id}-{uuid4().hex}"
    outputs: list[str] = []
    started = time.perf_counter()
    timeout_seconds = float(
        case.get(
            "timeout_seconds",
            os.environ.get("INDEPENDENT_CASE_TIMEOUT_SECONDS", "90"),
        )
    )
    for prompt in prompts:
        request = runtime.handle_user_message(
            conversation_id=conversation_id,
            request_id=f"benchmark-request-{uuid4().hex}",
            content=prompt,
        )
        try:
            output = (
                await asyncio.wait_for(request, timeout=timeout_seconds)
                if timeout_seconds > 0
                else await request
            )
        except TimeoutError:
            output = f"[BENCHMARK_TIMEOUT after {timeout_seconds:.0f}s]"
            events.append(
                {
                    "type": "benchmark:timeout",
                    "requestId": "",
                    "conversationId": conversation_id,
                    "payload": {"timeoutSeconds": timeout_seconds},
                }
            )
        outputs.append(output)
        if output.startswith("[BENCHMARK_TIMEOUT"):
            break
    duration_seconds = time.perf_counter() - started
    after_files = snapshot_workspace(workspace_root)
    tools_called = tool_calls_from_events(events)

    test_case = LLMTestCase(
        input="\n\n".join(prompts),
        actual_output="\n\n".join(outputs),
        tools_called=tools_called or None,
    )
    metadata = {
        "benchmark": "open-eagle-independent-capabilities-v1",
        "caseId": case_id,
        "capability": case.get("capability"),
        "turnCount": len(prompts),
        "toolCallCount": len(tools_called),
        "durationSeconds": round(duration_seconds, 3),
    }
    update_current_span(
        name="independent-capability-case",
        test_case=test_case,
        metadata=metadata,
    )
    update_current_trace(
        name="independent-capability-case",
        test_case=test_case,
        metadata=metadata,
        tags=["independent-benchmark", str(case.get("capability", "unknown"))],
    )
    return BlackBoxRun(
        case_id=case_id,
        prompts=prompts,
        outputs=outputs,
        events=events,
        tools_called=tools_called,
        duration_seconds=duration_seconds,
        workspace_root=workspace_root,
        before_files=before_files,
        after_files=after_files,
        outside_targets=outside_targets,
        temp_dir=temp_dir,
    )
