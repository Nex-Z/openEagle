from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch
from uuid import uuid4

from deepeval.test_case import LLMTestCase, ToolCall
from deepeval.tracing import observe, update_current_span, update_current_trace

from app.agent_router import AgentRouter
from app.agent_runtime import AgentRuntime
from app.attachments import AttachmentStore
from app.config import AgentConfig, AppConfig, PermissionConfig, WebSearchConfig
from app.confirmations import ToolConfirmationStore
from app.subagent_models import AgentRouteDecision
from app.token_usage import init_token_usage_db, token_usage_scope

TRACE_ONLY_TOOL_PARAM_KEYS = {"agentTaskId", "workerKind"}


@dataclass
class AgentLoopRun:
    input: str
    output: str
    route: str
    worker_kind: str
    events: list[dict[str, Any]]
    tools_called: list[ToolCall]
    duration_seconds: float
    workspace_root: Path
    workspace_files: dict[str, str] = field(default_factory=dict)
    token_usage: dict[str, Any] = field(default_factory=dict)

    @property
    def tool_names(self) -> list[str]:
        return [tool.name for tool in self.tools_called]

    @property
    def error_tool_count(self) -> int:
        return sum(
            1
            for event in completed_trace_events(self.events)
            if event.get("kind") in {"tool", "mcp"} and event.get("status") == "error"
        )

    @property
    def repair_count(self) -> int:
        return sum(
            1
            for event in completed_trace_events(self.events)
            if "self-repair" in str(event.get("name", ""))
        )


def build_eval_config() -> AppConfig:
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        raise RuntimeError("运行全链路测评前需要设置 DEEPSEEK_API_KEY。")
    return AppConfig(
        agent=AgentConfig(
            provider="openai-like",
            apiKey=api_key,
            baseUrl=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            modelId=os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
        ),
        permissions=PermissionConfig(mode="all"),
        webSearch=WebSearchConfig(provider="disabled"),
    )


def seed_workspace(root: Path) -> None:
    (root / "notes").mkdir(parents=True, exist_ok=True)
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text(
        "# openEagle Eval Workspace\n\n这是隔离的 Agent 测评工作区。\n",
        encoding="utf-8",
    )
    (root / "notes" / "project.txt").write_text(
        "项目代号：Falcon\n状态：进行中\n负责人：Lin\n",
        encoding="utf-8",
    )
    (root / "src" / "sample.py").write_text(
        "def add(left: int, right: int) -> int:\n    return left + right\n",
        encoding="utf-8",
    )


def snapshot_workspace(root: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or ".open-eagle" in path.parts:
            continue
        relative = path.relative_to(root).as_posix()
        try:
            snapshot[relative] = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            snapshot[relative] = f"<binary:{path.stat().st_size}>"
    return snapshot


def completed_trace_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    traces: list[dict[str, Any]] = []
    for event in events:
        if event.get("type") != "server:trace":
            continue
        trace = event.get("payload", {}).get("trace", {})
        if not isinstance(trace, dict):
            continue
        if trace.get("status") in {"completed", "error"}:
            traces.append(trace)
    return traces


def tool_calls_from_events(events: list[dict[str, Any]]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    seen: set[tuple[str, str]] = set()
    for trace in completed_trace_events(events):
        if trace.get("kind") not in {"tool", "mcp"}:
            continue
        name = str(trace.get("name") or "unknown")
        trace_id = str(trace.get("id") or "")
        dedupe_key = (trace_id, name)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        params = trace.get("params")
        business_params = (
            {
                key: value
                for key, value in params.items()
                if key not in TRACE_ONLY_TOOL_PARAM_KEYS
            }
            if isinstance(params, dict)
            else {}
        )
        calls.append(
            ToolCall(
                name=name,
                input_parameters=business_params,
                output=trace.get("result"),
            )
        )
    return calls


@observe(type="agent")
def _record_route_span(decision: AgentRouteDecision) -> None:
    update_current_span(
        name="main-agent-router",
        input={"task": decision.task_brief},
        output=decision.model_dump(),
        metadata={
            "route": decision.route,
            "workerKind": decision.worker_kind,
            "requiresWrite": decision.requires_write,
            "requiresGui": decision.requires_gui,
        },
    )


@observe(type="tool")
def _record_tool_span(tool: ToolCall, status: str) -> None:
    update_current_span(
        name=tool.name,
        input=tool.input_parameters or {},
        output=tool.output,
        metadata={"status": status},
    )


@observe(type="agent")
def _record_worker_span(trace: dict[str, Any]) -> None:
    update_current_span(
        name=str(trace.get("name") or "worker"),
        input=trace.get("params"),
        output=trace.get("result") or trace.get("summary"),
        metadata={"status": trace.get("status"), "kind": trace.get("kind")},
    )


@observe(type="agent")
async def run_agent_golden(
    *,
    input_text: str,
    expected_output: str | None = None,
    expected_tools: list[ToolCall] | None = None,
) -> AgentLoopRun:
    events: list[dict[str, Any]] = []
    captured_decisions: list[AgentRouteDecision] = []

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
        return f"桌面执行 worker 已接收任务：{task}"

    async def solo_control(
        conversation_id: str,
        request_id: str,
        action: str,
    ) -> str:
        return f"桌面执行状态已切换为：{action}"

    original_route = AgentRouter.route

    async def capture_route(
        router: AgentRouter,
        *args: Any,
        **kwargs: Any,
    ) -> AgentRouteDecision:
        decision = await original_route(router, *args, **kwargs)
        captured_decisions.append(decision.model_copy(deep=True))
        return decision

    with TemporaryDirectory(prefix="open-eagle-eval-") as temp_dir:
        workspace_root = Path(temp_dir).resolve()
        seed_workspace(workspace_root)
        config = build_eval_config()
        runtime = AgentRuntime(
            config_getter=lambda: config,
            confirmation_store=ToolConfirmationStore(),
            attachment_store=AttachmentStore(workspace_root),
            confirmed_tool_results={},
            send_event=send_event,
            start_solo=start_solo,
            solo_control=solo_control,
        )
        conversation_id = f"eval-{uuid4().hex}"
        request_id = f"request-{uuid4().hex}"
        init_token_usage_db(workspace_root / ".open-eagle" / "eval-token-usage.db")
        started = time.perf_counter()
        async with token_usage_scope(
            request_id=request_id,
            conversation_id=conversation_id,
            source="agent_eval",
        ) as request_usage:
            with patch.object(AgentRouter, "route", capture_route):
                output = await runtime.handle_user_message(
                    conversation_id,
                    request_id,
                    input_text,
                )
        duration_seconds = time.perf_counter() - started
        decision = captured_decisions[-1]
        tools_called = tool_calls_from_events(events)
        workspace_files = snapshot_workspace(workspace_root)

        _record_route_span(decision)
        tool_statuses = {
            str(trace.get("name") or "unknown"): str(trace.get("status") or "unknown")
            for trace in completed_trace_events(events)
            if trace.get("kind") in {"tool", "mcp"}
        }
        for tool in tools_called:
            _record_tool_span(tool, tool_statuses.get(tool.name, "unknown"))
        for trace in completed_trace_events(events):
            if trace.get("kind") == "agent":
                _record_worker_span(trace)

        test_case = LLMTestCase(
            input=input_text,
            actual_output=output,
            expected_output=expected_output,
            tools_called=tools_called,
            expected_tools=expected_tools or [],
        )
        update_current_span(
            name="open-eagle-agent-loop",
            test_case=test_case,
            metadata={
                "route": decision.route,
                "workerKind": decision.worker_kind,
                "durationSeconds": round(duration_seconds, 3),
                "toolCallCount": len(tools_called),
                "toolErrorCount": sum(
                    1
                    for trace in completed_trace_events(events)
                    if trace.get("kind") in {"tool", "mcp"}
                    and trace.get("status") == "error"
                ),
                "workspaceFiles": sorted(workspace_files),
            },
        )
        update_current_trace(
            name="open-eagle-agent-loop",
            test_case=test_case,
            metadata={
                "route": decision.route,
                "workerKind": decision.worker_kind,
                "durationSeconds": round(duration_seconds, 3),
                "toolCallCount": len(tools_called),
                "toolErrorCount": sum(
                    1
                    for trace in completed_trace_events(events)
                    if trace.get("kind") in {"tool", "mcp"}
                    and trace.get("status") == "error"
                ),
            },
        )
        return AgentLoopRun(
            input=input_text,
            output=output,
            route=decision.route,
            worker_kind=decision.worker_kind,
            events=events,
            tools_called=tools_called,
            duration_seconds=duration_seconds,
            workspace_root=workspace_root,
            workspace_files=workspace_files,
            token_usage=request_usage.payload(),
        )
