"""openEagle 真实 AgentRuntime 多轮回调。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

from deepeval.test_case import LLMTestCase, Turn
from deepeval.tracing import observe, update_current_span, update_current_trace

from app.agent_runtime import AgentRuntime
from app.attachments import AttachmentStore
from app.confirmations import ToolConfirmationStore

from agent_loop_harness import (
    build_eval_config,
    seed_workspace,
    tool_calls_from_events,
)


@dataclass
class RuntimeConversation:
    temp_dir: TemporaryDirectory[str]
    runtime: AgentRuntime
    events: list[dict[str, Any]] = field(default_factory=list)


_sessions: dict[str, RuntimeConversation] = {}


def _build_session(session_id: str) -> RuntimeConversation:
    temp_dir = TemporaryDirectory(prefix=f"open-eagle-conversation-{session_id[:16]}-")
    workspace_root = Path(temp_dir.name).resolve()
    seed_workspace(workspace_root)
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
        return f"桌面执行 worker 已接收任务：{task}"

    async def solo_control(
        conversation_id: str,
        request_id: str,
        action: str,
    ) -> str:
        return f"桌面执行状态已切换为：{action}"

    runtime = AgentRuntime(
        config_getter=build_eval_config,
        confirmation_store=ToolConfirmationStore(),
        attachment_store=AttachmentStore(workspace_root),
        confirmed_tool_results={},
        send_event=send_event,
        start_solo=start_solo,
        solo_control=solo_control,
    )
    return RuntimeConversation(temp_dir=temp_dir, runtime=runtime, events=events)


def _get_session(thread_id: str | None) -> tuple[str, RuntimeConversation]:
    session_id = thread_id or f"fallback-{uuid4().hex}"
    session = _sessions.get(session_id)
    if session is None:
        session = _build_session(session_id)
        _sessions[session_id] = session
    return session_id, session


@observe(type="agent")
async def chatbot_callback(
    input: str,
    turns: list[Turn] | None = None,
    thread_id: str | None = None,
) -> Turn:
    session_id, session = _get_session(thread_id)
    event_offset = len(session.events)
    history = [
        {"role": turn.role, "content": turn.content}
        for turn in (turns or [])[-8:]
    ]
    output = await session.runtime.handle_user_message(
        conversation_id=f"eval-conversation-{session_id}",
        request_id=f"eval-request-{uuid4().hex}",
        content=input,
        history=history,
    )
    tools_called = tool_calls_from_events(session.events[event_offset:])
    test_case = LLMTestCase(
        input=input,
        actual_output=output,
        tools_called=tools_called or None,
    )
    metadata = {
        "threadId": session_id,
        "turnCount": len(turns or []) + 1,
        "toolCallCount": len(tools_called),
    }
    update_current_span(
        name="open-eagle-multi-turn",
        test_case=test_case,
        metadata=metadata,
    )
    update_current_trace(
        name="open-eagle-multi-turn",
        test_case=test_case,
        thread_id=session_id,
        metadata=metadata,
    )
    return Turn(role="assistant", content=output)
